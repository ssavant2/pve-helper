from types import SimpleNamespace

from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from core.models import CurrentGuestInventory, ProxmoxEndpoint, ProxmoxInventory, ScanRun
from core.services.current_guest_inventory import (
    ScanGuestObservation,
    reconcile_live_guest_inventory,
    reconcile_scan_guest_inventory,
    refresh_current_guest_from_client,
    update_current_guest_config,
)
from core.services.proxmox import ProxmoxAPIError, ProxmoxGuestSummary, VerifiedGuestInventory
from core.tasks import refresh_current_guest_inventory


class CurrentGuestInventoryTests(TestCase):
    def setUp(self):
        self.pve1 = ProxmoxEndpoint.objects.create(name="pve1", url="https://pve1:8006")
        self.pve2 = ProxmoxEndpoint.objects.create(name="pve2", url="https://pve2:8006")
        self.scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)

    @staticmethod
    def scan_guest(*, node, object_type, vmid, name, tags=""):
        return SimpleNamespace(
            node=node,
            object_type=object_type,
            vmid=vmid,
            name=name,
            status="stopped",
            config={"tags": tags} if tags else {},
            disk_references=[],
        )

    def current_guest(self, *, endpoint, object_type, vmid, name):
        return CurrentGuestInventory.objects.create(
            source_endpoint=endpoint,
            source_scan=self.scan,
            node=endpoint.name,
            object_type=object_type,
            vmid=vmid,
            name=name,
            config={},
            observed_at=timezone.now(),
        )

    def test_complete_scan_atomically_replaces_membership(self):
        self.current_guest(endpoint=self.pve1, object_type="vm", vmid=99, name="deleted")
        existing = self.current_guest(endpoint=self.pve2, object_type="ct", vmid=200, name="old-name")
        observations = [
            ScanGuestObservation(
                self.pve1,
                self.scan_guest(node="pve1", object_type="vm", vmid=100, name="new-vm", tags="prod"),
            ),
            ScanGuestObservation(
                self.pve2,
                self.scan_guest(node="pve2", object_type="ct", vmid=200, name="new-name"),
            ),
        ]

        state = reconcile_scan_guest_inventory(
            scan=self.scan,
            observations=observations,
            attempted_endpoints=[self.pve1, self.pve2],
            successful_endpoints=[self.pve1, self.pve2],
            errors={},
        )

        self.assertTrue(state.complete)
        self.assertEqual(set(CurrentGuestInventory.objects.values_list("object_type", "vmid")), {("vm", 100), ("ct", 200)})
        existing.refresh_from_db()
        self.assertEqual(existing.name, "new-name")
        self.assertEqual(CurrentGuestInventory.objects.get(vmid=100).config["tags"], "prod")

    def test_partial_scan_only_retires_membership_from_successful_endpoints(self):
        self.current_guest(endpoint=self.pve1, object_type="vm", vmid=101, name="gone-from-pve1")
        preserved = self.current_guest(endpoint=self.pve2, object_type="vm", vmid=202, name="preserved")

        state = reconcile_scan_guest_inventory(
            scan=self.scan,
            observations=[
                ScanGuestObservation(
                    self.pve1,
                    self.scan_guest(node="pve1", object_type="vm", vmid=100, name="seen"),
                )
            ],
            attempted_endpoints=[self.pve1, self.pve2],
            successful_endpoints=[self.pve1],
            errors={"pve2": "unavailable"},
        )

        self.assertFalse(state.complete)
        self.assertFalse(CurrentGuestInventory.objects.filter(vmid=101).exists())
        self.assertTrue(CurrentGuestInventory.objects.filter(pk=preserved.pk).exists())
        self.assertTrue(CurrentGuestInventory.objects.filter(vmid=100).exists())

    def test_partial_live_refresh_adds_and_updates_but_never_deletes_unseen_guests(self):
        preserved = self.current_guest(endpoint=self.pve2, object_type="vm", vmid=202, name="preserved")
        result = VerifiedGuestInventory(
            guests=(
                ProxmoxGuestSummary(
                    node="pve1",
                    object_type="vm",
                    vmid=100,
                    name="new-live",
                    status="running",
                    cpu=0.25,
                    mem=1024,
                    maxmem=2048,
                    uptime=90,
                    lock="backup",
                    tags=("prod",),
                ),
            ),
            attempted_endpoints=(self.pve1.url, self.pve2.url),
            successful_endpoints=(self.pve1.url,),
            errors=("pve2 unavailable",),
        )

        state = reconcile_live_guest_inventory(result)

        self.assertFalse(state.complete)
        self.assertTrue(CurrentGuestInventory.objects.filter(pk=preserved.pk).exists())
        new_guest = CurrentGuestInventory.objects.get(vmid=100)
        self.assertEqual(new_guest.config["tags"], "prod")
        self.assertFalse(new_guest.config_complete)
        self.assertEqual(new_guest.cpu_usage, 0.25)
        self.assertEqual(new_guest.memory_used_bytes, 1024)
        self.assertEqual(new_guest.uptime_seconds, 90)
        self.assertEqual(new_guest.runtime_lock, "backup")
        self.assertIsNotNone(new_guest.runtime_observed_at)

    def test_complete_live_refresh_removes_guests_absent_from_authoritative_membership(self):
        self.current_guest(endpoint=self.pve2, object_type="vm", vmid=202, name="deleted")
        result = VerifiedGuestInventory(
            guests=(ProxmoxGuestSummary(node="pve1", object_type="vm", vmid=100, name="kept", status="running"),),
            attempted_endpoints=(self.pve1.url, self.pve2.url),
            successful_endpoints=(self.pve1.url, self.pve2.url),
            errors=(),
        )

        state = reconcile_live_guest_inventory(result)

        self.assertTrue(state.complete)
        self.assertEqual(list(CurrentGuestInventory.objects.values_list("vmid", flat=True)), [100])

    def test_direct_guest_updates_do_not_mutate_historical_scan_evidence(self):
        historical = ProxmoxInventory.objects.create(
            scan_run=self.scan,
            node="pve1",
            object_type="vm",
            vmid=100,
            config={"tags": "old"},
        )
        current = self.current_guest(endpoint=self.pve1, object_type="vm", vmid=100, name="vm")
        current.config = {"tags": "old"}
        current.save(update_fields=["config"])

        update_current_guest_config(
            object_type="vm",
            vmid=100,
            updates={"tags": "new"},
            delete=[],
        )

        current.refresh_from_db()
        historical.refresh_from_db()
        self.assertEqual(current.config["tags"], "new")
        self.assertEqual(historical.config["tags"], "old")

    def test_direct_guest_update_creates_a_partial_current_row_when_missing(self):
        update_current_guest_config(
            object_type="vm",
            vmid=303,
            node="pve1",
            updates={"tags": "new"},
            delete=[],
        )

        current = CurrentGuestInventory.objects.get(object_type="vm", vmid=303)
        self.assertEqual(current.node, "pve1")
        self.assertEqual(current.config, {"tags": "new"})
        self.assertFalse(current.config_complete)

    def test_targeted_refresh_updates_power_state_and_authoritative_config_immediately(self):
        current = self.current_guest(endpoint=self.pve1, object_type="vm", vmid=100, name="old")
        client = Mock(endpoint=self.pve1.url)
        client.guest_current.return_value = {
            "status": "running",
            "cpu": 0.5,
            "mem": 1024,
            "maxmem": 4096,
            "uptime": 12,
        }
        client.guest_config.return_value = {"name": "renamed", "tags": "prod"}

        result = refresh_current_guest_from_client(
            client,
            node="pve1",
            object_type="vm",
            vmid=100,
        )

        self.assertTrue(result.found)
        current.refresh_from_db()
        self.assertEqual(current.status, "running")
        self.assertEqual(current.name, "renamed")
        self.assertEqual(current.cpu_usage, 0.5)
        self.assertEqual(current.config["tags"], "prod")
        self.assertIsNotNone(current.runtime_observed_at)
        self.assertIsNotNone(current.config_observed_at)

    def test_targeted_refresh_discovers_migrated_node(self):
        current = self.current_guest(endpoint=self.pve1, object_type="vm", vmid=100, name="vm")
        client = Mock(endpoint=self.pve1.url)
        client.guest_current.side_effect = [
            ProxmoxAPIError("not on old node"),
            {"status": "running"},
        ]
        client.get.return_value = [{"type": "qemu", "vmid": 100, "node": "pve2"}]
        client.guest_config.return_value = {"name": "vm"}

        result = refresh_current_guest_from_client(
            client,
            node="pve1",
            object_type="vm",
            vmid=100,
            allow_relocation=True,
        )

        self.assertTrue(result.found)
        self.assertEqual(result.node, "pve2")
        current.refresh_from_db()
        self.assertEqual(current.node, "pve2")

    def test_targeted_refresh_deletes_only_after_authoritative_absence(self):
        self.current_guest(endpoint=self.pve1, object_type="vm", vmid=100, name="deleted")
        client = Mock(endpoint=self.pve1.url)
        client.guest_current.side_effect = ProxmoxAPIError("not found")
        client.get.return_value = []

        result = refresh_current_guest_from_client(
            client,
            node="pve1",
            object_type="vm",
            vmid=100,
            allow_relocation=True,
            delete_if_authoritatively_absent=True,
        )

        self.assertTrue(result.absent)
        self.assertFalse(CurrentGuestInventory.objects.filter(vmid=100).exists())

    @patch("core.tasks.fetch_verified_guest_inventory")
    def test_periodic_refresh_updates_projection_outside_the_request(self, fetch_inventory):
        fetch_inventory.return_value = VerifiedGuestInventory(
            guests=(
                ProxmoxGuestSummary(
                    node="pve1",
                    object_type="vm",
                    vmid=100,
                    name="periodic",
                    status="running",
                    cpu=0.75,
                ),
            ),
            attempted_endpoints=(self.pve1.url,),
            successful_endpoints=(self.pve1.url,),
            errors=(),
        )

        result = refresh_current_guest_inventory()

        self.assertFalse(result["skipped"])
        self.assertTrue(result["complete"])
        guest = CurrentGuestInventory.objects.get(vmid=100)
        self.assertEqual(guest.status, "running")
        self.assertEqual(guest.cpu_usage, 0.75)

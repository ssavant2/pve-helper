from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import (
    AuditEvent,
    CurrentGuestInventory,
    ProxmoxCluster,
    RuntimeConfigurationState,
    ScanRun,
    ScheduledAction,
)
from core.services.cluster_activation import (
    ClusterActivationError,
    activate_multicluster_identity,
    enable_cluster,
)
from core.services.refs import GuestRef


class MulticlusterRouteInventoryTests(SimpleTestCase):
    """A new object/node route must join the qualification migration explicitly."""

    def test_cluster_qualified_route_inventory_is_complete_and_ratchets(self):
        from core.urls import urlpatterns

        routes = [str(pattern.pattern) for pattern in urlpatterns]

        self.assertEqual(sum(route.startswith("vms/<str:cluster_key>/<str:object_type>") for route in routes), 37)
        self.assertEqual(sum(route.startswith("vms/<str:cluster_key>/") for route in routes), 40)
        self.assertEqual(sum(route.startswith("vms/<str:object_type>") for route in routes), 37)
        # The datastore object view: eight tabs in two scope shapes each, plus the
        # cluster-wide catalog refresh.
        self.assertEqual(sum(route.startswith("clusters/<str:cluster_key>/datastores/") for route in routes), 9)
        self.assertEqual(
            sum(route.startswith("clusters/<str:cluster_key>/nodes/<str:node>/datastores/") for route in routes), 8
        )
        self.assertEqual(sum(route.startswith("storage-api/<str:node>/<str:storage>/") for route in routes), 8)

    def test_cluster_scope_is_not_read_from_session_state(self):
        offenders = []
        for path in sorted((Path(settings.BASE_DIR) / "core").rglob("*.py")):
            if "migrations" in path.parts or path.name.startswith("tests"):
                continue
            source = path.read_text()
            if 'request.session["cluster' in source or "request.session['cluster" in source:
                offenders.append(str(path.relative_to(settings.BASE_DIR)))
        self.assertEqual(offenders, [])


@override_settings(APP_REQUIRE_LOGIN=False)
class LegacyClusterUrlTests(TestCase):
    def setUp(self):
        self.cluster_a = ProxmoxCluster.objects.create(key="a", display_name="Cluster A", enabled=True)
        self.cluster_b = ProxmoxCluster.objects.create(key="b", display_name="Cluster B", enabled=True)
        self._guest(self.cluster_a, vmid=500, node="pve1", name="a-500")

    def _guest(self, cluster, *, vmid, node, name):
        return CurrentGuestInventory.objects.create(
            cluster=cluster,
            node=node,
            object_type="vm",
            vmid=vmid,
            name=name,
            observed_at=timezone.now(),
        )

    def test_unique_legacy_guest_get_redirects_and_preserves_query(self):
        response = self.client.get(
            reverse("core:legacy_guest_summary", kwargs={"object_type": "vm", "vmid": 500}),
            {"from": "bookmark"},
        )

        self.assertRedirects(
            response,
            reverse(
                "core:guest_summary",
                kwargs={"cluster_key": "a", "object_type": "vm", "vmid": 500},
            )
            + "?from=bookmark",
            fetch_redirect_response=False,
        )

    def test_ambiguous_legacy_guest_get_returns_cluster_chooser(self):
        self._guest(self.cluster_b, vmid=500, node="pve1", name="b-500")

        response = self.client.get(reverse("core:legacy_guest_summary", kwargs={"object_type": "vm", "vmid": 500}))

        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "/vms/a/vm/500/summary/", status_code=409)
        self.assertContains(response, "/vms/b/vm/500/summary/", status_code=409)

    def test_legacy_guest_mutation_is_rejected_even_when_unique(self):
        response = self.client.post(
            reverse("core:legacy_guest_power", kwargs={"object_type": "vm", "vmid": 500}),
            {"action": "start"},
        )

        self.assertEqual(response.status_code, 409)

    def test_unscoped_tags_require_an_explicit_cluster_when_several_are_enabled(self):
        response = self.client.get(reverse("core:legacy_tags_overview"))

        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "/clusters/a/tags/", status_code=409)
        self.assertContains(response, "/clusters/b/tags/", status_code=409)

    def test_legacy_node_url_is_ambiguous_across_same_named_nodes(self):
        self._guest(self.cluster_b, vmid=501, node="pve1", name="b-501")

        response = self.client.get(
            reverse(
                "core:legacy_api_storage_summary",
                kwargs={"node": "pve1", "storage": "local"},
            )
        )

        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "/clusters/a/nodes/pve1/datastores/local/summary/", status_code=409)
        self.assertContains(response, "/clusters/b/nodes/pve1/datastores/local/summary/", status_code=409)

    def test_recent_tasks_cluster_filter_uses_durable_cluster_key(self):
        for cluster in (self.cluster_a, self.cluster_b):
            AuditEvent.objects.create(
                cluster=cluster,
                cluster_key_snapshot=cluster.key,
                action="guest.power.start",
                object_type="guest",
                object_id="vm:500",
                details={
                    "guest_ref": GuestRef(cluster.key, "vm", 500, "pve1").serialize(),
                    "target_type": "vm",
                    "vmid": 500,
                    "node": "pve1",
                },
            )

        response = self.client.get(reverse("core:recent_tasks"), {"cluster": "b"})

        self.assertEqual(response.status_code, 200)
        tasks = response.json()["tasks"]
        self.assertEqual([task["cluster_key"] for task in tasks], ["b"])

    def test_recent_tasks_cluster_filter_keeps_global_tasks(self):
        ScanRun.objects.create(status=ScanRun.Status.COMPLETED, target_label="All storages")

        response = self.client.get(reverse("core:recent_tasks"), {"cluster": "b"})

        self.assertEqual(response.status_code, 200)
        tasks = response.json()["tasks"]
        self.assertEqual([task["cluster_key"] for task in tasks], [""])

    def test_recent_tasks_reject_unknown_cluster_filter(self):
        response = self.client.get(reverse("core:recent_tasks"), {"cluster": "missing"})

        self.assertEqual(response.status_code, 400)

    def test_guest_detail_sidebar_keeps_guests_from_every_cluster(self):
        self._guest(self.cluster_b, vmid=500, node="pve1", name="b-500")

        response = self.client.get(reverse("core:guest_summary", args=[self.cluster_a.key, "vm", 500]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "a-500")
        self.assertContains(response, "b-500")
        self.assertContains(response, 'class="guest-list-item active"', count=1)


class MulticlusterActivationTests(TestCase):
    def setUp(self):
        RuntimeConfigurationState.objects.create(
            bootstrap_completed=True,
            identity_contract_version=0,
        )
        self.cluster_a = ProxmoxCluster.objects.create(key="a", display_name="Cluster A", enabled=True)
        self.cluster_b = ProxmoxCluster.objects.create(key="b", display_name="Cluster B", enabled=False)

    def test_activation_refuses_enabled_unqualified_scheduled_action(self):
        ScheduledAction.objects.create(
            name="legacy start",
            enabled=True,
            action_type=ScheduledAction.ActionType.START,
            cluster=None,
            target_type="vm",
            target_vmid=500,
        )

        with self.assertRaises(ClusterActivationError):
            activate_multicluster_identity()

        state = RuntimeConfigurationState.objects.get()
        self.assertEqual(state.identity_contract_version, 0)

    def test_activation_allows_enabling_second_cluster(self):
        state = activate_multicluster_identity()
        enable_cluster(self.cluster_b)

        self.assertEqual(state.identity_contract_version, 1)
        self.cluster_b.refresh_from_db()
        self.assertTrue(self.cluster_b.enabled)

    def test_ca_uuid_cannot_be_claimed_by_two_clusters(self):
        self.cluster_a.discovered_ca_uuid = "same-ca"
        self.cluster_a.save(update_fields=["discovered_ca_uuid"])

        with self.assertRaises(IntegrityError), transaction.atomic():
            self.cluster_b.discovered_ca_uuid = "same-ca"
            self.cluster_b.save(update_fields=["discovered_ca_uuid"])

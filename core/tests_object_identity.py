from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from core.models import (
    CurrentGuestInventory,
    ProxmoxCluster,
    ProxmoxEndpoint,
    ScheduledAction,
)
from core.services.audit_events import record_audit_event
from core.services.refs import GuestRef, NodeRef, RefParseError
from core.services.scheduled_actions import _find_guest
from core.views.common import _decorate_guests_with_scheduled_actions
from core.views.guests.operation_lifecycle import guest_ref_from_target_value
from core.views.guests.read_model_support import _resolve_guest_detail


class GuestRefTests(SimpleTestCase):
    def test_versioned_round_trip_preserves_optional_node(self):
        ref = GuestRef("cluster-a", "vm", 500, "pve1")

        self.assertEqual(GuestRef.parse(ref.serialize()), ref)
        self.assertEqual(ref.without_node().serialize(), "gr1:cluster-a:vm:500")
        self.assertEqual(ref.node_ref, NodeRef("cluster-a", "pve1"))

    def test_invalid_or_unknown_references_fail_closed(self):
        for raw in ("", "vm:500", "gr2:cluster-a:vm:500", "gr1:cluster-a:qemu:500"):
            with self.subTest(raw=raw), self.assertRaises(RefParseError):
                GuestRef.parse(raw)


class DurableGuestIdentityTests(TestCase):
    def setUp(self):
        self.cluster_a = ProxmoxCluster.objects.create(key="cluster-a", display_name="Cluster A", enabled=True)
        self.cluster_b = ProxmoxCluster.objects.create(key="cluster-b", display_name="Cluster B", enabled=False)
        self.endpoint_a = ProxmoxEndpoint.objects.create(
            cluster=self.cluster_a,
            name="pve1",
            url="https://a.example.test:8006",
        )
        self.endpoint_b = ProxmoxEndpoint.objects.create(
            cluster=self.cluster_b,
            name="pve1",
            url="https://b.example.test:8006",
        )
        for cluster, endpoint, name in (
            (self.cluster_a, self.endpoint_a, "a-500"),
            (self.cluster_b, self.endpoint_b, "b-500"),
        ):
            CurrentGuestInventory.objects.create(
                cluster=cluster,
                source_endpoint=endpoint,
                node="pve1",
                object_type="vm",
                vmid=500,
                name=name,
                observed_at=timezone.now(),
            )

    def test_lookup_never_falls_back_across_same_vmid(self):
        detail_a = _resolve_guest_detail(GuestRef("cluster-a", "vm", 500))
        detail_b = _resolve_guest_detail(GuestRef("cluster-b", "vm", 500))

        self.assertEqual(detail_a.name, "a-500")
        self.assertEqual(detail_b.name, "b-500")
        self.assertEqual(detail_a.guest_ref.identity_tuple, ("cluster-a", "vm", 500))
        self.assertEqual(detail_b.guest_ref.identity_tuple, ("cluster-b", "vm", 500))

    def test_bulk_target_boundary_preserves_explicit_cluster(self):
        ref = GuestRef("cluster-b", "vm", 500, "pve1")

        self.assertEqual(guest_ref_from_target_value(ref.serialize()), ref)

    def test_scheduled_action_decoration_does_not_cross_same_vmid(self):
        action_a = ScheduledAction.objects.create(
            name="start a-500",
            cluster=self.cluster_a,
            action_type=ScheduledAction.ActionType.START,
            target_type="vm",
            target_vmid=500,
        )
        action_b = ScheduledAction.objects.create(
            name="stop b-500",
            cluster=self.cluster_b,
            action_type=ScheduledAction.ActionType.STOP,
            target_type="vm",
            target_vmid=500,
        )
        guests = list(CurrentGuestInventory.objects.select_related("cluster").order_by("cluster__key"))

        _decorate_guests_with_scheduled_actions(guests)

        self.assertEqual(guests[0].scheduled_actions, [action_a])
        self.assertEqual(guests[1].scheduled_actions, [action_b])
        self.assertIn("gr1%3Acluster-a%3Avm%3A500", guests[0].schedule_action_url)

    def test_audit_persists_relation_snapshot_and_versioned_ref(self):
        ref = GuestRef("cluster-a", "vm", 500, "pve1")

        event = record_audit_event(
            username="system",
            action="guest.power.start",
            object_type="guest",
            cluster=self.cluster_a,
            guest_ref=ref,
        )

        self.assertEqual(event.cluster, self.cluster_a)
        self.assertEqual(event.cluster_key_snapshot, "cluster-a")
        self.assertEqual(event.object_id, "gr1:cluster-a:vm:500")
        self.assertEqual(event.details["guest_ref"], ref.serialize())
        self.assertEqual(event.details["node_ref"], "nr1:cluster-a:pve1")

    def test_scheduled_lookup_stays_inside_actions_cluster(self):
        action = ScheduledAction.objects.create(
            name="start a-500",
            cluster=self.cluster_a,
            action_type=ScheduledAction.ActionType.START,
            target_type="vm",
            target_vmid=500,
        )
        requested_urls = []

        class Client:
            def __init__(self, url):
                requested_urls.append(url)

            def node_names(self, *, fallback):
                return [fallback]

            def guest_current(self, **kwargs):
                return {"status": "stopped", "name": "a-500"}

            def guest_config(self, **kwargs):
                return {"name": "a-500"}

        target = _find_guest(action, client_factory=Client)

        self.assertEqual(target.endpoint, self.endpoint_a)
        self.assertEqual(requested_urls, [self.endpoint_a.url])

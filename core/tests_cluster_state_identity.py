from types import SimpleNamespace

from django.core.cache import cache
from django.test import TestCase

from core.models import ProxmoxCluster
from core.services.cluster_state_identity import (
    cluster_advisory_lock_id,
    cluster_cache_key,
    invalidate_cluster_cache,
)
from core.services.proxmox import (
    LIVE_GUEST_INVENTORY_CACHE_NAMESPACE,
    ProxmoxGuestSummary,
    fetch_live_guest_inventory,
)
from core.services.tag_operation_confirmation import (
    INVALID_CONFIRMATION_ERROR,
    issue_tag_operation_confirmation,
    validate_tag_operation_confirmation,
)
from core.services.tag_registry import cache_registered_tags, registered_tags
from core.services.tags import RegisteredTag


class ClusterStateIdentityTests(TestCase):
    def setUp(self):
        self.a = ProxmoxCluster.objects.create(key="a", display_name="A", enabled=True)
        self.b = ProxmoxCluster.objects.create(key="b", display_name="B", enabled=False)

    def test_same_vmid_and_tag_name_have_independent_cache_state(self):
        guest_a = ProxmoxGuestSummary("pve1", "vm", 500, "a-500", "running")
        guest_b = ProxmoxGuestSummary("pve1", "vm", 500, "b-500", "stopped")
        cache.set(cluster_cache_key(LIVE_GUEST_INVENTORY_CACHE_NAMESPACE, self.a), [guest_a], 60)
        cache.set(cluster_cache_key(LIVE_GUEST_INVENTORY_CACHE_NAMESPACE, self.b), [guest_b], 60)
        cache_registered_tags({"prod": RegisteredTag("prod", "112233", "ffffff")}, cluster=self.a)
        cache_registered_tags({"prod": RegisteredTag("prod", "abcdef", "000000")}, cluster=self.b)

        self.assertEqual(fetch_live_guest_inventory(cluster=self.a)[0].name, "a-500")
        self.assertEqual(fetch_live_guest_inventory(cluster=self.b)[0].name, "b-500")
        self.assertEqual(registered_tags(cluster=self.a)[0]["prod"].background, "112233")
        self.assertEqual(registered_tags(cluster=self.b)[0]["prod"].background, "abcdef")

    def test_generation_invalidation_changes_only_one_clusters_namespace(self):
        old_a = cluster_cache_key("guest-agent-summary:v2", self.a, "pve1", "vm", 500)
        old_b = cluster_cache_key("guest-agent-summary:v2", self.b, "pve1", "vm", 500)

        invalidate_cluster_cache(self.a)
        self.a.refresh_from_db()
        self.b.refresh_from_db()

        self.assertNotEqual(old_a, cluster_cache_key("guest-agent-summary:v2", self.a, "pve1", "vm", 500))
        self.assertEqual(old_b, cluster_cache_key("guest-agent-summary:v2", self.b, "pve1", "vm", 500))

    def test_cluster_operation_lock_ids_are_stable_and_distinct(self):
        base = 0x50564547554501
        self.assertEqual(
            cluster_advisory_lock_id(base, self.a),
            cluster_advisory_lock_id(base, self.a),
        )
        self.assertNotEqual(
            cluster_advisory_lock_id(base, self.a),
            cluster_advisory_lock_id(base, self.b),
        )

    def test_signed_tag_confirmation_cannot_be_replayed_in_another_cluster(self):
        summary = SimpleNamespace(guest_count=0, guests=[], registered=True)
        token = issue_tag_operation_confirmation(
            operation="delete",
            tag="prod",
            summary=summary,
            user_id=7,
            cluster_key=self.a.key,
        )

        confirmation, error = validate_tag_operation_confirmation(
            token,
            operation="delete",
            tag="prod",
            summary=summary,
            user_id=7,
            cluster_key=self.b.key,
        )

        self.assertIsNone(confirmation)
        self.assertEqual(error, INVALID_CONFIRMATION_ERROR)

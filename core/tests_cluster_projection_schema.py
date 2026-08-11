"""Executable acceptance contract for Module 5 phase 5a1A."""

from django.db import IntegrityError, models, transaction
from django.test import TestCase

from core.models import (
    ClusterMembershipState,
    ClusterNodeState,
    ClusterProjectionCoverage,
    ProxmoxCluster,
)
from core.services.cluster_deletion_eligibility import unused_connection_deletion_eligibility
from core.services.cluster_projection import retire_cluster_projection, stamp_cluster_projection_footprint
from core.services.cluster_topology_role import TopologyRole


def _cluster(key: str) -> ProxmoxCluster:
    return ProxmoxCluster.objects.create(key=key, display_name=key, enabled=False)


class ClusterProjectionSchemaTests(TestCase):
    def test_duplicate_node_ref_is_rejected_but_duplicate_node_name_across_clusters_is_valid(self):
        first = _cluster("first")
        second = _cluster("second")
        ClusterNodeState.objects.create(cluster=first, node_name="pve1")
        ClusterNodeState.objects.create(cluster=second, node_name="pve1")

        with self.assertRaises(IntegrityError), transaction.atomic():
            ClusterNodeState.objects.create(cluster=first, node_name="pve1")

    def test_node_state_and_runtime_coverage_reject_unserializable_node_refs(self):
        cluster = _cluster("invalid-node-ref")

        for node_name in ("", "bad:name"):
            with (
                self.subTest(model="node_state", node_name=node_name),
                self.assertRaises(IntegrityError),
                transaction.atomic(),
            ):
                ClusterNodeState.objects.create(cluster=cluster, node_name=node_name)
            with (
                self.subTest(model="runtime_coverage", node_name=node_name),
                self.assertRaises(IntegrityError),
                transaction.atomic(),
            ):
                ClusterProjectionCoverage.objects.create(
                    cluster=cluster,
                    domain=ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME,
                    node_name=node_name,
                    based_on_generation=1,
                )

    def test_cluster_grain_coverage_has_exactly_one_null_identity(self):
        cluster = _cluster("coverage")
        ClusterProjectionCoverage.objects.create(
            cluster=cluster,
            domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP,
            node_name=None,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            ClusterProjectionCoverage.objects.create(
                cluster=cluster,
                domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP,
                node_name=None,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClusterProjectionCoverage.objects.create(
                cluster=cluster,
                domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP,
                node_name="",
            )

    def test_coverage_domain_enforces_its_publication_grain_and_membership_binding(self):
        cluster = _cluster("grain")
        ClusterProjectionCoverage.objects.create(
            cluster=cluster,
            domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP,
        )
        ClusterProjectionCoverage.objects.create(
            cluster=cluster,
            domain=ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME,
            node_name="pve1",
            based_on_generation=7,
        )

        invalid_scopes = (
            {"domain": ClusterProjectionCoverage.DOMAIN_MEMBERSHIP, "node_name": "pve1"},
            {"domain": ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME, "node_name": None},
            {"domain": ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME, "node_name": "pve2"},
            {"domain": "future_unreviewed_domain", "node_name": None},
        )
        for index, scope in enumerate(invalid_scopes):
            with self.subTest(index=index, scope=scope), self.assertRaises(IntegrityError), transaction.atomic():
                ClusterProjectionCoverage.objects.create(cluster=cluster, **scope)

    def test_node_grain_coverage_is_independent_per_exact_node_ref(self):
        first = _cluster("first")
        second = _cluster("second")
        for cluster in (first, second):
            ClusterProjectionCoverage.objects.create(
                cluster=cluster,
                domain=ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME,
                node_name="pve1",
                based_on_generation=1,
            )
        ClusterProjectionCoverage.objects.create(
            cluster=first,
            domain=ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME,
            node_name="pve2",
            based_on_generation=1,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            ClusterProjectionCoverage.objects.create(
                cluster=first,
                domain=ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME,
                node_name="pve1",
                based_on_generation=1,
            )

    def test_discovery_field_names_match_the_enrollment_contract(self):
        field_names = {field.name for field in ClusterNodeState._meta.get_fields()}
        self.assertTrue(
            {
                "node_name",
                "first_discovered_at",
                "last_discovered_at",
                "reported_ring_address",
                "membership_generation",
            }
            <= field_names
        )
        self.assertTrue({"node", "first_seen_at", "last_seen_at"}.isdisjoint(field_names))

    def test_runtime_shape_is_typed_and_unknown_by_default(self):
        state = ClusterNodeState.objects.create(cluster=_cluster("runtime"), node_name="pve1")
        nullable_metrics = (
            "cpu_usage",
            "cpu_wait",
            "cpu_sockets",
            "cpu_cores",
            "memory_total_bytes",
            "memory_used_bytes",
            "swap_total_bytes",
            "swap_used_bytes",
            "rootfs_total_bytes",
            "rootfs_used_bytes",
            "load_average_1m",
            "load_average_5m",
            "load_average_15m",
            "uptime_seconds",
            "secure_boot_enabled",
        )
        for field in nullable_metrics:
            with self.subTest(field=field):
                self.assertIsNone(getattr(state, field))
        self.assertEqual(state.runtime_generation, 0)
        self.assertEqual(state.cpu_model, "")
        self.assertEqual(state.pve_version, "")
        self.assertEqual(state.kernel_version, "")

        expected_types = {
            "runtime_generation": models.PositiveBigIntegerField,
            "cpu_usage": models.FloatField,
            "cpu_wait": models.FloatField,
            "cpu_model": models.CharField,
            "cpu_sockets": models.PositiveSmallIntegerField,
            "cpu_cores": models.PositiveSmallIntegerField,
            "memory_total_bytes": models.PositiveBigIntegerField,
            "memory_used_bytes": models.PositiveBigIntegerField,
            "swap_total_bytes": models.PositiveBigIntegerField,
            "swap_used_bytes": models.PositiveBigIntegerField,
            "rootfs_total_bytes": models.PositiveBigIntegerField,
            "rootfs_used_bytes": models.PositiveBigIntegerField,
            "load_average_1m": models.FloatField,
            "load_average_5m": models.FloatField,
            "load_average_15m": models.FloatField,
            "uptime_seconds": models.PositiveBigIntegerField,
            "pve_version": models.CharField,
            "kernel_version": models.CharField,
            "current_kernel_release": models.CharField,
            "boot_mode": models.CharField,
            "secure_boot_enabled": models.BooleanField,
        }
        for field_name, field_type in expected_types.items():
            with self.subTest(field=field_name):
                self.assertIsInstance(ClusterNodeState._meta.get_field(field_name), field_type)

    def test_unreadable_registered_role_is_unknown_and_cannot_create_a_pending_block(self):
        state = ClusterMembershipState.objects.create(cluster=_cluster("future"), topology_role="corosync-v2")
        self.assertIs(state.role(), TopologyRole.UNKNOWN)
        self.assertFalse(state.role_is_readable)
        field_names = {field.name for field in ClusterMembershipState._meta.get_fields()}
        self.assertNotIn("transition_pending", field_names)
        self.assertNotIn("pending_topology_role", field_names)


class ClusterProjectionLifecycleTests(TestCase):
    def test_owner_finalizer_removes_all_mutable_projection_rows(self):
        cluster = _cluster("retiring")
        ClusterMembershipState.objects.create(cluster=cluster)
        ClusterNodeState.objects.create(cluster=cluster, node_name="pve1")
        ClusterProjectionCoverage.objects.create(
            cluster=cluster,
            domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP,
        )

        result = retire_cluster_projection(cluster)

        self.assertEqual(result.membership_rows_deleted, 1)
        self.assertEqual(result.node_rows_deleted, 1)
        self.assertEqual(result.coverage_rows_deleted, 1)
        self.assertFalse(ClusterMembershipState.objects.filter(cluster=cluster).exists())
        self.assertFalse(ClusterNodeState.objects.filter(cluster=cluster).exists())
        self.assertFalse(ClusterProjectionCoverage.objects.filter(cluster=cluster).exists())

    def test_projection_footprint_is_declared_reconstructible(self):
        cluster = _cluster("footprint")
        self.assertTrue(stamp_cluster_projection_footprint(cluster))
        cluster.refresh_from_db()
        self.assertEqual(cluster.operational_footprint_reason, "host_projection")
        self.assertTrue(unused_connection_deletion_eligibility(cluster).eligible)

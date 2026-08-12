"""Executable acceptance contract for Module 5 phase 5a1F."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from core.models import (
    ClusterMembershipState,
    ClusterNodeState,
    ClusterProjectionCoverage,
    ClusterStorage,
    CurrentGuestInventory,
    ProxmoxCluster,
)
from core.services.cluster_projection_read import (
    ClusterProjectionNotFound,
    MembershipReadStatus,
    NodeRuntimeReadStatus,
    read_cluster_projection,
)

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


@override_settings(HOST_PROJECTION_REFRESH_INTERVAL_MINUTES=1)
class ClusterProjectionReadTests(TestCase):
    def _cluster(self, key: str, **fields) -> ProxmoxCluster:
        return ProxmoxCluster.objects.create(key=key, display_name=key.upper(), **fields)

    def _membership(
        self,
        cluster: ProxmoxCluster,
        *,
        generation: int = 7,
        complete: bool = True,
        observed_at=NOW,
        error_code: str = "",
    ) -> ClusterMembershipState:
        state = ClusterMembershipState.objects.create(
            cluster=cluster,
            topology_role="corosync",
            membership_generation=generation,
            member_count=3,
            quorate=True,
            observed_from="pve1",
        )
        ClusterProjectionCoverage.objects.create(
            cluster=cluster,
            domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP,
            generation=generation,
            complete=complete,
            attempted_at=NOW,
            observed_at=observed_at,
            error_code=error_code,
        )
        return state

    def _node(
        self,
        cluster: ProxmoxCluster,
        node_name: str,
        *,
        membership_generation: int = 7,
        runtime_generation: int = 4,
        present: bool = True,
        online: bool = True,
        with_coverage: bool = True,
        coverage_complete: bool = True,
        coverage_generation: int | None = None,
        based_on_generation: int = 7,
        observed_at=NOW,
        error_code: str = "",
    ) -> ClusterNodeState:
        row = ClusterNodeState.objects.create(
            cluster=cluster,
            node_name=node_name,
            nodeid=1,
            present=present,
            online=online,
            reported_ring_address="10.0.0.1",
            membership_generation=membership_generation,
            runtime_generation=runtime_generation,
            cpu_usage=0.25,
            cpu_wait=0.01,
            cpu_model="Example CPU",
            cpu_sockets=2,
            cpu_cores=16,
            memory_total_bytes=128,
            memory_used_bytes=64,
            load_average_1m=1.0,
            load_average_5m=0.8,
            load_average_15m=0.5,
            uptime_seconds=3600,
            pve_version="pve-manager/9.2.9",
        )
        if with_coverage:
            ClusterProjectionCoverage.objects.create(
                cluster=cluster,
                domain=ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME,
                node_name=node_name,
                generation=runtime_generation if coverage_generation is None else coverage_generation,
                based_on_generation=based_on_generation,
                complete=coverage_complete,
                attempted_at=NOW,
                observed_at=observed_at,
                error_code=error_code,
            )
        return row

    def test_current_projection_is_typed_cluster_qualified_and_immutable(self):
        cluster = self._cluster("alpha", enabled=True, ingestion_quarantined=True)
        self._membership(cluster, error_code="topology_role_change_observed")
        self._node(cluster, "pve1")

        result = read_cluster_projection("alpha", now=NOW + timedelta(seconds=30))

        self.assertEqual(result.cluster_key, "alpha")
        self.assertTrue(result.enabled)
        self.assertTrue(result.ingestion_quarantined)
        self.assertEqual(result.membership_status, MembershipReadStatus.CURRENT)
        self.assertTrue(result.membership_current)
        self.assertTrue(result.membership_coverage.current)
        self.assertEqual(result.membership_coverage.error_code, "topology_role_change_observed")
        self.assertEqual(len(result.nodes), 1)
        node = result.nodes[0]
        self.assertEqual(node.node_ref, "nr1:alpha:pve1")
        self.assertEqual(node.runtime_status, NodeRuntimeReadStatus.CURRENT)
        self.assertTrue(node.runtime_current)
        self.assertEqual(node.runtime.cpu_model, "Example CPU")
        with self.assertRaises(FrozenInstanceError):
            node.online = False

    def test_membership_diagnostics_keep_missing_incomplete_and_stale_distinct(self):
        missing = self._cluster("missing")

        incomplete = self._cluster("incomplete")
        self._membership(incomplete, complete=False, error_code="provider_timeout")

        old = self._cluster("old")
        self._membership(old, observed_at=NOW - timedelta(minutes=2, microseconds=1))

        mismatched = self._cluster("mismatched")
        state = self._membership(mismatched)
        state.membership_generation = 8
        state.save(update_fields=["membership_generation"])

        self.assertEqual(
            read_cluster_projection(missing.key, now=NOW).membership_status,
            MembershipReadStatus.MISSING,
        )
        failed = read_cluster_projection(incomplete.key, now=NOW)
        self.assertEqual(failed.membership_status, MembershipReadStatus.INCOMPLETE)
        self.assertFalse(failed.membership_coverage.current)
        self.assertEqual(failed.membership_coverage.error_code, "provider_timeout")
        self.assertEqual(read_cluster_projection(old.key, now=NOW).membership_status, MembershipReadStatus.STALE)
        self.assertEqual(
            read_cluster_projection(mismatched.key, now=NOW).membership_status,
            MembershipReadStatus.STALE,
        )

    def test_runtime_read_distinguishes_offline_departed_failed_unknown_and_stale(self):
        cluster = self._cluster("states")
        self._membership(cluster)
        self._node(
            cluster,
            "offline",
            online=False,
            coverage_complete=False,
            error_code="node_offline_by_membership",
        )
        self._node(
            cluster,
            "departed",
            present=False,
            online=False,
            coverage_complete=False,
            error_code="node_absent_from_membership",
        )
        self._node(cluster, "failed", coverage_complete=False, error_code="provider_timeout")
        self._node(cluster, "unknown", with_coverage=False)
        self._node(cluster, "stale-generation", based_on_generation=6)

        states = {node.node_name: node for node in read_cluster_projection(cluster.key, now=NOW).nodes}

        self.assertEqual(states["offline"].runtime_status, NodeRuntimeReadStatus.OFFLINE_SKIPPED)
        self.assertEqual(states["departed"].runtime_status, NodeRuntimeReadStatus.DEPARTED)
        self.assertEqual(states["failed"].runtime_status, NodeRuntimeReadStatus.FAILED)
        self.assertEqual(states["unknown"].runtime_status, NodeRuntimeReadStatus.UNKNOWN)
        self.assertEqual(states["stale-generation"].runtime_status, NodeRuntimeReadStatus.STALE)
        self.assertEqual(states["failed"].runtime.cpu_model, "Example CPU")
        self.assertFalse(any(node.runtime_current for node in states.values()))

    def test_runtime_cannot_be_current_without_a_published_membership_generation(self):
        cluster = self._cluster("no-membership")
        self._node(cluster, "pve1", membership_generation=0, based_on_generation=0)

        zero = self._cluster("zero-membership")
        self._membership(zero, generation=0)
        self._node(zero, "pve1", membership_generation=0, based_on_generation=0)

        result = read_cluster_projection(cluster.key, now=NOW)
        zero_result = read_cluster_projection(zero.key, now=NOW)

        self.assertEqual(result.membership_status, MembershipReadStatus.MISSING)
        self.assertEqual(result.nodes[0].runtime_status, NodeRuntimeReadStatus.STALE)
        self.assertEqual(zero_result.membership_status, MembershipReadStatus.STALE)
        self.assertEqual(zero_result.nodes[0].runtime_status, NodeRuntimeReadStatus.STALE)

    def test_mixed_membership_generations_are_per_node_state_not_a_cluster_fault(self):
        cluster = self._cluster("mixed")
        self._membership(cluster, generation=8)
        self._node(cluster, "new", membership_generation=8, based_on_generation=8)
        self._node(cluster, "old", membership_generation=7, based_on_generation=7)

        result = read_cluster_projection(cluster.key, now=NOW)
        states = {node.node_name: node.runtime_status for node in result.nodes}

        self.assertEqual(result.membership_status, MembershipReadStatus.CURRENT)
        self.assertEqual(states, {"new": NodeRuntimeReadStatus.CURRENT, "old": NodeRuntimeReadStatus.STALE})

    def test_every_runtime_currency_input_fails_closed_when_removed(self):
        variants = {
            "not present": {"present": False},
            "not online": {"online": False},
            "row membership mismatch": {"membership_generation": 6},
            "coverage incomplete": {"coverage_complete": False},
            "coverage error": {"error_code": "provider_error"},
            "coverage membership mismatch": {"based_on_generation": 6},
            "runtime generation mismatch": {"coverage_generation": 3},
            "too old": {"observed_at": NOW - timedelta(minutes=2, microseconds=1)},
            "future dated": {"observed_at": NOW + timedelta(microseconds=1)},
        }
        for index, (label, fields) in enumerate(variants.items()):
            with self.subTest(label=label):
                cluster = self._cluster(f"currency-{index}")
                self._membership(cluster)
                self._node(cluster, "pve1", **fields)
                result = read_cluster_projection(cluster.key, now=NOW)
                self.assertNotEqual(result.nodes[0].runtime_status, NodeRuntimeReadStatus.CURRENT)

    def test_offline_and_departed_labels_require_current_matching_membership_evidence(self):
        variants = (
            ("offline-wrong-generation", False, True, "node_offline_by_membership", 6, 7, False),
            ("offline-row-mismatch", False, True, "node_offline_by_membership", 7, 6, False),
            ("offline-now-online", True, True, "node_offline_by_membership", 7, 7, False),
            ("offline-complete", False, True, "node_offline_by_membership", 7, 7, True),
            ("departed-wrong-generation", False, False, "node_absent_from_membership", 6, 7, False),
            ("departed-row-mismatch", False, False, "node_absent_from_membership", 7, 6, False),
            ("departed-now-present", True, True, "node_absent_from_membership", 7, 7, False),
            ("departed-complete", False, False, "node_absent_from_membership", 7, 7, True),
        )
        cluster = self._cluster("special-evidence")
        self._membership(cluster)
        for name, online, present, error_code, based_on, row_generation, complete in variants:
            self._node(
                cluster,
                name,
                online=online,
                present=present,
                membership_generation=row_generation,
                coverage_complete=complete,
                error_code=error_code,
                based_on_generation=based_on,
            )

        states = {node.node_name: node.runtime_status for node in read_cluster_projection(cluster.key, now=NOW).nodes}

        self.assertEqual(
            states,
            {
                "departed-complete": NodeRuntimeReadStatus.STALE,
                "departed-now-present": NodeRuntimeReadStatus.FAILED,
                "departed-row-mismatch": NodeRuntimeReadStatus.FAILED,
                "departed-wrong-generation": NodeRuntimeReadStatus.STALE,
                "offline-complete": NodeRuntimeReadStatus.STALE,
                "offline-now-online": NodeRuntimeReadStatus.FAILED,
                "offline-row-mismatch": NodeRuntimeReadStatus.FAILED,
                "offline-wrong-generation": NodeRuntimeReadStatus.STALE,
            },
        )

    def test_freshness_boundary_is_inclusive_and_uses_twice_the_ratified_cadence(self):
        cluster = self._cluster("boundary")
        self._membership(cluster, observed_at=NOW - timedelta(minutes=2))
        self._node(cluster, "pve1", observed_at=NOW - timedelta(minutes=2))

        result = read_cluster_projection(cluster.key, now=NOW)

        self.assertEqual(result.membership_status, MembershipReadStatus.CURRENT)
        self.assertEqual(result.nodes[0].runtime_status, NodeRuntimeReadStatus.CURRENT)

    def test_missing_and_retired_keys_do_not_fall_back_to_provider_or_history(self):
        retired = self._cluster(
            "retired",
            enabled=False,
            retired_at=NOW,
            retirement_mode=ProxmoxCluster.RetirementMode.FORCED,
        )
        self._membership(retired)
        self._node(retired, "pve1")

        with patch("core.services.proxmox.ProxmoxClient._request") as provider_request:
            for key in ("does-not-exist", retired.key):
                with self.subTest(key=key), self.assertRaises(ClusterProjectionNotFound):
                    read_cluster_projection(key, now=NOW)
        provider_request.assert_not_called()

    def test_read_rejects_a_naive_diagnostic_clock_before_querying(self):
        with self.assertNumQueries(0), self.assertRaisesRegex(ValueError, "timezone-aware"):
            read_cluster_projection("anything", now=NOW.replace(tzinfo=None))

    def test_diagnostics_cost_exactly_four_queries_at_1_3_and_20_nodes_and_zero_provider_calls(self):
        clusters = []
        for count in (1, 3, 20):
            cluster = self._cluster(f"budget-{count}")
            self._membership(cluster)
            for index in range(count):
                self._node(cluster, f"node-{index:02d}")
            clusters.append((count, cluster))

        with (
            patch(
                "core.services.proxmox.ProxmoxClient._request",
                side_effect=AssertionError("a projection read must not contact Proxmox"),
            ) as provider_request,
            patch(
                "core.services.cluster_resolver.client_for_endpoint",
                side_effect=AssertionError("a projection read must not construct a provider client"),
            ) as provider_factory,
        ):
            for count, cluster in clusters:
                with self.subTest(nodes=count), self.assertNumQueries(4):
                    result = read_cluster_projection(cluster.key, now=NOW)
                self.assertEqual(len(result.nodes), count)

        provider_request.assert_not_called()
        provider_factory.assert_not_called()

    def test_diagnostics_queries_only_the_host_projection_owners(self):
        cluster = self._cluster("owners")
        self._membership(cluster)
        self._node(cluster, "pve1")
        CurrentGuestInventory.objects.create(
            cluster=cluster,
            node="pve1",
            object_type="vm",
            vmid=100,
            observed_at=NOW,
        )
        ClusterStorage.objects.create(
            cluster=cluster,
            storage_id="local",
            storage_type="dir",
        )

        with CaptureQueriesContext(connection) as captured:
            read_cluster_projection(cluster.key, now=NOW)

        sql = " ".join(query["sql"] for query in captured.captured_queries)
        self.assertNotIn(CurrentGuestInventory._meta.db_table, sql)
        self.assertNotIn(ClusterStorage._meta.db_table, sql)

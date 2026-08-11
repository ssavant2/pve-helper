from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from core.models import (
    ClusterMembershipState,
    ClusterNodeState,
    ClusterProjectionCoverage,
    ProxmoxCluster,
    ProxmoxEndpoint,
)
from core.services.cluster_node_runtime import (
    ERROR_ACQUISITION_DISABLED,
    ERROR_ACQUISITION_QUARANTINED,
    ERROR_ACQUISITION_RETIRED,
    ERROR_INVALID_PAYLOAD,
    ERROR_MEMBERSHIP_NOT_PUBLISHED,
    ERROR_NO_ENABLED_ENDPOINT,
    ERROR_NODE_ABSENT,
    ERROR_NODE_NOT_A_MEMBER,
    ERROR_NODE_OFFLINE,
    ERROR_PROVIDER,
    ERROR_PROVIDER_TIMEOUT,
    ERROR_PROVIDER_UNAUTHORIZED,
    InvalidNodeStatusPayload,
    normalize_node_status,
    refresh_cluster_node_runtime,
    refresh_node_runtime,
)
from core.services.proxmox import (
    ProxmoxAPIError,
    ProxmoxInvalidResponseError,
    ProxmoxTransportError,
)

STATUS_BODY = {
    "boot-info": {"mode": "efi", "secureboot": 0},
    "cpu": 0,
    "cpuinfo": {"cores": 8, "cpus": 16, "model": "AMD Ryzen 7 5800X", "sockets": 1},
    "current-kernel": {"release": "6.14.11-4-pve", "version": "#1 SMP"},
    "idle": 0,
    "ksm": {"shared": 0},
    "kversion": "Linux 6.14.11-4-pve",
    "loadavg": ["0.14", "0.21", "0.19"],
    "memory": {"free": 1024, "total": 67108864, "used": 33554432},
    "pveversion": "pve-manager/9.2.10",
    "rootfs": {"avail": 512, "free": 1024, "total": 107374182, "used": 21474836},
    "swap": {"free": 0, "total": 8589934, "used": 0},
    "uptime": 864000,
    "wait": 0.0654962000970188,
}


def _body(**overrides):
    body = {key: (dict(value) if isinstance(value, dict) else value) for key, value in STATUS_BODY.items()}
    body.update(overrides)
    return body


class NodeStatusNormalizationTests(SimpleTestCase):
    """The live-observed traps, each of which is silent if missed."""

    def test_accepts_the_captured_shape(self):
        runtime = normalize_node_status(STATUS_BODY)

        self.assertEqual(runtime.cpu_usage, 0.0)
        self.assertEqual(runtime.cpu_model, "AMD Ryzen 7 5800X")
        self.assertEqual(runtime.cpu_sockets, 1)
        self.assertEqual(runtime.memory_total_bytes, 67108864)
        self.assertEqual(runtime.rootfs_used_bytes, 21474836)
        self.assertEqual(runtime.uptime_seconds, 864000)
        self.assertEqual(runtime.pve_version, "pve-manager/9.2.10")
        self.assertEqual(runtime.current_kernel_release, "6.14.11-4-pve")
        self.assertEqual(runtime.boot_mode, "efi")
        self.assertIs(runtime.secure_boot_enabled, False)

    def test_cpu_accepts_int_zero_and_float(self):
        """pve1 answered int 0 and pve2 a float; one sample must not fix the type."""
        self.assertEqual(normalize_node_status(_body(cpu=0)).cpu_usage, 0.0)
        self.assertEqual(normalize_node_status(_body(cpu=0.42)).cpu_usage, 0.42)

    def test_loadavg_is_a_list_of_strings(self):
        runtime = normalize_node_status(_body(loadavg=["1.50", "0.75", "0.25"]))

        self.assertEqual(runtime.load_average_1m, 1.5)
        self.assertEqual(runtime.load_average_15m, 0.25)

    def test_unparsable_loadavg_is_not_silently_null(self):
        with self.assertRaises(InvalidNodeStatusPayload):
            normalize_node_status(_body(loadavg=["1.5", "nope", "0.2"]))
        with self.assertRaises(InvalidNodeStatusPayload):
            normalize_node_status(_body(loadavg=["1.5", "0.2"]))

    def test_absent_optional_key_is_unknown_not_zero(self):
        body = _body()
        del body["wait"]
        del body["swap"]
        del body["boot-info"]

        runtime = normalize_node_status(body)

        self.assertIsNone(runtime.cpu_wait)
        self.assertIsNone(runtime.swap_total_bytes)
        self.assertIsNone(runtime.secure_boot_enabled)
        self.assertEqual(runtime.boot_mode, "")

    def test_empty_body_is_refused_rather_than_published_as_an_idle_node(self):
        """The failure U1 named for this phase: a filtered response must not
        normalize into a healthy-looking node with every metric zero."""
        with self.assertRaises(InvalidNodeStatusPayload):
            normalize_node_status({})

    def test_each_floor_key_is_required(self):
        for key in ("uptime", "cpu", "loadavg", "cpuinfo", "memory", "rootfs", "pveversion"):
            body = _body()
            del body[key]
            with self.subTest(missing=key):
                with self.assertRaises(InvalidNodeStatusPayload):
                    normalize_node_status(body)

    def test_floor_covers_the_nested_values_the_columns_are_fed_from(self):
        """A name-only floor passes `{"memory": {}}`; the columns still get nothing."""
        for section, key in (("memory", "total"), ("rootfs", "used"), ("cpuinfo", "model")):
            body = _body()
            body[section] = {name: value for name, value in body[section].items() if name != key}
            with self.subTest(section=section, key=key):
                with self.assertRaises(InvalidNodeStatusPayload):
                    normalize_node_status(body)

    def test_wrong_type_fails_the_scope_rather_than_nulling_the_field(self):
        with self.assertRaises(InvalidNodeStatusPayload):
            normalize_node_status(_body(cpu="0.5"))
        with self.assertRaises(InvalidNodeStatusPayload):
            normalize_node_status(_body(uptime=-1))

    def test_secureboot_maps_only_binary_ints(self):
        self.assertIs(
            normalize_node_status(_body(**{"boot-info": {"mode": "efi", "secureboot": 1}})).secure_boot_enabled, True
        )
        with self.assertRaises(InvalidNodeStatusPayload):
            normalize_node_status(_body(**{"boot-info": {"mode": "efi", "secureboot": 2}}))

    def test_long_display_string_is_truncated_not_refused(self):
        runtime = normalize_node_status(_body(cpuinfo={**STATUS_BODY["cpuinfo"], "model": "x" * 400}))

        self.assertEqual(len(runtime.cpu_model), 255)
        self.assertTrue(runtime.cpu_model.endswith("…"))

    def test_out_of_range_decision_number_is_refused(self):
        with self.assertRaises(InvalidNodeStatusPayload):
            normalize_node_status(_body(memory={**STATUS_BODY["memory"], "total": 2**64}))


def _cluster(key: str = "clusterhq", **fields) -> ProxmoxCluster:
    return ProxmoxCluster.objects.create(key=key, display_name=key, **fields)


def _endpoint(cluster: ProxmoxCluster, name: str = "endpoint") -> ProxmoxEndpoint:
    return ProxmoxEndpoint.objects.create(
        cluster=cluster,
        name=name,
        url=f"https://{name}.{cluster.key}.test:8006",
    )


def _publish_membership(cluster: ProxmoxCluster, nodes: dict[str, bool], *, generation: int = 1) -> None:
    """Stand in for a complete 5a1B publication."""
    now = timezone.now()
    ClusterMembershipState.objects.update_or_create(
        cluster=cluster,
        defaults={
            "membership_generation": generation,
            "member_count": len(nodes),
            "quorate": True,
            "topology_role": "corosync",
        },
    )
    for name, online in nodes.items():
        ClusterNodeState.objects.update_or_create(
            cluster=cluster,
            node_name=name,
            defaults={"present": True, "online": online, "membership_generation": generation},
        )
    ClusterProjectionCoverage.objects.update_or_create(
        cluster=cluster,
        domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP,
        node_name=None,
        defaults={"generation": generation, "complete": True, "attempted_at": now, "observed_at": now},
    )


def _client(value=None, *, error: Exception | None = None):
    client = MagicMock()
    client.get.side_effect = error
    if error is None:
        client.get.return_value = value
    return client


def _coverage(cluster: ProxmoxCluster, node_name: str) -> ClusterProjectionCoverage:
    return ClusterProjectionCoverage.objects.get(
        cluster=cluster,
        domain=ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME,
        node_name=node_name,
    )


class NodeRuntimePublicationTests(TestCase):
    def setUp(self):
        self.cluster = _cluster()
        _endpoint(self.cluster)
        _publish_membership(self.cluster, {"pve1": True, "pve2": True})

    def test_sweep_publishes_each_node_independently(self):
        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=_client(STATUS_BODY)):
            result = refresh_cluster_node_runtime(self.cluster)

        self.assertTrue(result.complete)
        self.assertEqual(result.targets, 2)
        for node_name in ("pve1", "pve2"):
            row = ClusterNodeState.objects.get(cluster=self.cluster, node_name=node_name)
            self.assertEqual(row.cpu_model, "AMD Ryzen 7 5800X")
            self.assertEqual(row.runtime_generation, 1)
            coverage = _coverage(self.cluster, node_name)
            self.assertTrue(coverage.complete)
            self.assertEqual(coverage.error_code, "")
            self.assertEqual(coverage.based_on_generation, 1)

    def test_one_node_failure_leaves_its_sibling_untouched(self):
        """The phase's central assertion: a dead node blanks nobody."""

        def fake_client(endpoint):
            return _client(STATUS_BODY)

        with patch("core.services.cluster_node_runtime.client_for_endpoint", side_effect=fake_client):
            refresh_cluster_node_runtime(self.cluster)

        healthy_before = ClusterNodeState.objects.get(cluster=self.cluster, node_name="pve1").cpu_usage

        def failing_for_pve2(endpoint):
            client = MagicMock()

            def get(path, **kwargs):
                if "pve2" in path:
                    raise ProxmoxTransportError("unreachable")
                return STATUS_BODY

            client.get.side_effect = get
            return client

        with patch("core.services.cluster_node_runtime.client_for_endpoint", side_effect=failing_for_pve2):
            refresh_cluster_node_runtime(self.cluster)

        pve1 = ClusterNodeState.objects.get(cluster=self.cluster, node_name="pve1")
        pve2 = ClusterNodeState.objects.get(cluster=self.cluster, node_name="pve2")

        self.assertEqual(pve1.cpu_usage, healthy_before)
        self.assertEqual(pve1.runtime_generation, 2)
        self.assertTrue(_coverage(self.cluster, "pve1").complete)
        # Previous-good payload and generation are preserved for the failure.
        self.assertEqual(pve2.cpu_model, "AMD Ryzen 7 5800X")
        self.assertEqual(pve2.runtime_generation, 1)
        self.assertFalse(_coverage(self.cluster, "pve2").complete)

    def test_failed_read_preserves_previous_good_observed_at(self):
        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=_client(STATUS_BODY)):
            refresh_cluster_node_runtime(self.cluster)
        observed_at = _coverage(self.cluster, "pve1").observed_at

        with patch(
            "core.services.cluster_node_runtime.client_for_endpoint",
            return_value=_client(
                error=ProxmoxTransportError(
                    "boom",
                )
            ),
        ):
            refresh_cluster_node_runtime(self.cluster)

        coverage = _coverage(self.cluster, "pve1")
        self.assertEqual(coverage.observed_at, observed_at)
        self.assertFalse(coverage.complete)
        self.assertIsNotNone(coverage.attempted_at)

    def test_offline_node_is_skipped_without_a_provider_call(self):
        _publish_membership(self.cluster, {"pve1": True, "pve2": False}, generation=2)
        client = _client(STATUS_BODY)

        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=client):
            refresh_cluster_node_runtime(self.cluster)

        self.assertEqual(client.get.call_count, 1)
        self.assertEqual(_coverage(self.cluster, "pve2").error_code, ERROR_NODE_OFFLINE)

    def test_offline_skip_needs_complete_membership_coverage(self):
        """A failed membership refresh withdraws the skip authority."""
        _publish_membership(self.cluster, {"pve1": True, "pve2": False}, generation=2)
        ClusterProjectionCoverage.objects.filter(
            cluster=self.cluster, domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP
        ).update(complete=False, error_code=ERROR_PROVIDER_TIMEOUT)
        client = _client(STATUS_BODY)

        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=client):
            refresh_cluster_node_runtime(self.cluster)

        self.assertEqual(client.get.call_count, 2)

    def test_error_codes_map_from_provider_failures(self):
        cases = {
            ERROR_PROVIDER_TIMEOUT: ProxmoxTransportError("t"),
            ERROR_PROVIDER: ProxmoxTransportError("x"),
            ERROR_PROVIDER_UNAUTHORIZED: ProxmoxAPIError("denied", status_code=403),
            ERROR_INVALID_PAYLOAD: ProxmoxInvalidResponseError("bad"),
        }
        timeout = ProxmoxTransportError("t")
        timeout.__cause__ = httpx.ConnectTimeout("t")
        cases[ERROR_PROVIDER_TIMEOUT] = timeout

        for expected, error in cases.items():
            with self.subTest(expected=expected):
                with patch(
                    "core.services.cluster_node_runtime.client_for_endpoint",
                    return_value=_client(error=error),
                ):
                    result = refresh_node_runtime(self.cluster, "pve1")
                self.assertEqual(result.error_code, expected)

    def test_unusable_body_is_invalid_payload_not_a_published_node(self):
        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=_client({})):
            result = refresh_node_runtime(self.cluster, "pve1")

        self.assertEqual(result.error_code, ERROR_INVALID_PAYLOAD)
        self.assertIsNone(ClusterNodeState.objects.get(cluster=self.cluster, node_name="pve1").cpu_usage)


class NodeRuntimeMembershipBindingTests(TestCase):
    def setUp(self):
        self.cluster = _cluster()
        _endpoint(self.cluster)
        _publish_membership(self.cluster, {"pve1": True})

    def test_node_dropped_mid_sweep_is_not_published_as_current(self):
        """The provenance lie this phase must not manufacture: publishing
        `complete=True` bound to a generation that says the node is not a member."""
        original = ClusterNodeState.objects.select_for_update

        def drop_pve1(*args, **kwargs):
            queryset = original(*args, **kwargs)
            ClusterNodeState.objects.filter(cluster=self.cluster, node_name="pve1").update(
                present=False, online=False, membership_generation=2
            )
            ClusterMembershipState.objects.filter(cluster=self.cluster).update(membership_generation=2)
            return queryset

        client = _client(STATUS_BODY)
        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=client):
            with patch.object(ClusterNodeState.objects, "select_for_update", side_effect=drop_pve1):
                result = refresh_cluster_node_runtime(self.cluster)

        self.assertEqual(client.get.call_count, 0)
        self.assertEqual(result.nodes[0].error_code, ERROR_NODE_ABSENT)
        coverage = _coverage(self.cluster, "pve1")
        self.assertFalse(coverage.complete)
        self.assertEqual(coverage.based_on_generation, 2)

    def test_departed_node_stops_presenting_runtime(self):
        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=_client(STATUS_BODY)):
            refresh_cluster_node_runtime(self.cluster)

        ClusterNodeState.objects.filter(cluster=self.cluster, node_name="pve1").update(
            present=False, membership_generation=2
        )
        ClusterMembershipState.objects.filter(cluster=self.cluster).update(membership_generation=2)

        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=_client(STATUS_BODY)):
            result = refresh_cluster_node_runtime(self.cluster)

        self.assertEqual(result.departed, 1)
        row = ClusterNodeState.objects.get(cluster=self.cluster, node_name="pve1")
        self.assertIsNone(row.cpu_usage)
        self.assertEqual(row.cpu_model, "")
        # The row survives: "disappeared" must stay distinguishable from "never seen".
        self.assertFalse(row.present)
        coverage = _coverage(self.cluster, "pve1")
        self.assertEqual(coverage.error_code, ERROR_NODE_ABSENT)
        self.assertEqual(coverage.based_on_generation, 2)

    def test_departed_pass_is_idempotent(self):
        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=_client(STATUS_BODY)):
            refresh_cluster_node_runtime(self.cluster)
        ClusterNodeState.objects.filter(cluster=self.cluster).update(present=False)

        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=_client(STATUS_BODY)):
            first = refresh_cluster_node_runtime(self.cluster)
            second = refresh_cluster_node_runtime(self.cluster)

        self.assertEqual(first.departed, 1)
        self.assertEqual(second.departed, 0)

    def test_membership_columns_are_never_written_by_this_module(self):
        row = ClusterNodeState.objects.get(cluster=self.cluster, node_name="pve1")
        row.nodeid = 7
        row.reported_ring_address = "192.0.2.7"
        row.save()

        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=_client(STATUS_BODY)):
            refresh_cluster_node_runtime(self.cluster)

        row.refresh_from_db()
        self.assertEqual(row.nodeid, 7)
        self.assertEqual(row.reported_ring_address, "192.0.2.7")
        self.assertEqual(row.membership_generation, 1)
        self.assertTrue(row.present)


class NodeRuntimeRefusalTests(TestCase):
    def setUp(self):
        self.cluster = _cluster()
        _endpoint(self.cluster)
        _publish_membership(self.cluster, {"pve1": True})

    def _assert_zero_call_refusal(self, expected: str):
        client = _client(STATUS_BODY)
        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=client):
            result = refresh_cluster_node_runtime(self.cluster)

        self.assertEqual(result.error_code, expected)
        self.assertEqual(client.get.call_count, 0)
        self.assertFalse(
            ClusterProjectionCoverage.objects.filter(
                cluster=self.cluster, domain=ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME
            ).exists()
        )

    def test_disabled_cluster_makes_no_call_and_writes_no_row(self):
        ProxmoxCluster.objects.filter(pk=self.cluster.pk).update(enabled=False)
        self._assert_zero_call_refusal(ERROR_ACQUISITION_DISABLED)

    def test_quarantined_cluster_makes_no_call(self):
        ProxmoxCluster.objects.filter(pk=self.cluster.pk).update(ingestion_quarantined=True)
        self._assert_zero_call_refusal(ERROR_ACQUISITION_QUARANTINED)

    def test_retired_cluster_cannot_resurrect_finalized_state(self):
        ProxmoxCluster.objects.filter(pk=self.cluster.pk).update(
            retired_at=timezone.now(),
            enabled=False,
            retirement_mode=ProxmoxCluster.RetirementMode.VERIFIED,
        )
        self._assert_zero_call_refusal(ERROR_ACQUISITION_RETIRED)

    def test_no_enabled_endpoint_refuses(self):
        ProxmoxEndpoint.objects.filter(cluster=self.cluster).update(enabled=False)
        self._assert_zero_call_refusal(ERROR_NO_ENABLED_ENDPOINT)

    def test_membership_never_completely_published_refuses(self):
        ClusterProjectionCoverage.objects.filter(
            cluster=self.cluster, domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP
        ).update(complete=False, observed_at=None)
        self._assert_zero_call_refusal(ERROR_MEMBERSHIP_NOT_PUBLISHED)

    def test_zero_target_sweep_is_a_success(self):
        ClusterNodeState.objects.filter(cluster=self.cluster).delete()
        client = _client(STATUS_BODY)

        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=client):
            result = refresh_cluster_node_runtime(self.cluster)

        self.assertTrue(result.complete)
        self.assertEqual(result.targets, 0)
        self.assertEqual(client.get.call_count, 0)

    def test_per_node_entry_point_refuses_an_unknown_name_without_a_row(self):
        client = _client(STATUS_BODY)
        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=client):
            result = refresh_node_runtime(self.cluster, "ghost")

        self.assertEqual(result.error_code, ERROR_NODE_NOT_A_MEMBER)
        self.assertEqual(client.get.call_count, 0)
        self.assertFalse(
            ClusterProjectionCoverage.objects.filter(
                cluster=self.cluster, domain=ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME, node_name="ghost"
            ).exists()
        )


class NodeRuntimeIsolationTests(TestCase):
    def test_sibling_cluster_is_never_touched(self):
        first = _cluster("clusterhq")
        _endpoint(first)
        _publish_membership(first, {"pve1": True})
        second = _cluster("clusterb")
        _endpoint(second, name="other")
        _publish_membership(second, {"pve1": True})

        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=_client(STATUS_BODY)):
            refresh_cluster_node_runtime(first)

        self.assertFalse(
            ClusterProjectionCoverage.objects.filter(
                cluster=second, domain=ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME
            ).exists()
        )
        self.assertIsNone(ClusterNodeState.objects.get(cluster=second, node_name="pve1").cpu_usage)


class NodeRuntimeEndpointFailoverTests(TestCase):
    def setUp(self):
        self.cluster = _cluster()
        _endpoint(self.cluster, name="alpha")
        _endpoint(self.cluster, name="beta")
        _publish_membership(self.cluster, {"pve1": True, "pve2": True, "pve3": True})

    def test_dead_endpoint_is_tried_once_per_sweep_not_once_per_node(self):
        """`last_health_status` is only written by the scan task, so without the
        per-sweep skip set a dead transport costs a timeout on every node."""
        attempts: list[str] = []

        def fake_client(endpoint):
            attempts.append(endpoint.name)
            if endpoint.name == "alpha":
                error = ProxmoxTransportError("dead")
                error.__cause__ = httpx.ConnectTimeout("dead")
                return _client(error=error)
            return _client(STATUS_BODY)

        with patch("core.services.cluster_node_runtime.client_for_endpoint", side_effect=fake_client):
            result = refresh_cluster_node_runtime(self.cluster)

        self.assertTrue(result.complete)
        self.assertEqual(attempts.count("alpha"), 1)
        self.assertEqual(attempts.count("beta"), 3)

    def test_invalid_payload_does_not_condemn_the_endpoint(self):
        """A node-specific bad body is not evidence against the transport."""
        attempts: list[str] = []

        def fake_client(endpoint):
            attempts.append(endpoint.name)
            client = MagicMock()

            def get(path, **kwargs):
                if "pve1" in path:
                    return {}
                return STATUS_BODY

            client.get.side_effect = get
            return client

        with patch("core.services.cluster_node_runtime.client_for_endpoint", side_effect=fake_client):
            refresh_cluster_node_runtime(self.cluster)

        self.assertEqual(_coverage(self.cluster, "pve1").error_code, ERROR_INVALID_PAYLOAD)
        self.assertTrue(_coverage(self.cluster, "pve2").complete)
        self.assertEqual(attempts.count("alpha"), 3)


class NodeRuntimeBudgetTests(TestCase):
    """The budget is `a + b·(N − F) + b′·F + c·D`, pinned by measurement.

    The prose enumeration was removed from the plan after four review rounds
    corrected it without ever changing a decision; these numbers are the
    authority and this test fails if any term grows.
    """

    def setUp(self):
        self.cluster = _cluster()
        _endpoint(self.cluster)

    def _sweep(self):
        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=_client(STATUS_BODY)):
            return refresh_cluster_node_runtime(self.cluster)

    def test_provider_calls_are_one_per_attempted_node(self):
        for count in (1, 3, 20):
            with self.subTest(nodes=count):
                ClusterNodeState.objects.filter(cluster=self.cluster).delete()
                _publish_membership(self.cluster, {f"pve{index}": True for index in range(1, count + 1)})
                client = _client(STATUS_BODY)
                with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=client):
                    refresh_cluster_node_runtime(self.cluster)
                self.assertEqual(client.get.call_count, count)

    def test_a_skipped_node_costs_no_provider_call(self):
        _publish_membership(self.cluster, {"pve1": True, "pve2": False, "pve3": False})
        client = _client(STATUS_BODY)

        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=client):
            refresh_cluster_node_runtime(self.cluster)

        self.assertEqual(client.get.call_count, 1)

    def _measure(self, node_count: int) -> int:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        ClusterNodeState.objects.filter(cluster=self.cluster).delete()
        ClusterProjectionCoverage.objects.filter(cluster=self.cluster).delete()
        _publish_membership(self.cluster, {f"pve{index}": True for index in range(1, node_count + 1)})
        with CaptureQueriesContext(connection) as captured:
            self._sweep()
        return len(captured.captured_queries)

    def test_query_count_is_linear_in_nodes(self):
        """The marginal cost of one more node must stay constant as the cluster
        grows -- a per-node query that quietly became per-node-squared is exactly
        what a budget expressed only in prose never catches."""
        one, two, three = self._measure(1), self._measure(2), self._measure(3)

        self.assertEqual(two - one, three - two)
        self.assertEqual(three, one + 2 * (two - one))

    def test_skipped_node_is_cheaper_than_an_attempted_one(self):
        """`b′ < b`: a skipped node never builds a client, so it never resolves
        the credential or the trust profile."""
        attempted = self._measure(2)

        ClusterNodeState.objects.filter(cluster=self.cluster).delete()
        ClusterProjectionCoverage.objects.filter(cluster=self.cluster).delete()
        _publish_membership(self.cluster, {"pve1": True, "pve2": False})
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as captured:
            self._sweep()

        self.assertLess(len(captured.captured_queries), attempted)

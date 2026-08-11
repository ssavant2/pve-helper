from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import httpx
from django.db import transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from core.models import (
    ClusterMembershipState,
    ClusterNodeState,
    ClusterProjectionCoverage,
    ProxmoxCluster,
    ProxmoxEndpoint,
)
from core.services.cluster_credentials import set_cluster_credential
from core.services.cluster_node_runtime import (
    ERROR_ACQUISITION_DISABLED,
    ERROR_ACQUISITION_QUARANTINED,
    ERROR_ACQUISITION_RETIRED,
    ERROR_ENDPOINTS_EXHAUSTED,
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
from core.services.cluster_projection import retire_cluster_projection
from core.services.proxmox import (
    ProxmoxAPIError,
    ProxmoxClient,
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
        self.assertEqual(
            ClusterNodeState.objects.get(cluster=self.cluster, node_name="pve1").runtime_generation,
            0,
            "the loop's own drop branch must not rewind the generation either",
        )
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
        self.assertEqual(
            row.runtime_generation,
            1,
            "departure nulls the columns but must not rewind the generation: a "
            "returning node would then publish 1 after 7, while membership's own "
            "generation only ever increases",
        )

    def test_departed_pass_is_idempotent(self):
        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=_client(STATUS_BODY)):
            refresh_cluster_node_runtime(self.cluster)
        ClusterNodeState.objects.filter(cluster=self.cluster).update(present=False)

        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=_client(STATUS_BODY)):
            first = refresh_cluster_node_runtime(self.cluster)
            second = refresh_cluster_node_runtime(self.cluster)

        self.assertEqual(first.departed, 1)
        self.assertEqual(second.departed, 0)

    def test_the_node_loop_clears_runtime_on_its_own_drop_branch(self):
        """The loop's drop branch, not the departed pass.

        A node dropped by a mid-sweep republication is refused *by the loop*, and
        the departed pass then skips it because the loop already wrote its code.
        So the loop's own clearing is the only thing nulling those columns --
        deleting it left the whole suite green, because the one test reaching
        this branch swept a node that had never published runtime.
        """
        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=_client(STATUS_BODY)):
            refresh_cluster_node_runtime(self.cluster)

        published = ClusterNodeState.objects.get(cluster=self.cluster, node_name="pve1")
        self.assertIsNotNone(published.cpu_usage, "precondition: the node published runtime")

        # 5a1B republishes without this node between the target read and its turn.
        original = refresh_node_runtime

        def drop_then_refresh(cluster, node_name, **kwargs):
            ClusterNodeState.objects.filter(cluster=cluster, node_name=node_name).update(present=False)
            ClusterMembershipState.objects.filter(cluster=cluster).update(membership_generation=2)
            return original(cluster, node_name, **kwargs)

        client = _client(STATUS_BODY)
        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=client):
            drop_then_refresh(self.cluster, "pve1")

        row = ClusterNodeState.objects.get(cluster=self.cluster, node_name="pve1")
        self.assertIsNone(row.cpu_usage, "the loop's drop branch must null the stale runtime itself")
        self.assertEqual(row.cpu_model, "")
        self.assertEqual(row.runtime_generation, 1, "nulling must not rewind the generation")
        self.assertEqual(client.get.call_count, 0, "a dropped node costs no provider call")
        coverage = _coverage(self.cluster, "pve1")
        self.assertEqual(coverage.error_code, ERROR_NODE_ABSENT)
        self.assertEqual(coverage.based_on_generation, 2, "the generation that proved the absence")

    def test_publication_stamps_the_operational_footprint(self):
        """The footprint is what tells retirement this cluster was really used.

        Nothing asserted it: neutering every stamp call failed only the query
        counters, so a cluster whose sole projection activity was runtime would
        classify as never-used.
        """
        ProxmoxCluster.objects.filter(pk=self.cluster.pk).update(operational_footprint_at=None)

        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=_client(STATUS_BODY)):
            refresh_cluster_node_runtime(self.cluster)

        self.cluster.refresh_from_db()
        self.assertIsNotNone(self.cluster.operational_footprint_at)

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

    def test_the_per_node_entry_point_refuses_an_unpublished_membership(self):
        ClusterProjectionCoverage.objects.filter(
            cluster=self.cluster, domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP
        ).update(complete=False, observed_at=None)
        client = _client(STATUS_BODY)

        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=client):
            result = refresh_node_runtime(self.cluster, "pve1")

        self.assertEqual(result.error_code, ERROR_MEMBERSHIP_NOT_PUBLISHED)
        self.assertEqual(client.get.call_count, 0)

    def test_the_per_node_entry_point_refuses_a_cluster_with_no_endpoint(self):
        """The sweep checks this once per cluster; the seam must not diverge."""
        ProxmoxEndpoint.objects.filter(cluster=self.cluster).update(enabled=False)

        result = refresh_node_runtime(self.cluster, "pve1")

        self.assertEqual(result.error_code, ERROR_NO_ENABLED_ENDPOINT)
        self.assertFalse(
            ClusterProjectionCoverage.objects.filter(
                cluster=self.cluster, domain=ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME
            ).exists()
        )

    def test_the_per_node_entry_point_reports_retirement_not_a_missing_endpoint(self):
        """Retirement deletes a cluster's endpoints *and* its projection, so any
        test placed before the lifecycle check answers the wrong question -- and
        sends a 5a1E operator to configure a connection whose projection is
        finalized.

        The state is produced by the real finalizer rather than assembled by
        hand. The hand-assembled version left the membership coverage row in
        place, which is a state retirement cannot produce, and so it passed while
        the membership test in front of the lifecycle check answered
        ``membership_not_published`` for exactly this cluster.
        """
        retire_cluster_projection(self.cluster)
        ProxmoxEndpoint.objects.filter(cluster=self.cluster).delete()
        ProxmoxCluster.objects.filter(pk=self.cluster.pk).update(
            retired_at=timezone.now(),
            enabled=False,
            retirement_mode=ProxmoxCluster.RetirementMode.VERIFIED,
        )

        result = refresh_node_runtime(self.cluster, "pve1")
        sweep = refresh_cluster_node_runtime(self.cluster)

        self.assertEqual(result.error_code, ERROR_ACQUISITION_RETIRED)
        self.assertEqual(sweep.error_code, ERROR_ACQUISITION_RETIRED)
        self.assertFalse(
            ClusterProjectionCoverage.objects.filter(
                cluster=self.cluster, domain=ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME
            ).exists()
        )

    def test_both_entry_points_refuse_a_cluster_identically(self):
        """The seam 5a1E inherits must not have its own gate order.

        Each of these states is reachable, and for two of them the two entry
        points once disagreed: a retired cluster answered
        ``membership_not_published`` on the seam, and an endpoint-less cluster
        wrote an offline coverage row on the seam while the sweep refused
        cluster-wide and wrote nothing.
        """
        cases = {
            "retired": lambda: (
                retire_cluster_projection(self.cluster),
                ProxmoxEndpoint.objects.filter(cluster=self.cluster).delete(),
                ProxmoxCluster.objects.filter(pk=self.cluster.pk).update(
                    retired_at=timezone.now(),
                    enabled=False,
                    retirement_mode=ProxmoxCluster.RetirementMode.VERIFIED,
                ),
            ),
            "disabled": lambda: ProxmoxCluster.objects.filter(pk=self.cluster.pk).update(enabled=False),
            "no endpoint": lambda: ProxmoxEndpoint.objects.filter(cluster=self.cluster).update(enabled=False),
            "no endpoint, offline node": lambda: (
                ProxmoxEndpoint.objects.filter(cluster=self.cluster).update(enabled=False),
                ClusterNodeState.objects.filter(cluster=self.cluster, node_name="pve1").update(online=False),
            ),
            "membership never published": lambda: (
                ClusterProjectionCoverage.objects.filter(
                    cluster=self.cluster, domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP
                ).update(observed_at=None),
            ),
        }

        for label, arrange in cases.items():
            with self.subTest(label):
                with transaction.atomic():
                    arrange()

                    per_node = refresh_node_runtime(self.cluster, "pve1")
                    sweep = refresh_cluster_node_runtime(self.cluster)
                    rows = ClusterProjectionCoverage.objects.filter(
                        cluster=self.cluster, domain=ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME
                    ).count()

                    self.assertEqual(per_node.error_code, sweep.error_code)
                    self.assertEqual(rows, 0, "a cluster-grain refusal is zero-row on both entry points")
                    transaction.set_rollback(True)

    def test_a_standalone_host_publishes_like_any_other(self):
        """This phase never reads topology; a one-node standalone scope must work
        exactly as a corosync member does."""
        ClusterMembershipState.objects.filter(cluster=self.cluster).update(topology_role="standalone", quorate=False)

        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=_client(STATUS_BODY)):
            result = refresh_cluster_node_runtime(self.cluster)

        self.assertTrue(result.complete)
        self.assertTrue(_coverage(self.cluster, "pve1").complete)

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


class NodeRuntimeExhaustedEndpointTests(TestCase):
    def setUp(self):
        self.cluster = _cluster()
        _endpoint(self.cluster)
        _publish_membership(self.cluster, {"pve1": True, "pve2": True})

    def test_a_later_node_is_not_mislabelled_as_having_no_endpoint(self):
        """The cluster has an endpoint; it is unreachable. Recording the
        configuration code would send an operator to check a setting that is
        correct, and 5a1F could not tell the two states apart."""
        error = ProxmoxTransportError("dead", request_sent=False)
        error.__cause__ = httpx.ConnectTimeout("dead")

        with patch("core.services.cluster_node_runtime.client_for_endpoint", return_value=_client(error=error)):
            result = refresh_cluster_node_runtime(self.cluster)

        first, second = result.nodes
        self.assertEqual(first.error_code, ERROR_PROVIDER_TIMEOUT)
        self.assertTrue(first.called_provider)
        self.assertEqual(second.error_code, ERROR_ENDPOINTS_EXHAUSTED)
        self.assertFalse(second.called_provider, "no endpoint was left to call")
        self.assertEqual(_coverage(self.cluster, "pve2").error_code, ERROR_ENDPOINTS_EXHAUSTED)

    def test_an_http_error_about_one_node_does_not_condemn_the_endpoint(self):
        """A 500 while reading pve1 says nothing about the endpoint's ability to
        answer for pve2. Treating it as fatal would blank the rest of the sweep."""

        def fake_client(endpoint):
            client = MagicMock()

            def get(path, **kwargs):
                if "pve1" in path:
                    raise ProxmoxAPIError("boom", status_code=500)
                return STATUS_BODY

            client.get.side_effect = get
            return client

        with patch("core.services.cluster_node_runtime.client_for_endpoint", side_effect=fake_client):
            refresh_cluster_node_runtime(self.cluster)

        self.assertEqual(_coverage(self.cluster, "pve1").error_code, ERROR_PROVIDER)
        self.assertTrue(_coverage(self.cluster, "pve2").complete)


class NodeRuntimeMidSweepLifecycleTests(TestCase):
    """A cluster whose lifecycle changes between two nodes of one sweep.

    Untested through two review rounds, and it covers the departed pass's own
    refusal guard: deleting those three lines otherwise leaves the suite green.
    """

    def setUp(self):
        self.cluster = _cluster()
        _endpoint(self.cluster)
        _publish_membership(self.cluster, {"pve1": True, "pve2": True, "pve3": True})

    def test_a_cluster_disabled_mid_sweep_writes_nothing_further(self):
        def disable_after_first(endpoint):
            ProxmoxCluster.objects.filter(pk=self.cluster.pk).update(enabled=False)
            return _client(STATUS_BODY)

        with patch("core.services.cluster_node_runtime.client_for_endpoint", side_effect=disable_after_first):
            result = refresh_cluster_node_runtime(self.cluster)

        self.assertTrue(_coverage(self.cluster, "pve1").complete)
        for node_name in ("pve2", "pve3"):
            self.assertFalse(
                ClusterProjectionCoverage.objects.filter(
                    cluster=self.cluster,
                    domain=ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME,
                    node_name=node_name,
                ).exists(),
                "a late refusal must create no row at all",
            )
        self.assertEqual([node.error_code for node in result.nodes[1:]], [ERROR_ACQUISITION_DISABLED] * 2)

    def test_the_departed_pass_refuses_a_cluster_disabled_mid_sweep(self):
        ClusterNodeState.objects.filter(cluster=self.cluster, node_name="pve3").update(present=False)

        def disable_after_first(endpoint):
            ProxmoxCluster.objects.filter(pk=self.cluster.pk).update(enabled=False)
            return _client(STATUS_BODY)

        with patch("core.services.cluster_node_runtime.client_for_endpoint", side_effect=disable_after_first):
            result = refresh_cluster_node_runtime(self.cluster)

        self.assertEqual(result.departed, 0)
        self.assertFalse(
            ClusterProjectionCoverage.objects.filter(
                cluster=self.cluster,
                domain=ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME,
                node_name="pve3",
            ).exists(),
            "the departed pass must apply the same refusal the node loop does",
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
        per-sweep skip set a dead transport costs a timeout on every node.

        `request_sent=False` is what the real client sets for a connect timeout
        (`proxmox._UNSENT_TRANSPORT_ERRORS`), and it is what proves the failure
        belongs to the endpoint rather than to the node it was asked about.
        """
        attempts: list[str] = []

        def fake_client(endpoint):
            attempts.append(endpoint.name)
            if endpoint.name == "alpha":
                error = ProxmoxTransportError("dead", request_sent=False)
                error.__cause__ = httpx.ConnectTimeout("dead")
                return _client(error=error)
            return _client(STATUS_BODY)

        with patch("core.services.cluster_node_runtime.client_for_endpoint", side_effect=fake_client):
            result = refresh_cluster_node_runtime(self.cluster)

        self.assertTrue(result.complete)
        self.assertEqual(attempts.count("alpha"), 1)
        self.assertEqual(attempts.count("beta"), 3)

    def test_unauthorized_condemns_the_endpoint_for_the_rest_of_the_sweep(self):
        """A rejected credential is endpoint-wide by construction: it will reject
        the next node too. Deleting this arm of the condemnation rule left the
        whole suite green."""
        attempts: list[str] = []

        def fake_client(endpoint):
            attempts.append(endpoint.name)
            if endpoint.name == "alpha":
                error = ProxmoxAPIError("denied")
                error.status_code = 403
                return _client(error=error)
            return _client(STATUS_BODY)

        with patch("core.services.cluster_node_runtime.client_for_endpoint", side_effect=fake_client):
            result = refresh_cluster_node_runtime(self.cluster)

        self.assertTrue(result.complete)
        self.assertEqual(attempts.count("alpha"), 1)
        self.assertEqual(attempts.count("beta"), 3)

    def test_one_hung_node_does_not_starve_its_siblings(self):
        """`nodes/<node>/status` is proxied, so a read timeout has two possible
        owners and `request_sent` is the only thing that separates them.

        Condemning on the first ambiguous failure made one hung node blank the
        rest of the sweep with `endpoints_exhausted` and zero provider calls --
        and because the loop is ordered by node name, the same siblings starved
        every cycle.
        """
        attempts: list[str] = []

        def fake_client(endpoint):
            attempts.append(endpoint.name)
            client = MagicMock()

            def get(path, **kwargs):
                if "pve1" in path:
                    error = ProxmoxTransportError("node hung", request_sent=True)
                    error.__cause__ = httpx.ReadTimeout("node hung")
                    raise error
                return STATUS_BODY

            client.get.side_effect = get
            return client

        with patch("core.services.cluster_node_runtime.client_for_endpoint", side_effect=fake_client):
            result = refresh_cluster_node_runtime(self.cluster)

        self.assertTrue(result.complete)
        self.assertEqual(_coverage(self.cluster, "pve1").error_code, ERROR_PROVIDER_TIMEOUT)
        self.assertTrue(_coverage(self.cluster, "pve2").complete)
        self.assertTrue(_coverage(self.cluster, "pve3").complete)
        # The hung node exhausts both endpoints itself; the siblings still get one.
        self.assertEqual(attempts.count("alpha"), 3)

    def test_an_endpoint_hanging_for_two_nodes_is_condemned(self):
        """The bound on the rule above. One ambiguous failure is the node's; the
        same endpoint hanging for a *second* node is evidence about the endpoint,
        and without a threshold the sweep's transport waste would grow with N."""
        attempts: list[str] = []

        def fake_client(endpoint):
            attempts.append(endpoint.name)
            if endpoint.name == "alpha":
                error = ProxmoxTransportError("relay hung", request_sent=True)
                error.__cause__ = httpx.ReadTimeout("relay hung")
                return _client(error=error)
            return _client(STATUS_BODY)

        with patch("core.services.cluster_node_runtime.client_for_endpoint", side_effect=fake_client):
            result = refresh_cluster_node_runtime(self.cluster)

        self.assertTrue(result.complete)
        # pve1 and pve2 each pay one timeout; pve3 is spared.
        self.assertEqual(attempts.count("alpha"), 2)
        self.assertEqual(attempts.count("beta"), 3)
        self.assertTrue(_coverage(self.cluster, "pve3").complete)

    def test_the_node_name_is_url_quoted(self):
        """The path is built by interpolation, so the quoting is the only thing
        between a node name and the request line."""
        paths: list[str] = []

        def fake_client(endpoint):
            client = MagicMock()

            def get(path, **kwargs):
                paths.append(path)
                return STATUS_BODY

            client.get.side_effect = get
            return client

        ClusterNodeState.objects.filter(cluster=self.cluster, node_name="pve1").update(node_name="pve 1/x")

        with patch("core.services.cluster_node_runtime.client_for_endpoint", side_effect=fake_client):
            refresh_cluster_node_runtime(self.cluster)

        self.assertIn("nodes/pve%201%2Fx/status", paths)

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


#: Measured, then pinned. The entry contract deliberately holds no enumeration:
#: four review rounds corrected a hand-counted figure without ever changing a
#: decision, so the numbers live here where they are checked rather than argued.
#: Total queries for one sweep of N attempted nodes, from a cold projection.
#: Measured, not derived: exactly ``14 + 19N``, linear at 1, 3 and 20 nodes.
SWEEP_QUERIES = {1: 33, 3: 71, 20: 394}
#: One attempted node plus one node the offline gate skips. A skipped node pays
#: the cluster gate (endpoints included, so both entry points refuse identically)
#: but never builds a client, so it never resolves the credential or the trust
#: profile -- which is what keeps `b′` below `b`.
MIXED_SWEEP_QUERIES = 47
#: One departed node, nothing attempted.
DEPARTED_SWEEP_QUERIES = 18


@override_settings(
    PVE_HELPER_ENCRYPTION_KEYS=f"k1:{base64.b64encode(b'C' * 32).decode()}",
    PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID="k1",
)
class NodeRuntimeBudgetTests(TestCase):
    """The budget is `a + b·(N − F) + b′·F + c·D`, pinned by measurement.

    The prose enumeration was removed from the plan after four review rounds
    corrected it without ever changing a decision; these numbers are the
    authority and this test fails if any term grows.
    """

    def setUp(self):
        self.cluster = _cluster()
        _endpoint(self.cluster)
        # A real credential, because the budget must include the credential and
        # trust resolution `client_for_endpoint` performs per call.
        set_cluster_credential(self.cluster, token_id="root@pam!budget", token_secret="x" * 32)

    def _sweep(self):
        """Patch the transport, not the client factory.

        Patching `client_for_endpoint` would hide the queries the contract names
        as part of the per-node cost: `client_for_endpoint` dereferences
        `endpoint.cluster` and runs `resolve_credential` and
        `resolve_trust_profile` on every call by design
        (`cluster_resolver.py:168-184`). A budget measured with that mocked out
        under-reports production by several queries per node -- and the
        skipped-node test's stated mechanism would be unobservable, since the
        attempted node would not resolve them either.
        """
        with patch.object(ProxmoxClient, "_request", return_value=STATUS_BODY):
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

    def _reset(self):
        ClusterNodeState.objects.filter(cluster=self.cluster).delete()
        ClusterProjectionCoverage.objects.filter(cluster=self.cluster).delete()

    def _prepare(self, node_count: int) -> None:
        """Fixture work happens outside the measured block."""
        self._reset()
        _publish_membership(self.cluster, {f"pve{index}": True for index in range(1, node_count + 1)})

    def test_the_sweep_cost_is_pinned_exactly_at_each_scale(self):
        """Absolute counts, not a slope.

        The entry contract deleted its prose enumeration on the ground that this
        test is the authority; a test that only checks linearity is not one, since
        a change adding five queries per node keeps it green.
        """
        for nodes, expected in sorted(SWEEP_QUERIES.items()):
            with self.subTest(nodes=nodes):
                self._prepare(nodes)
                with self.assertNumQueries(expected):
                    self._sweep()

    def test_a_skipped_node_costs_fewer_queries_than_an_attempted_one(self):
        """`b′ < b`: a skipped node never builds a client, so it never resolves
        the credential or the trust profile."""
        self._reset()
        _publish_membership(self.cluster, {"pve1": True, "pve2": False})

        with self.assertNumQueries(MIXED_SWEEP_QUERIES):
            self._sweep()

        self.assertLess(
            MIXED_SWEEP_QUERIES,
            SWEEP_QUERIES[1] + (SWEEP_QUERIES[3] - SWEEP_QUERIES[1]) / 2,
            "a skipped node must cost less than an attempted one: it never builds "
            "a client, so it never resolves the credential or the trust profile",
        )

    def test_the_departed_pass_cost_is_pinned_per_departed_row(self):
        """`c` was never measured before: the sweep helper deleted every node row
        first, so `D` was always zero and the term was untested."""
        self._reset()
        _publish_membership(self.cluster, {"pve1": True})
        self._sweep()
        ClusterNodeState.objects.filter(cluster=self.cluster).update(present=False)

        with self.assertNumQueries(DEPARTED_SWEEP_QUERIES):
            self._sweep()

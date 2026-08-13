from __future__ import annotations

from datetime import timedelta
from threading import Event, Lock, Thread
from unittest.mock import MagicMock, patch

import httpx
from django.db import close_old_connections, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import (
    AuditEvent,
    ClusterMembershipState,
    ClusterNodeState,
    ClusterProjectionCoverage,
    ProxmoxCluster,
    ProxmoxEndpoint,
)
from core.services.cluster_lifecycle_lock import cluster_lifecycle_lock
from core.services.cluster_membership import (
    ERROR_ACQUISITION_DISABLED,
    ERROR_ACQUISITION_QUARANTINED,
    ERROR_ACQUISITION_RETIRED,
    ERROR_INVALID_PAYLOAD,
    ERROR_NO_ENABLED_ENDPOINT,
    ERROR_OBSERVER_NOT_MEMBER,
    ERROR_PROVIDER,
    ERROR_PROVIDER_TIMEOUT,
    ERROR_PROVIDER_UNAUTHORIZED,
    ERROR_TOPOLOGY_ROLE_CHANGE,
    InvalidMembershipPayload,
    normalize_cluster_status,
    refresh_cluster_membership,
)
from core.services.cluster_projection import retire_cluster_projection
from core.services.cluster_scopes import historical_clusters
from core.services.cluster_topology_role import RoleTransition, TopologyRole
from core.services.proxmox import (
    ProxmoxAPIError,
    ProxmoxClient,
    ProxmoxInvalidResponseError,
    ProxmoxTransportError,
)
from core.tests_membership_contract_fixtures import (
    CREDENTIAL_REVOKED_MESSAGE,
    ONE_NODE_COROSYNC_CLUSTER_STATUS,
    PERMISSION_DENIED_MESSAGE,
    STANDALONE_CLUSTER_STATUS,
)


def _cluster(key: str, **fields) -> ProxmoxCluster:
    return ProxmoxCluster.objects.create(key=key, display_name=key, **fields)


def _endpoint(cluster: ProxmoxCluster, name: str = "endpoint") -> ProxmoxEndpoint:
    return ProxmoxEndpoint.objects.create(
        cluster=cluster,
        name=name,
        url=f"https://{name}.{cluster.key}.test:8006",
    )


def _corosync_rows(node_count: int, *, local: str = "pve1", prefix: str = "pve") -> list[dict]:
    rows = [
        {
            "id": "cluster",
            "name": "Test Cluster",
            "nodes": node_count,
            "quorate": 1,
            "type": "cluster",
            "version": 1,
        }
    ]
    for number in range(1, node_count + 1):
        name = f"{prefix}{number}"
        rows.append(
            {
                "id": f"node/{name}",
                "ip": f"192.0.2.{number}",
                "level": "",
                "local": int(name == local),
                "name": name,
                "nodeid": number,
                "online": 1,
                "type": "node",
            }
        )
    return rows


def _client(value=None, *, error: Exception | None = None):
    client = MagicMock()
    client.get.side_effect = error
    if error is None:
        client.get.return_value = value
    return client


def _status_error(status_code: int) -> ProxmoxAPIError:
    messages = {401: CREDENTIAL_REVOKED_MESSAGE, 403: PERMISSION_DENIED_MESSAGE}
    return ProxmoxAPIError(messages[status_code])


def _timeout_error() -> ProxmoxTransportError:
    cause = httpx.ReadTimeout("timed out")
    error = ProxmoxTransportError("provider timeout")
    error.__cause__ = cause
    return error


class MembershipNormalizerTests(SimpleTestCase):
    def test_transport_populates_structured_http_status(self):
        request = httpx.Request("GET", "https://provider.test/api2/json/cluster/status")
        response = httpx.Response(403, request=request)
        transport = MagicMock()
        transport.request.return_value = response
        client = ProxmoxClient("https://provider.test")

        with patch.object(client, "_http_client", return_value=transport), self.assertRaises(ProxmoxAPIError) as raised:
            client.get("cluster/status")

        self.assertEqual(raised.exception.status_code, 403)

    def test_u1_standalone_and_one_node_corosync_shapes_remain_distinct(self):
        standalone = normalize_cluster_status(STANDALONE_CLUSTER_STATUS)
        clustered = normalize_cluster_status(ONE_NODE_COROSYNC_CLUSTER_STATUS)

        self.assertFalse(standalone.has_cluster_row)
        self.assertFalse(standalone.quorate)
        self.assertEqual(standalone.observed_from, "pve301")
        self.assertTrue(clustered.has_cluster_row)
        self.assertTrue(clustered.quorate)
        self.assertEqual(clustered.observed_from, "pve201")

    def test_empty_malformed_duplicate_and_ambiguous_shapes_are_never_standalone(self):
        cases = {
            "empty": [],
            "not a list": {},
            "unknown row": [{"type": "qdevice"}],
            "no local": _corosync_rows(1, local="missing"),
            "two local": [
                _corosync_rows(2)[0],
                {**_corosync_rows(2)[1], "local": 1},
                {**_corosync_rows(2)[2], "local": 1},
            ],
            "duplicate": [_corosync_rows(2)[0], _corosync_rows(2)[1], _corosync_rows(2)[1]],
            "count mismatch": [{**_corosync_rows(1)[0], "nodes": 2}, _corosync_rows(1)[1]],
            "clusterless multi-node": _corosync_rows(2)[1:],
            "wrong online type": [{**STANDALONE_CLUSTER_STATUS[0], "online": True}],
        }
        for label, payload in cases.items():
            with self.subTest(label=label), self.assertRaises(InvalidMembershipPayload):
                normalize_cluster_status(payload)


class MembershipReconcilerTests(TestCase):
    def _refresh(self, cluster: ProxmoxCluster, clients: dict[str, MagicMock]):
        with patch(
            "core.services.cluster_membership.client_for_endpoint",
            side_effect=lambda endpoint: clients[endpoint.name],
        ):
            return refresh_cluster_membership(cluster)

    def test_healthy_cost_is_one_call_for_1_3_and_20_nodes(self):
        for count in (1, 3, 20):
            with self.subTest(nodes=count):
                cluster = _cluster(f"cost-{count}")
                endpoint = _endpoint(cluster)
                client = _client(_corosync_rows(count))

                result = self._refresh(cluster, {endpoint.name: client})

                self.assertTrue(result.complete)
                client.get.assert_called_once_with("cluster/status")
                self.assertEqual(ClusterNodeState.objects.filter(cluster=cluster, present=True).count(), count)

    def test_success_publishes_one_atomic_generation_and_footprint(self):
        cluster = _cluster("publish")
        endpoint = _endpoint(cluster)

        result = self._refresh(cluster, {endpoint.name: _client(_corosync_rows(3))})

        cluster.refresh_from_db()
        state = ClusterMembershipState.objects.get(cluster=cluster)
        coverage = ClusterProjectionCoverage.objects.get(cluster=cluster, domain="membership")
        self.assertEqual(result.generation, 1)
        self.assertEqual(state.membership_generation, 1)
        self.assertEqual(state.topology_role, TopologyRole.COROSYNC)
        self.assertEqual(state.member_count, 3)
        self.assertTrue(state.quorate)
        self.assertEqual(coverage.generation, 1)
        self.assertTrue(coverage.complete)
        self.assertEqual(coverage.error_code, "")
        self.assertEqual(cluster.operational_footprint_reason, "host_projection")

    def test_complete_coverage_alone_marks_missing_node_absent(self):
        cluster = _cluster("absence")
        endpoint = _endpoint(cluster)
        first = _client(_corosync_rows(3))
        self._refresh(cluster, {endpoint.name: first})

        failed = _client(error=ProxmoxAPIError("failed"))
        self._refresh(cluster, {endpoint.name: failed})
        self.assertTrue(ClusterNodeState.objects.get(cluster=cluster, node_name="pve3").present)

        accepted = _client(_corosync_rows(2))
        result = self._refresh(cluster, {endpoint.name: accepted})
        missing = ClusterNodeState.objects.get(cluster=cluster, node_name="pve3")
        self.assertTrue(result.complete)
        self.assertFalse(missing.present)
        self.assertFalse(missing.online)
        self.assertEqual(missing.membership_generation, result.generation)

        next_result = self._refresh(cluster, {endpoint.name: _client(_corosync_rows(2))})
        missing.refresh_from_db()
        self.assertEqual(missing.membership_generation, next_result.generation)

    def test_wrong_observer_fails_over_to_an_accepted_member(self):
        cluster = _cluster("observer-failover")
        _endpoint(cluster, "a")
        _endpoint(cluster, "b")
        self._refresh(cluster, {"a": _client(_corosync_rows(2)), "b": _client(_corosync_rows(2))})

        outside = _client([{**STANDALONE_CLUSTER_STATUS[0], "name": "pve3", "id": "node/pve3"}])
        accepted = _client(_corosync_rows(2, local="pve2"))
        result = self._refresh(cluster, {"a": outside, "b": accepted})

        self.assertTrue(result.complete)
        self.assertEqual(result.observed_from, "pve2")
        self.assertEqual([item.error_code for item in result.attempts], [ERROR_OBSERVER_NOT_MEMBER, ""])
        outside.get.assert_called_once_with("cluster/status")
        accepted.get.assert_called_once_with("cluster/status")

    def test_each_failed_attempt_kind_fails_over_to_a_healthy_endpoint(self):
        cases = (
            ("provider", ProxmoxAPIError("failed"), ERROR_PROVIDER),
            ("timeout", _timeout_error(), ERROR_PROVIDER_TIMEOUT),
            ("transport parse", ProxmoxInvalidResponseError("bad json"), ERROR_INVALID_PAYLOAD),
            ("membership parse", None, ERROR_INVALID_PAYLOAD),
        )
        for label, error, expected in cases:
            with self.subTest(label=label):
                cluster = _cluster(f"failover-{label.replace(' ', '-')}")
                _endpoint(cluster, "a")
                _endpoint(cluster, "b")
                failed = _client([]) if error is None else _client(error=error)
                healthy = _client(_corosync_rows(1))

                result = self._refresh(cluster, {"a": failed, "b": healthy})

                self.assertTrue(result.complete)
                self.assertEqual([attempt.error_code for attempt in result.attempts], [expected, ""])
                failed.get.assert_called_once_with("cluster/status")
                healthy.get.assert_called_once_with("cluster/status")

    def test_endpoints_after_first_accepted_success_are_not_called(self):
        cluster = _cluster("stop-after-success")
        clients = {}
        for name in ("a", "b", "c"):
            _endpoint(cluster, name)
            clients[name] = _client(_corosync_rows(1))

        result = self._refresh(cluster, clients)

        self.assertTrue(result.complete)
        clients["a"].get.assert_called_once_with("cluster/status")
        clients["b"].get.assert_not_called()
        clients["c"].get.assert_not_called()

    def test_all_failed_endpoints_are_attempted_once_and_no_more(self):
        cluster = _cluster("all-failed")
        clients = {}
        for name in ("a", "b", "c"):
            _endpoint(cluster, name)
            clients[name] = _client(error=ProxmoxAPIError("failed"))

        result = self._refresh(cluster, clients)

        self.assertFalse(result.complete)
        self.assertEqual(result.error_code, ERROR_PROVIDER)
        self.assertEqual([attempt.endpoint_name for attempt in result.attempts], ["a", "b", "c"])
        for client in clients.values():
            client.get.assert_called_once_with("cluster/status")

    def test_all_wrong_observers_preserve_previous_good_projection(self):
        cluster = _cluster("observer-stale")
        endpoint = _endpoint(cluster)
        first_time = timezone.now() - timedelta(minutes=5)
        with patch("core.services.cluster_membership.timezone.now", return_value=first_time):
            self._refresh(cluster, {endpoint.name: _client(_corosync_rows(2))})
        before = ClusterMembershipState.objects.get(cluster=cluster)

        outside_rows = [{**STANDALONE_CLUSTER_STATUS[0], "name": "pve9", "id": "node/pve9"}]
        result = self._refresh(cluster, {endpoint.name: _client(outside_rows)})

        after = ClusterMembershipState.objects.get(cluster=cluster)
        coverage = ClusterProjectionCoverage.objects.get(cluster=cluster, domain="membership")
        self.assertFalse(result.complete)
        self.assertEqual(result.error_code, ERROR_OBSERVER_NOT_MEMBER)
        self.assertEqual(result.observed_from, "pve9")
        self.assertEqual(after.membership_generation, before.membership_generation)
        self.assertEqual(after.observed_from, before.observed_from)
        self.assertFalse(coverage.complete)
        self.assertEqual(coverage.error_code, ERROR_OBSERVER_NOT_MEMBER)
        self.assertEqual(coverage.observed_at, first_time)
        self.assertFalse(ClusterNodeState.objects.filter(cluster=cluster, node_name="pve9").exists())

    def test_failed_attempt_codes_preserve_previous_good_generation_and_observed_at(self):
        cluster = _cluster("failures")
        endpoint = _endpoint(cluster)
        first_time = timezone.now() - timedelta(minutes=5)
        with patch("core.services.cluster_membership.timezone.now", return_value=first_time):
            self._refresh(cluster, {endpoint.name: _client(_corosync_rows(1))})
        generation = ClusterMembershipState.objects.get(cluster=cluster).membership_generation

        cases = (
            (ERROR_PROVIDER_UNAUTHORIZED, _status_error(403)),
            (ERROR_PROVIDER_UNAUTHORIZED, _status_error(401)),
            (ERROR_PROVIDER_TIMEOUT, _timeout_error()),
            (ERROR_INVALID_PAYLOAD, ProxmoxInvalidResponseError("bad json")),
            (ERROR_PROVIDER, ProxmoxAPIError("provider failed")),
        )
        for expected, error in cases:
            with self.subTest(error=expected):
                result = self._refresh(cluster, {endpoint.name: _client(error=error)})
                state = ClusterMembershipState.objects.get(cluster=cluster)
                coverage = ClusterProjectionCoverage.objects.get(cluster=cluster, domain="membership")
                self.assertFalse(result.complete)
                self.assertEqual(result.error_code, expected)
                self.assertEqual(state.membership_generation, generation)
                self.assertEqual(coverage.generation, generation)
                self.assertEqual(coverage.observed_at, first_time)

        malformed = self._refresh(cluster, {endpoint.name: _client([])})
        self.assertEqual(malformed.error_code, ERROR_INVALID_PAYLOAD)
        self.assertEqual(ClusterMembershipState.objects.get(cluster=cluster).membership_generation, generation)

    def test_role_change_persists_one_sticky_block_and_audits_once(self):
        cluster = _cluster("role-change")
        endpoint = _endpoint(cluster)
        standalone = _client([{**STANDALONE_CLUSTER_STATUS[0], "name": "pve1", "id": "node/pve1"}])
        self._refresh(cluster, {endpoint.name: standalone})

        result = self._refresh(cluster, {endpoint.name: _client(_corosync_rows(1))})

        state = ClusterMembershipState.objects.get(cluster=cluster)
        coverage = ClusterProjectionCoverage.objects.get(cluster=cluster, domain="membership")
        self.assertTrue(result.complete)
        self.assertEqual(result.error_code, ERROR_TOPOLOGY_ROLE_CHANGE)
        self.assertEqual(result.role_decision.transition, RoleTransition.TRANSITION_PENDING)
        self.assertEqual(state.topology_role, TopologyRole.STANDALONE)
        self.assertTrue(coverage.complete)
        self.assertEqual(coverage.error_code, ERROR_TOPOLOGY_ROLE_CHANGE)
        self.assertTrue(state.transition_pending)
        self.assertEqual(state.pending_topology_role, TopologyRole.COROSYNC)
        event = AuditEvent.objects.get(action="cluster.topology_transition_detected")
        self.assertEqual(event.cluster_key_snapshot, cluster.key)
        self.assertEqual(event.details["registered_role"], TopologyRole.STANDALONE)
        self.assertEqual(event.details["pending_role"], TopologyRole.COROSYNC)

        repeated = self._refresh(cluster, {endpoint.name: _client(_corosync_rows(1))})
        state.refresh_from_db()
        self.assertEqual(repeated.role_decision.transition, RoleTransition.TRANSITION_PENDING)
        self.assertTrue(state.transition_pending)
        self.assertEqual(AuditEvent.objects.filter(action="cluster.topology_transition_detected").count(), 1)

    def test_complete_return_to_registered_role_withdraws_and_audits_the_block(self):
        cluster = _cluster("role-withdrawn")
        endpoint = _endpoint(cluster)
        standalone_rows = [{**STANDALONE_CLUSTER_STATUS[0], "name": "pve1", "id": "node/pve1"}]
        self._refresh(cluster, {endpoint.name: _client(standalone_rows)})
        self._refresh(cluster, {endpoint.name: _client(_corosync_rows(1))})

        result = self._refresh(cluster, {endpoint.name: _client(standalone_rows)})

        state = ClusterMembershipState.objects.get(cluster=cluster)
        self.assertEqual(result.role_decision.transition, RoleTransition.TRANSITION_WITHDRAWN)
        self.assertFalse(state.transition_pending)
        self.assertEqual(state.pending_topology_role, TopologyRole.UNKNOWN)
        event = AuditEvent.objects.get(action="cluster.topology_transition_withdrawn")
        self.assertEqual(event.details["withdrawn_pending_role"], TopologyRole.COROSYNC)

    def test_unreadable_pending_target_stays_blocked_until_operator_repair(self):
        cluster = _cluster("future-pending")
        endpoint = _endpoint(cluster)
        ClusterMembershipState.objects.create(
            cluster=cluster,
            topology_role=TopologyRole.STANDALONE,
            transition_pending=True,
            pending_topology_role="corosync-v2",
        )

        result = self._refresh(cluster, {endpoint.name: _client(_corosync_rows(1))})

        state = ClusterMembershipState.objects.get(cluster=cluster)
        self.assertEqual(result.role_decision.transition, RoleTransition.INDETERMINATE)
        self.assertTrue(state.transition_pending)
        self.assertEqual(state.pending_topology_role, "corosync-v2")

    def test_sibling_clusters_with_same_node_names_are_isolated(self):
        first = _cluster("first-sibling")
        second = _cluster("second-sibling")
        first_endpoint = _endpoint(first)
        second_endpoint = _endpoint(second)
        self._refresh(first, {first_endpoint.name: _client(_corosync_rows(2))})
        self._refresh(second, {second_endpoint.name: _client(_corosync_rows(3))})

        self._refresh(first, {first_endpoint.name: _client(_corosync_rows(1))})

        self.assertFalse(ClusterNodeState.objects.get(cluster=first, node_name="pve2").present)
        self.assertTrue(ClusterNodeState.objects.get(cluster=second, node_name="pve2").present)
        self.assertTrue(ClusterNodeState.objects.get(cluster=second, node_name="pve3").present)

    def test_publication_failure_rolls_back_projection_coverage_and_footprint(self):
        boundaries = (
            "core.services.cluster_membership.ClusterMembershipState.save",
            "core.services.cluster_membership.ClusterNodeState.save",
            "core.services.cluster_membership.ClusterProjectionCoverage.save",
            "core.services.cluster_membership.stamp_cluster_projection_footprint",
        )
        for number, target in enumerate(boundaries):
            with self.subTest(boundary=target):
                cluster = _cluster(f"rollback-{number}")
                _endpoint(cluster)
                with (
                    patch(
                        "core.services.cluster_membership.client_for_endpoint",
                        return_value=_client(_corosync_rows(2)),
                    ),
                    patch(target, side_effect=RuntimeError("injected")),
                    self.assertRaises(RuntimeError),
                ):
                    refresh_cluster_membership(cluster)

                cluster.refresh_from_db()
                self.assertFalse(ClusterMembershipState.objects.filter(cluster=cluster).exists())
                self.assertFalse(ClusterNodeState.objects.filter(cluster=cluster).exists())
                self.assertFalse(ClusterProjectionCoverage.objects.filter(cluster=cluster).exists())
                self.assertIsNone(cluster.operational_footprint_at)

    def test_incomplete_first_attempt_stamps_footprint_and_rolls_back_together(self):
        cluster = _cluster("failed-footprint")
        endpoint = _endpoint(cluster)
        result = self._refresh(cluster, {endpoint.name: _client(error=ProxmoxAPIError("failed"))})
        cluster.refresh_from_db()
        self.assertFalse(result.complete)
        self.assertEqual(cluster.operational_footprint_reason, "host_projection")

        fresh = _cluster("failed-rollback")
        _endpoint(fresh)
        with (
            patch(
                "core.services.cluster_membership.client_for_endpoint",
                return_value=_client(error=ProxmoxAPIError("failed")),
            ),
            patch(
                "core.services.cluster_membership.stamp_cluster_projection_footprint",
                side_effect=RuntimeError("injected"),
            ),
            self.assertRaises(RuntimeError),
        ):
            refresh_cluster_membership(fresh)
        self.assertFalse(ClusterProjectionCoverage.objects.filter(cluster=fresh).exists())

    def test_zero_call_refusals_write_nothing_and_stamp_nothing(self):
        disabled = _cluster("disabled", enabled=False)
        quarantined = _cluster(
            "quarantined",
            ingestion_quarantined=True,
            quarantine_reason="identity changed",
        )
        retired = _cluster(
            "retired",
            enabled=False,
            retired_at=timezone.now(),
            retirement_mode=ProxmoxCluster.RetirementMode.FORCED,
            retirement_reason="retired",
        )
        no_endpoint = _cluster("no-endpoint")
        cases = (
            (disabled, ERROR_ACQUISITION_DISABLED),
            (quarantined, ERROR_ACQUISITION_QUARANTINED),
            (retired, ERROR_ACQUISITION_RETIRED),
            (no_endpoint, ERROR_NO_ENABLED_ENDPOINT),
        )
        with patch("core.services.cluster_membership.client_for_endpoint") as factory:
            for cluster, expected in cases:
                with self.subTest(cluster=cluster.key):
                    result = refresh_cluster_membership(cluster)
                    cluster.refresh_from_db()
                    self.assertEqual(result.error_code, expected)
                    self.assertIsNone(cluster.operational_footprint_at)
                    self.assertFalse(ClusterProjectionCoverage.objects.filter(cluster=cluster).exists())
            factory.assert_not_called()


@override_settings(APP_REQUIRE_LOGIN=False)
class PassiveConnectionsMembershipTests(TestCase):
    def test_list_and_detail_never_refresh_membership(self):
        cluster = _cluster("passive")
        with patch("core.services.proxmox.ProxmoxClient.get") as provider_get:
            overview = self.client.get(reverse("core:clusters_overview"))
            detail = self.client.get(reverse("core:cluster_connection", kwargs={"cluster_key": cluster.key}))

        self.assertEqual(overview.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        provider_get.assert_not_called()


class MembershipConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def test_concurrent_refreshes_serialize_generation_and_provider_acquisition(self):
        cluster = _cluster("concurrent")
        _endpoint(cluster)
        first_in_provider = Event()
        release_first = Event()
        second_in_provider = Event()
        factory_lock = Lock()
        factory_calls = 0
        results = []
        errors = []
        response_times = []

        class FirstClient:
            def get(self, path):
                self_path = path
                first_in_provider.set()
                release_first.wait(5)
                if self_path != "cluster/status":
                    raise AssertionError(self_path)
                response_times.append(timezone.now())
                return _corosync_rows(1)

        class SecondClient:
            def get(self, path):
                second_in_provider.set()
                if path != "cluster/status":
                    raise AssertionError(path)
                response_times.append(timezone.now())
                return _corosync_rows(1)

        def factory(_endpoint):
            nonlocal factory_calls
            with factory_lock:
                factory_calls += 1
                return FirstClient() if factory_calls == 1 else SecondClient()

        def run_refresh():
            close_old_connections()
            try:
                results.append(refresh_cluster_membership(ProxmoxCluster.objects.get(pk=cluster.pk)))
            except Exception as exc:  # pragma: no cover - assertion reports thread failures
                errors.append(exc)
            finally:
                close_old_connections()

        with patch("core.services.cluster_membership.client_for_endpoint", side_effect=factory):
            first = Thread(target=run_refresh)
            second = Thread(target=run_refresh)
            first.start()
            self.assertTrue(first_in_provider.wait(5))
            second.start()
            self.assertFalse(
                second_in_provider.wait(0.2),
                "The second refresh reached Proxmox before the first released the lifecycle lock.",
            )
            release_first.set()
            first.join(5)
            second.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sorted(result.generation for result in results), [1, 2])
        self.assertEqual(ClusterMembershipState.objects.get(cluster=cluster).membership_generation, 2)
        coverage = ClusterProjectionCoverage.objects.get(cluster=cluster, domain="membership")
        self.assertGreaterEqual(coverage.observed_at, max(response_times))

    def test_retirement_waits_for_refresh_then_finalizes_without_resurrection(self):
        cluster = _cluster("retirement-race")
        _endpoint(cluster)
        in_provider = Event()
        release_provider = Event()
        retirement_complete = Event()
        errors = []

        class BlockingClient:
            def get(self, path):
                if path != "cluster/status":
                    raise AssertionError(path)
                in_provider.set()
                release_provider.wait(5)
                return _corosync_rows(1)

        def run_refresh():
            close_old_connections()
            try:
                refresh_cluster_membership(ProxmoxCluster.objects.get(pk=cluster.pk))
            except Exception as exc:  # pragma: no cover - assertion reports thread failures
                errors.append(exc)
            finally:
                close_old_connections()

        def run_retirement():
            close_old_connections()
            try:
                with transaction.atomic():
                    target = ProxmoxCluster.objects.get(pk=cluster.pk)
                    with cluster_lifecycle_lock(target):
                        locked = historical_clusters().select_for_update().get(pk=cluster.pk)
                        retire_cluster_projection(locked)
                        historical_clusters().filter(pk=cluster.pk).update(
                            enabled=False,
                            retired_at=timezone.now(),
                            retirement_mode=ProxmoxCluster.RetirementMode.FORCED,
                            retirement_reason="race test",
                        )
                retirement_complete.set()
            except Exception as exc:  # pragma: no cover - assertion reports thread failures
                errors.append(exc)
            finally:
                close_old_connections()

        with patch("core.services.cluster_membership.client_for_endpoint", return_value=BlockingClient()):
            refresh_thread = Thread(target=run_refresh)
            retire_thread = Thread(target=run_retirement)
            refresh_thread.start()
            self.assertTrue(in_provider.wait(5))
            retire_thread.start()
            self.assertFalse(retirement_complete.wait(0.2))
            release_provider.set()
            refresh_thread.join(5)
            retire_thread.join(5)

        self.assertEqual(errors, [])
        self.assertTrue(retirement_complete.is_set())
        self.assertFalse(ClusterMembershipState.objects.filter(cluster=cluster).exists())
        self.assertFalse(ClusterNodeState.objects.filter(cluster=cluster).exists())
        self.assertFalse(ClusterProjectionCoverage.objects.filter(cluster=cluster).exists())


class StandaloneNodeidTests(SimpleTestCase):
    """A standalone host reports `nodeid: 0`; a corosync member never does.

    The adapter required `nodeid >= 1` on every node row, which rejected the real
    standalone payload outright. The failure was invisible in the obvious place —
    it looked like an unreadable cluster, not like a parser that could not read the
    shape it claimed to distinguish.
    """

    def test_a_standalone_payload_with_nodeid_zero_is_accepted(self):
        normalized = normalize_cluster_status(STANDALONE_CLUSTER_STATUS)

        self.assertFalse(normalized.has_cluster_row)
        self.assertEqual(normalized.observed_from, "pve301")
        self.assertEqual(normalized.nodes[0].nodeid, 0)

    def test_a_clustered_member_may_not_report_nodeid_zero(self):
        """Inside corosync a zero is a malformed answer, not the standalone shape."""
        payload = [
            {"type": "cluster", "id": "cluster", "name": "c", "nodes": 1, "quorate": 1, "version": 1},
            {
                "id": "node/pve1",
                "ip": "10.0.0.1",
                "level": "",
                "local": 1,
                "name": "pve1",
                "nodeid": 0,
                "online": 1,
                "type": "node",
            },
        ]

        with self.assertRaises(InvalidMembershipPayload):
            normalize_cluster_status(payload)

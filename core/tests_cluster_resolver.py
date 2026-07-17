from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from core.models import ProxmoxCluster, ProxmoxEndpoint, RuntimeConfigurationState
from core.services.cluster_resolver import (
    ClusterResolutionError,
    LegacyClusterScopeError,
    cluster_wide_read,
    enabled_endpoints,
    pin_cluster_write_client,
    require_sole_enabled_cluster_for_legacy_caller,
)
from core.services.proxmox import ProxmoxAPIError


class FakeClient:
    def __init__(self, endpoint: str, *, error: Exception | None = None, value=None):
        self.endpoint = endpoint
        self._error = error
        self._value = value
        self.calls = 0

    def read(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._value


class ClusterResolverTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.cluster_a = ProxmoxCluster.objects.create(key="a", display_name="A", enabled=True)
        self.cluster_b = ProxmoxCluster.objects.create(key="b", display_name="B", enabled=False)
        self.a1 = ProxmoxEndpoint.objects.create(
            name="a1", url="https://a1.example.com:8006", cluster=self.cluster_a, enabled=True
        )
        self.a2 = ProxmoxEndpoint.objects.create(
            name="a2", url="https://a2.example.com:8006", cluster=self.cluster_a, enabled=True
        )
        self.b1 = ProxmoxEndpoint.objects.create(
            name="b1", url="https://b1.example.com:8006", cluster=self.cluster_b, enabled=True
        )

    def _clients(self, mapping: dict[str, FakeClient]):
        return patch(
            "core.services.cluster_resolver.client_for_endpoint",
            side_effect=lambda endpoint: mapping[endpoint.name],
        )


class EndpointSelectionTests(ClusterResolverTestCase):
    def test_only_the_selected_clusters_endpoints_are_returned(self):
        self.assertEqual([e.name for e in enabled_endpoints(self.cluster_a)], ["a1", "a2"])
        self.assertEqual([e.name for e in enabled_endpoints(self.cluster_b)], ["b1"])

    def test_disabled_endpoints_are_excluded(self):
        self.a1.enabled = False
        self.a1.save(update_fields=["enabled"])

        self.assertEqual([e.name for e in enabled_endpoints(self.cluster_a)], ["a2"])


class ClusterWideReadTests(ClusterResolverTestCase):
    def test_one_authoritative_answer_is_complete_coverage(self):
        clients = {"a1": FakeClient("a1", value=["guest"]), "a2": FakeClient("a2", value=["guest"])}

        with self._clients(clients):
            result = cluster_wide_read(self.cluster_a, operation="inventory", call=lambda c: c.read())

        self.assertTrue(result.complete)
        self.assertEqual(result.value, ["guest"])
        self.assertEqual(result.answering_endpoint, "a1")
        # The second endpoint is an alternative transport to the same control
        # plane, so it must not be read again and the guest must not be stored twice.
        self.assertEqual(clients["a2"].calls, 0)

    def test_failover_stays_inside_the_cluster_and_stays_complete(self):
        clients = {
            "a1": FakeClient("a1", error=ProxmoxAPIError("boom")),
            "a2": FakeClient("a2", value=["guest"]),
        }

        with self._clients(clients):
            result = cluster_wide_read(self.cluster_a, operation="inventory", call=lambda c: c.read())

        # A failed redundant endpoint degrades endpoint health but does not make a
        # successful cluster-wide read partial.
        self.assertTrue(result.complete)
        self.assertEqual(result.answering_endpoint, "a2")
        self.assertEqual([a.ok for a in result.attempted], [False, True])
        self.assertEqual(result.errors, ["boom"])

    def test_never_falls_back_to_another_clusters_endpoint(self):
        clients = {
            "a1": FakeClient("a1", error=ProxmoxAPIError("down")),
            "a2": FakeClient("a2", error=ProxmoxAPIError("down")),
            "b1": FakeClient("b1", value=["other cluster guest"]),
        }

        with self._clients(clients):
            result = cluster_wide_read(self.cluster_a, operation="inventory", call=lambda c: c.read())

        self.assertFalse(result.complete)
        self.assertIsNone(result.value)
        self.assertEqual(clients["b1"].calls, 0)
        self.assertEqual([a.endpoint_name for a in result.attempted], ["a1", "a2"])

    def test_wholly_unreachable_cluster_is_incomplete_rather_than_empty(self):
        clients = {
            "a1": FakeClient("a1", error=ProxmoxAPIError("down")),
            "a2": FakeClient("a2", error=ProxmoxAPIError("down")),
        }

        with self._clients(clients):
            result = cluster_wide_read(self.cluster_a, operation="inventory", call=lambda c: c.read())

        # Guests are unknown, not absent: an empty value with complete coverage
        # would let a caller retire another cluster's inventory.
        self.assertFalse(result.complete)
        self.assertIsNone(result.value)

    def test_unexpected_exceptions_are_not_swallowed_as_degradation(self):
        clients = {"a1": FakeClient("a1", error=RuntimeError("programming bug")), "a2": FakeClient("a2")}

        with self._clients(clients), self.assertRaises(RuntimeError):
            cluster_wide_read(self.cluster_a, operation="inventory", call=lambda c: c.read())

        self.assertEqual(clients["a2"].calls, 0)


class ClusterWriteTests(ClusterResolverTestCase):
    def test_write_pins_a_single_endpoint(self):
        clients = {"a1": FakeClient("a1"), "a2": FakeClient("a2")}

        with self._clients(clients):
            endpoint, client = pin_cluster_write_client(self.cluster_a)

        self.assertEqual(endpoint.name, "a1")
        self.assertIs(client, clients["a1"])

    def test_write_without_an_enabled_endpoint_fails_explicitly(self):
        ProxmoxEndpoint.objects.filter(cluster=self.cluster_a).update(enabled=False)

        with self.assertRaises(ClusterResolutionError):
            pin_cluster_write_client(self.cluster_a)


class LegacyScopeAdapterTests(ClusterResolverTestCase):
    def test_returns_the_sole_enabled_cluster(self):
        self.assertEqual(require_sole_enabled_cluster_for_legacy_caller(), self.cluster_a)

    def test_fails_when_no_cluster_is_enabled(self):
        ProxmoxCluster.objects.update(enabled=False)

        with self.assertRaises(LegacyClusterScopeError):
            require_sole_enabled_cluster_for_legacy_caller()

    def test_fails_closed_at_contract_version_one(self):
        RuntimeConfigurationState.objects.create(
            pk=RuntimeConfigurationState.SINGLETON_PK,
            bootstrap_completed=True,
            identity_contract_version=1,
        )

        # Even with exactly one enabled cluster, an implicit selection must stop
        # working once identity is active: the adapter must fail closed rather than
        # silently keep choosing.
        with self.assertRaises(LegacyClusterScopeError):
            require_sole_enabled_cluster_for_legacy_caller()

    # The "several enabled clusters" branch of the adapter has no test on purpose.
    # It is unreachable today — the database permits only one enabled cluster before
    # activation, and after activation the version check above fires first — so any
    # test would have to mock the ORM and would assert against the mock rather than
    # the code. The branch stays as defence for the window where activation drops the
    # single-enabled-cluster constraint; Phase 4 deletes the adapter entirely.

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from core.models import ProxmoxCluster, ProxmoxEndpoint
from core.services.cluster_resolver import (
    ClusterDisabledError,
    ClusterResolutionError,
    cluster_clients,
    cluster_wide_read,
    cluster_write,
    enabled_endpoints,
    pin_cluster_write_client,
)
from core.services.proxmox import ProxmoxAPIError, ProxmoxTransportError


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

    def test_known_healthy_endpoint_precedes_a_known_failed_endpoint(self):
        self.a1.last_health_status = "error"
        self.a1.save(update_fields=["last_health_status"])
        self.a2.last_health_status = "ok"
        self.a2.save(update_fields=["last_health_status"])

        self.assertEqual([e.name for e in enabled_endpoints(self.cluster_a)], ["a2", "a1"])


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


class DisabledClusterTests(ClusterResolverTestCase):
    """Disabling a cluster blocks live acquisition immediately. Stored read models
    and history stay readable; only new provider traffic stops."""

    def setUp(self):
        super().setUp()
        self.cluster_a.enabled = False
        self.cluster_a.save(update_fields=["enabled"])

    def test_reads_are_refused(self):
        with self.assertRaises(ClusterDisabledError):
            cluster_wide_read(self.cluster_a, operation="inventory", call=lambda c: c.read())

    def test_writes_are_refused(self):
        with self.assertRaises(ClusterDisabledError):
            cluster_write(
                self.cluster_a,
                operation="guest_post",
                call=lambda c: c.read(),
                error_message=str,
            )

    def test_client_selection_is_refused(self):
        with self.assertRaises(ClusterDisabledError):
            cluster_clients(self.cluster_a)
        with self.assertRaises(ClusterDisabledError):
            pin_cluster_write_client(self.cluster_a)

    def test_endpoint_query_still_answers_for_verification_flows(self):
        # Onboarding and re-enable must be able to inspect a cluster that is not
        # enabled yet; only acquisition is gated.
        self.assertEqual([e.name for e in enabled_endpoints(self.cluster_a)], ["a1", "a2"])


class ClusterWriteContractTests(ClusterResolverTestCase):
    """A mutation must never be replayed on a second endpoint unless the first
    attempt provably never left."""

    def _write(self, clients):
        with self._clients(clients):
            return cluster_write(
                self.cluster_a,
                operation="guest_post",
                call=lambda c: c.read(),
                error_message=lambda exc: f"public: {exc}",
            )

    def test_a_refused_connection_may_advance_to_another_endpoint(self):
        # Nothing was sent, so the mutation demonstrably did not happen.
        clients = {
            "a1": FakeClient("a1", error=ProxmoxTransportError("refused", request_sent=False)),
            "a2": FakeClient("a2", value="UPID:ok"),
        }

        result = self._write(clients)

        self.assertTrue(result.ok)
        self.assertEqual(result.answering_endpoint, "a2")
        self.assertEqual(clients["a2"].calls, 1)

    def test_an_ambiguous_failure_is_never_replayed(self):
        # The request may already have been applied; replaying it could shut down
        # or snapshot the guest twice.
        clients = {
            "a1": FakeClient("a1", error=ProxmoxTransportError("read timeout", request_sent=True)),
            "a2": FakeClient("a2", value="UPID:ok"),
        }

        result = self._write(clients)

        self.assertFalse(result.ok)
        self.assertEqual(clients["a2"].calls, 0)
        self.assertIn("public:", result.error)

    def test_an_http_error_is_not_re_asked_on_another_endpoint(self):
        # The server received the request and decided; another member of the same
        # control plane would answer the same settled question.
        clients = {
            "a1": FakeClient("a1", error=ProxmoxAPIError("403: permission denied")),
            "a2": FakeClient("a2", value="UPID:ok"),
        }

        result = self._write(clients)

        self.assertFalse(result.ok)
        self.assertEqual(clients["a2"].calls, 0)

    def test_a_write_never_reaches_another_cluster(self):
        clients = {
            "a1": FakeClient("a1", error=ProxmoxTransportError("refused", request_sent=False)),
            "a2": FakeClient("a2", error=ProxmoxTransportError("refused", request_sent=False)),
            "b1": FakeClient("b1", value="UPID:wrong-cluster"),
        }

        result = self._write(clients)

        self.assertFalse(result.ok)
        self.assertEqual(clients["b1"].calls, 0)

    def test_transport_ambiguity_defaults_to_unsafe(self):
        # Anything not proven unsent must be treated as possibly applied.
        self.assertTrue(ProxmoxTransportError("boom").ambiguous)
        self.assertTrue(ProxmoxTransportError("boom", request_sent=True).ambiguous)
        self.assertFalse(ProxmoxTransportError("boom", request_sent=False).ambiguous)

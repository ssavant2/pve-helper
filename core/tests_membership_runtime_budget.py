"""The membership + node-runtime cycle costs 1 + N provider calls. Exactly.

5a0A / U0 item 5 wrote this budget before the adapters existed, against a
reference walk this module defined itself -- a specification that could not fail.
**5a1D re-pointed it at the shipped reconcilers**, which was that phase's stated
exit criterion: a tautological budget test sitting beside a real one is worse
than no test, because a future reader picks the wrong one as proof.

Two live facts from the 2026-08-07 evidence capture make the budget achievable,
and both are asserted here so a refactor cannot quietly give them up:

* **One member answers for the whole cluster.** `nodes/<node>/status` returned
  data for pve1, pve2 and pve3 through pve1's transport alone. Nothing may open a
  transport per node, which would make the budget E + N and revive the
  endpoint-as-inventory-shard assumption the multicluster contract removed.
* **Membership is the availability authority, not the runtime read.**
  `nodes/<node>/status` carries no `status`/`online` field, so an offline node is
  known only from `cluster/status`. Skipping its runtime read is what keeps the
  budget at 1 + N rather than 1 + N + timeouts, and the skip decision comes from
  the membership generation being published.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from core.models import ProxmoxCluster, ProxmoxEndpoint
from core.services.cluster_membership import refresh_cluster_membership
from core.services.cluster_node_runtime import refresh_cluster_node_runtime
from core.services.proxmox import ProxmoxClient

# 1 membership call + 1 runtime call per node, for 1 / 3 / 20 nodes.
EXPECTED_CALLS = {1: 2, 3: 4, 20: 21}

NODE_STATUS = {
    "uptime": 1,
    "cpu": 0,
    "wait": 0.0,
    "loadavg": ["0.1", "0.1", "0.1"],
    "cpuinfo": {"model": "x", "sockets": 1, "cores": 2},
    "memory": {"total": 1024, "used": 512},
    "rootfs": {"total": 2048, "used": 1024},
    "pveversion": "pve-manager/9.2.10",
}


class CountingTransport:
    """Stands in for the HTTP layer and records the exact paths asked for.

    Patched over `ProxmoxClient._request` so the cycle exercises the real client's
    `get()` surface rather than a hand-rolled double that could drift from it.
    """

    def __init__(self, nodes: list[str], *, offline: frozenset[str] = frozenset()):
        self.nodes = nodes
        self.offline = offline
        self.paths: list[str] = []

    def __call__(self, method, path, *, timeout=None, data=None):
        self.paths.append(path)
        if path == "cluster/status":
            rows: list[dict] = [{"type": "cluster", "name": "c", "nodes": len(self.nodes), "quorate": 1}]
            rows += [
                {
                    "type": "node",
                    "name": node,
                    "nodeid": index + 1,
                    # int, exactly as the live cluster answered
                    "online": 0 if node in self.offline else 1,
                    "local": 1 if index == 0 else 0,
                    "ip": f"10.0.0.{index + 1}",
                }
                for index, node in enumerate(self.nodes)
            ]
            return rows
        if path.startswith("nodes/") and path.endswith("/status"):
            return dict(NODE_STATUS)
        raise AssertionError(f"membership+runtime must not call {path!r}")


class MembershipRuntimeBudgetTests(TestCase):
    def setUp(self):
        self.cluster = ProxmoxCluster.objects.create(key="budget", display_name="budget")
        ProxmoxEndpoint.objects.create(cluster=self.cluster, name="one", url="https://one.budget.test:8006")

    def _cycle(self, nodes: list[str], *, offline: frozenset[str] = frozenset()) -> CountingTransport:
        """Run the real reconcilers, in the order 5a1D schedules them."""
        transport = CountingTransport(nodes, offline=offline)

        def build_client(endpoint):
            client = ProxmoxClient.__new__(ProxmoxClient)
            client._request = transport
            return client

        with patch("core.services.cluster_membership.client_for_endpoint", side_effect=build_client):
            with patch("core.services.cluster_node_runtime.client_for_endpoint", side_effect=build_client):
                refresh_cluster_membership(self.cluster)
                refresh_cluster_node_runtime(self.cluster)
        return transport

    def test_the_budget_is_one_plus_n_at_one_three_and_twenty_nodes(self):
        measured = {}
        for count in sorted(EXPECTED_CALLS):
            self.cluster.node_states.all().delete()
            transport = self._cycle([f"node{index:02d}" for index in range(count)])
            measured[count] = len(transport.paths)

        self.assertEqual(
            measured,
            EXPECTED_CALLS,
            "Membership + node runtime must cost exactly 1 + N provider calls. "
            f"Measured {measured}, expected {EXPECTED_CALLS}.",
        )

    def test_every_node_is_read_through_the_one_transport(self):
        nodes = ["pve1", "pve2", "pve3"]
        transport = self._cycle(nodes)

        self.assertEqual(
            transport.paths,
            ["cluster/status"] + [f"nodes/{node}/status" for node in nodes],
            "One healthy member answers for every node (verified live 2026-08-07). "
            "Opening a transport per node would make the budget E + N and revive "
            "the endpoint-as-inventory-shard assumption.",
        )

    def test_an_offline_member_is_skipped_rather_than_attempted(self):
        transport = self._cycle(["pve1", "pve2", "pve3"], offline=frozenset({"pve2"}))

        self.assertNotIn("nodes/pve2/status", transport.paths)
        self.assertEqual(
            len(transport.paths),
            3,
            "An offline member costs no runtime call. The skip comes from the "
            "membership generation being published, because nodes/<node>/status "
            "carries no availability field of its own.",
        )

    def test_a_skipped_node_is_recorded_as_unread_not_as_empty(self):
        self._cycle(["pve1", "pve2"], offline=frozenset({"pve2"}))

        skipped = self.cluster.node_states.get(node_name="pve2")
        self.assertIsNone(
            skipped.cpu_usage,
            "An unread node must hold no runtime values -- 'could not see' is not 'nothing there'.",
        )
        self.assertEqual(skipped.runtime_generation, 0)

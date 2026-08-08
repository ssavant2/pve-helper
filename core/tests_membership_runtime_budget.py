"""The membership + node-runtime read costs 1 + N provider calls. Exactly.

5a0A / U0 item 5. This is an executable budget, written before the adapter it
constrains: 5a1B publishes membership from one `cluster/status`, 5a1C publishes one
runtime scope per `NodeRef`, and together they may not exceed one call per node plus
one for the cluster.

Two live facts from the 2026-08-07 evidence capture make that budget achievable, and
both are asserted here so a future refactor cannot quietly give them up:

* **One member answers for the whole cluster.** `nodes/<node>/status` returned data
  for pve1, pve2 and pve3 through pve1's transport alone. Nothing may open a
  transport per node, which would make the budget E + N and reintroduce the
  endpoint-as-inventory-shard assumption the multicluster contract removed.
* **Membership is the availability authority, not the runtime read.**
  `nodes/<node>/status` carries no `status`/`online` field, so an offline node is
  known only from `cluster/status`. Skipping its runtime read is what keeps the
  budget at 1 + N rather than 1 + N + timeouts, and the skip decision must come
  from the same membership generation that will be published.

`REFERENCE_WALK` below is the call sequence, not an implementation: 5a1B/5a1C
replace it with the real reconcilers and this module points at those instead. The
numbers are the contract either way.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from core.services.proxmox import ProxmoxClient

# 1 membership call + 1 runtime call per node, for 1 / 3 / 20 nodes.
EXPECTED_CALLS = {1: 2, 3: 4, 20: 21}


class CountingTransport:
    """Stands in for the HTTP layer and records the exact paths asked for.

    Patched over `ProxmoxClient._request` so the walk exercises the real client's
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
            # No status/online key here on purpose: the live endpoint has none.
            return {"uptime": 1, "cpu": 0, "loadavg": ["0.1", "0.1", "0.1"], "cpuinfo": {"model": "x"}}
        raise AssertionError(f"membership+runtime must not call {path!r}")


def reference_walk(client) -> dict[str, dict]:
    """Membership, then runtime for each node membership says is online."""
    status = client.get("cluster/status")
    members = [row for row in status if row.get("type") == "node"]
    runtime = {}
    for member in members:
        if not member.get("online"):
            continue
        runtime[member["name"]] = client.get(f"nodes/{member['name']}/status")
    return runtime


class MembershipRuntimeBudgetTests(SimpleTestCase):
    def _walk(self, nodes: list[str], *, offline: frozenset[str] = frozenset()):
        transport = CountingTransport(nodes, offline=offline)
        client = ProxmoxClient.__new__(ProxmoxClient)
        client._request = transport
        result = reference_walk(client)
        return transport, result

    def test_the_budget_is_one_plus_n_at_one_three_and_twenty_nodes(self):
        measured = {}
        for count in sorted(EXPECTED_CALLS):
            transport, _ = self._walk([f"node{index:02d}" for index in range(count)])
            measured[count] = len(transport.paths)

        self.assertEqual(
            measured,
            EXPECTED_CALLS,
            "Membership + node runtime must cost exactly 1 + N provider calls. "
            f"Measured {measured}, expected {EXPECTED_CALLS}.",
        )

    def test_every_node_is_read_through_the_one_transport(self):
        nodes = ["pve1", "pve2", "pve3"]
        transport, runtime = self._walk(nodes)

        self.assertEqual(sorted(runtime), nodes)
        self.assertEqual(
            transport.paths,
            ["cluster/status"] + [f"nodes/{node}/status" for node in nodes],
            "One healthy member answers for every node (verified live 2026-08-07). "
            "Opening a transport per node would make the budget E + N and revive "
            "the endpoint-as-inventory-shard assumption.",
        )

    def test_an_offline_member_is_skipped_rather_than_attempted(self):
        transport, runtime = self._walk(["pve1", "pve2", "pve3"], offline=frozenset({"pve2"}))

        self.assertNotIn("pve2", runtime)
        self.assertNotIn("nodes/pve2/status", transport.paths)
        self.assertEqual(
            len(transport.paths),
            3,
            "An offline member costs no runtime call. The skip must come from the "
            "membership generation being published, because nodes/<node>/status "
            "carries no availability field of its own.",
        )

    def test_a_skipped_node_is_absent_from_runtime_not_recorded_as_empty(self):
        _, runtime = self._walk(["pve1", "pve2"], offline=frozenset({"pve2"}))

        self.assertEqual(
            list(runtime),
            ["pve1"],
            "An unread node must be missing from the runtime scope, never present "
            "with an empty payload -- 'could not see' is not 'nothing there'.",
        )

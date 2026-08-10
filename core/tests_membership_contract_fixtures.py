"""Sanitized `cluster/status` contract fixtures, captured live. Module 5 U1.

`docs/hosts&clusters.local.md` U1 required two provider shapes before 5a1B could
start, because the standalone/corosync distinction is the one thing the whole
membership contract rests on and it had never been observed:

* a **true standalone** host — no corosync configuration, so no `type=cluster`
  row. Captured 2026-08-10 from `pve301`, a disposable PVE 9.2 install created
  for this purpose and never joined to a cluster;
* a **one-node corosync cluster**, which looks superficially identical and is
  not. Captured the same day from `clusterc`/`pve201`.

The shapes are sanitized: `id` and `ip` are placeholders, and no full response is
stored. What matters here is the key set, the types and the presence or absence of
the cluster row.

These fixtures are the reason 5a1B's adapter can be written against something
other than an assumption. They are asserted through the shipped 5a0B state machine
rather than compared to themselves, so a change in either the fixtures or the
classification rule fails.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from core.services.cluster_topology_role import (
    MembershipObservation,
    RoleTransition,
    TopologyRole,
    classify_role,
    evaluate_role_transition,
)

#: `GET cluster/status` from a genuinely standalone PVE 9.2 host. One row, and it
#: is a **node** row: there is no cluster row at all, which is the whole signal.
#: Note that the node row still carries `local` and `nodeid` — the endpoint→node
#: identity proof works on a standalone host too, which was not guaranteed.
STANDALONE_CLUSTER_STATUS = [
    {
        "id": "node/pve301",
        "ip": "10.0.0.0",
        "level": "",
        "local": 1,
        "name": "pve301",
        "nodeid": 1,
        "online": 1,
        "type": "node",
    },
]

#: `GET cluster/status` from a one-node corosync cluster. Two rows: the cluster
#: row makes it a cluster, `nodes=1` and `quorate=1` notwithstanding. Reading
#: "one member" as "standalone" is the mistake this fixture exists to prevent.
ONE_NODE_COROSYNC_CLUSTER_STATUS = [
    {"id": "cluster", "name": "ClusterB", "nodes": 1, "quorate": 1, "type": "cluster", "version": 1},
    {
        "id": "node/pve201",
        "ip": "10.0.0.1",
        "level": "",
        "local": 1,
        "name": "pve201",
        "nodeid": 1,
        "online": 1,
        "type": "node",
    },
]


def observation_from(rows: list[dict], *, complete: bool = True) -> MembershipObservation:
    """Normalize a `cluster/status` payload the way 5a1B's adapter must.

    Deliberately written here rather than imported: 5a1B does not exist yet, and
    this is the shape its adapter inherits. When it lands, this function is what
    it must agree with.
    """
    nodes = [row for row in rows if row.get("type") == "node"]
    cluster_rows = [row for row in rows if row.get("type") == "cluster"]
    local = [row.get("name") for row in nodes if row.get("local") == 1]
    return MembershipObservation(
        complete=complete,
        has_cluster_row=bool(cluster_rows),
        member_count=len(nodes),
        quorate=bool(cluster_rows and cluster_rows[0].get("quorate")),
        observed_from=local[0] if local else "",
    )


class StandaloneShapeTests(SimpleTestCase):
    def test_a_standalone_host_returns_no_cluster_row(self):
        self.assertEqual([row["type"] for row in STANDALONE_CLUSTER_STATUS], ["node"])

    def test_the_standalone_shape_classifies_as_standalone(self):
        self.assertIs(classify_role(observation_from(STANDALONE_CLUSTER_STATUS)), TopologyRole.STANDALONE)

    def test_a_standalone_node_still_identifies_itself(self):
        # `local=1` is how a candidate endpoint proves which node it is. It was
        # verified on clustered hosts during 5a0A; this confirms the same proof
        # survives with no cluster to be a member of.
        observation = observation_from(STANDALONE_CLUSTER_STATUS)
        self.assertEqual(observation.observed_from, "pve301")

    def test_the_one_node_cluster_shape_classifies_as_corosync(self):
        self.assertIs(classify_role(observation_from(ONE_NODE_COROSYNC_CLUSTER_STATUS)), TopologyRole.COROSYNC)

    def test_the_two_shapes_differ_only_by_the_cluster_row(self):
        # Both have exactly one node, both quorum-irrelevant, both `local=1`.
        # Member count cannot tell them apart, which is why the rule reads the
        # cluster row and nothing else.
        standalone = observation_from(STANDALONE_CLUSTER_STATUS)
        one_node = observation_from(ONE_NODE_COROSYNC_CLUSTER_STATUS)
        self.assertEqual(standalone.member_count, one_node.member_count)
        self.assertIsNot(classify_role(standalone), classify_role(one_node))


#: `GET nodes` answered by a token with **no permissions at all**, captured
#: 2026-08-10 from `pve301` using a deliberately ACL-less API token.
#:
#: It returned **HTTP 200**, not 403, and the node is present. What is missing is
#: the metric fields: `cpu`, `mem`, `maxcpu`, `maxmem`, `disk`, `maxdisk` and
#: `uptime` are **absent keys**, not nulls. Proxmox permission-filters this
#: endpoint per field rather than refusing the request.
PERMISSION_REDUCED_NODES = [
    {
        "id": "node/pve301",
        "level": "",
        "node": "pve301",
        "ssl_fingerprint": "AA:BB:CC",
        "status": "online",
        "type": "node",
    },
]

#: The same token against `cluster/status`, which **does** refuse. Recorded as the
#: message text because `ProxmoxAPIError` carries no populated `status_code` —
#: verified live, and the same limitation `cluster-retire` recorded on 2026-07-24.
#: A consumer that branches on a status attribute will never see this.
PERMISSION_DENIED_MESSAGE = "403: Permission check failed (/, Sys.Audit)"

#: The same read after the token was revoked, captured in the same session.
#: **401, not 403** — and note that `nodes`, which would not refuse a
#: permissionless token at all, does refuse an unauthenticated one.
CREDENTIAL_REVOKED_MESSAGE = "401: Authentication failed!"

#: Minimum privilege for `GET cluster/status`, from the refusal itself rather
#: than from documentation: `Sys.Audit` on `/`.
CLUSTER_STATUS_MIN_PRIVILEGE = ("/", "Sys.Audit")


class PermissionDeniedShapeTests(SimpleTestCase):
    """The 403 half of U1 — and the trap it exposed.

    The expected finding was the refusal shape. The unexpected one is that the
    two endpoints disagree about whether a permission failure is an error at all.
    """

    def test_cluster_status_refuses_and_names_the_required_privilege(self):
        path, privilege = CLUSTER_STATUS_MIN_PRIVILEGE
        self.assertIn("403", PERMISSION_DENIED_MESSAGE)
        self.assertIn(privilege, PERMISSION_DENIED_MESSAGE)
        self.assertIn(path, PERMISSION_DENIED_MESSAGE)

    def test_the_refusal_is_only_detectable_from_the_message(self):
        # `ProxmoxAPIError.status_code` was absent on the live 403. Pinned so a
        # future reader does not write `exc.status_code == 403` and ship a branch
        # that can never run.
        from core.services.proxmox import ProxmoxAPIError

        self.assertFalse(
            hasattr(ProxmoxAPIError("403: Permission check failed (/, Sys.Audit)"), "status_code"),
            "if ProxmoxAPIError gains a populated status_code, 403 detection should move to it",
        )

    def test_nodes_does_not_refuse_at_all(self):
        # **`GET nodes` answered 200 to a token with no permissions.** It cannot
        # report a permission failure, so it can never be an authority on whether
        # a membership read was complete. 5a0A ruled it out as a membership
        # source because it lacks `local` and an address; this is the stronger
        # reason, and it is a live observation rather than an inference.
        self.assertEqual(len(PERMISSION_REDUCED_NODES), 1)
        self.assertEqual(PERMISSION_REDUCED_NODES[0]["node"], "pve301")
        self.assertEqual(PERMISSION_REDUCED_NODES[0]["status"], "online")

    def test_a_permission_reduced_node_row_is_missing_keys_not_nulled(self):
        # The failure mode this creates: `row["cpu"]` raises KeyError, and
        # `row.get("cpu", 0)` publishes a healthy-looking idle node. 5a1C reads
        # per-node runtime and must treat absent metric keys as *unknown*, never
        # as zero.
        row = PERMISSION_REDUCED_NODES[0]
        for field in ("cpu", "mem", "maxcpu", "maxmem", "disk", "maxdisk", "uptime"):
            with self.subTest(field=field):
                self.assertNotIn(field, row)

    def test_a_dead_credential_is_401_and_a_denied_one_is_403(self):
        # Two different repairs: rotate the token, or grant it a privilege. A
        # consumer that collapses both into "provider error" tells the operator
        # to do the wrong thing. Both strings were captured live minutes apart
        # against the same endpoint.
        self.assertIn("401", CREDENTIAL_REVOKED_MESSAGE)
        self.assertIn("403", PERMISSION_DENIED_MESSAGE)
        self.assertNotIn("401", PERMISSION_DENIED_MESSAGE)

    def test_nodes_refuses_an_unauthenticated_read_though_not_an_unprivileged_one(self):
        # The asymmetry worth remembering: `nodes` answers 200 to a token with no
        # permissions and 401 to no valid token. Its silence is about
        # authorization only, so "it returned rows" proves authentication and
        # nothing else.
        self.assertIn("401", CREDENTIAL_REVOKED_MESSAGE)

    def test_the_reduced_row_still_identifies_the_node(self):
        # Identity survives the permission filter even when every metric is gone,
        # so a reduced response is genuinely partial data rather than a different
        # object. That is what makes silently publishing it plausible.
        full = {"node": "pve301", "status": "online", "type": "node"}
        self.assertTrue(full.items() <= PERMISSION_REDUCED_NODES[0].items())


class DegradedReadTests(SimpleTestCase):
    """A failed read must never flip a host between the Hosts and Clusters groups."""

    def test_a_timeout_preserves_a_registered_corosync_role_as_stale(self):
        # An incomplete read carries no classification, whatever it happens to
        # contain. The registered role survives and nothing is blocked.
        timed_out = observation_from(STANDALONE_CLUSTER_STATUS, complete=False)
        decision = evaluate_role_transition(TopologyRole.COROSYNC, timed_out)
        self.assertIs(decision.transition, RoleTransition.INDETERMINATE)
        self.assertIs(decision.role, TopologyRole.COROSYNC)
        self.assertFalse(decision.blocks_provider_work)

    def test_a_timeout_preserves_a_registered_standalone_role_as_stale(self):
        timed_out = observation_from(ONE_NODE_COROSYNC_CLUSTER_STATUS, complete=False)
        decision = evaluate_role_transition(TopologyRole.STANDALONE, timed_out)
        self.assertIs(decision.transition, RoleTransition.INDETERMINATE)
        self.assertIs(decision.role, TopologyRole.STANDALONE)

"""Sanitized `cluster/status` contract fixtures, captured live. Module 5 U1.

`docs/hosts&clusters.local.md` U1 required two provider shapes before 5a1B could
start, because the standalone/corosync distinction is the one thing the whole
membership contract rests on and it had never been observed:

* a **true standalone** host — no corosync configuration, so no `type=cluster`
  row. Captured 2026-08-10 from `pve301`, a disposable PVE 9.2 install created
  for this purpose and never joined to a cluster;
* a **one-node corosync cluster**, which looks superficially identical and is
  not. Captured the same day from `clusterc`/`pve201`.

`pve301` ran **PVE 9.2.10**, `clusterc` **9.2.5**. "9.2" is a family, not a
version, and an evidence row is per host.

The shapes are sanitized: `id`, `ip` and `ssl_fingerprint` are placeholders, and
the node-runtime artifact stores schema only, never values. What matters here is
the key set, types, nullability and the presence or absence of the cluster row.

**What is and is not asserted through the state machine.** The two `cluster/status`
shapes are: they go through `classify_role` and `evaluate_role_transition`, so a
changed fixture and a changed rule both fail. The permission-denied constants are
*recorded observations* — a message string and a key set have no behaviour to run
them through, and pretending otherwise was an overstatement in the first version
of this file. The one place the refusal does reach behaviour is
`observation_from_failure`, which is the mapping 5a1B inherits.
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

#: Exact source used for the permission contract. The digest makes a later
#: documentation change visible instead of silently rewriting this evidence.
PVE_API_SCHEMA_PROVENANCE = {
    "url": "https://pve.proxmox.com/pve-docs/api-viewer/apidoc.js",
    "sha256": "9def8f13611184ee1c7d0399713130dfc4a065701d0d91a69b9c03df929344e9",
    "captured_at": "2026-08-11",
}

#: Successful standalone read metadata. ``call_count`` is the provider cost of
#: one logical read, not the number of evidence samples in the ledger below.
#: Permissions are copied exactly from the official schema identified above;
#: the live Administrator reads prove the shapes, not minimum privilege.
STANDALONE_READ_CONTRACT = {
    "cluster/status": {
        "status_code": 200,
        "call_count": 1,
        "permissions": {"kind": "check", "path": "/", "privileges": ("Sys.Audit",)},
    },
    "nodes": {
        "status_code": 200,
        "call_count": 1,
        "permissions": {"kind": "user", "user": "all"},
    },
    "nodes/{node}/status": {
        "status_code": 200,
        "call_count": 1,
        "permissions": {"kind": "check", "path": "/nodes/{node}", "privileges": ("Sys.Audit",)},
    },
}

#: Every live request used to build U1, separated from per-operation cost. The
#: 2026-08-10 row is the original evidence session; the four 2026-08-11 calls
#: are the correction pass (three status samples to observe numeric variation,
#: then one full nodes shape). All were GETs; no provider mutation was issued.
U1_EVIDENCE_LEDGER = (
    {
        "date": "2026-08-10",
        "credential_state": "administrator",
        "path": "cluster/status",
        "status_code": 200,
        "call_count": 1,
    },
    {
        "date": "2026-08-10",
        "credential_state": "administrator",
        "path": "nodes",
        "status_code": 200,
        "call_count": 1,
    },
    {
        "date": "2026-08-10",
        "credential_state": "administrator",
        "path": "nodes/{node}/status",
        "status_code": 200,
        "call_count": 1,
    },
    {
        "date": "2026-08-10",
        "credential_state": "acl_less",
        "path": "cluster/status",
        "status_code": 403,
        "call_count": 1,
    },
    {
        "date": "2026-08-10",
        "credential_state": "acl_less",
        "path": "nodes",
        "status_code": 200,
        "call_count": 1,
    },
    {
        "date": "2026-08-10",
        "credential_state": "revoked",
        "path": "nodes",
        "status_code": 401,
        "call_count": 1,
    },
    {
        "date": "2026-08-11",
        "credential_state": "administrator",
        "path": "nodes/{node}/status",
        "status_code": 200,
        "call_count": 3,
    },
    {
        "date": "2026-08-11",
        "credential_state": "administrator",
        "path": "nodes",
        "status_code": 200,
        "call_count": 1,
    },
)

#: Schema-only shape from the successful 2026-08-11 ``GET nodes`` correction
#: read. Unlike the ACL-less shape below, all runtime fields are present.
STANDALONE_NODES_SHAPE = [
    {
        "cpu": "float",
        "disk": "int",
        "id": "str",
        "level": "str",
        "maxcpu": "int",
        "maxdisk": "int",
        "maxmem": "int",
        "mem": "int",
        "node": "str",
        "ssl_fingerprint": "str",
        "status": "str",
        "type": "str",
        "uptime": "int",
    },
]
STANDALONE_NODES_NULLABLE_KEYS = frozenset()

#: Sanitized key/type/nullability shape from one successful scoped
#: `GET nodes/pve301/status`, captured 2026-08-11 from standalone PVE 9.2.10.
#: Three harmless reads observed `cpu` and `wait` as both int zero and float under
#: load, hence `number`; every listed key was present and non-null. Nested values
#: are represented only by their types, never copied from the host.
STANDALONE_NODE_STATUS_SHAPE = {
    "boot-info": {"mode": "str", "secureboot": "int"},
    "cpu": "number",
    "cpuinfo": {
        "cores": "int",
        "cpus": "int",
        "family": "str",
        "flags": "str",
        "hvm": "str",
        "mhz": "str",
        "model": "str",
        "sockets": "int",
        "user_hz": "int",
        "vendor": "str",
    },
    "current-kernel": {"machine": "str", "release": "str", "sysname": "str", "version": "str"},
    "idle": "int",
    "ksm": {"shared": "int"},
    "kversion": "str",
    "loadavg": ["str"],
    "memory": {"available": "int", "free": "int", "total": "int", "used": "int"},
    "pveversion": "str",
    "rootfs": {"avail": "int", "free": "int", "total": "int", "used": "int"},
    "swap": {"free": "int", "total": "int", "used": "int"},
    "uptime": "int",
    "wait": "number",
}
STANDALONE_NODE_STATUS_NULLABLE_KEYS = frozenset()


def observation_from(
    rows: list[dict],
    *,
    complete: bool,
    accepted_members: frozenset[str] = frozenset(),
) -> MembershipObservation:
    """Normalize a `cluster/status` payload the way 5a1B's adapter must.

    Deliberately written here rather than imported: 5a1B does not exist yet, and
    this is the shape its adapter inherits. When it lands, this function is what
    it must agree with.

    **`complete` has no default, on purpose.** A default of `True` makes
    `observation_from([])` — an empty payload, which is what a stripped or
    truncated response looks like — classify as STANDALONE, because
    `classify_role` deliberately excludes member count as an input. That is the
    silent Hosts/Clusters flip the whole contract exists to prevent, reachable
    through a keyword nobody typed. Every caller states its completeness.

    `accepted_members` is passed through rather than dropped: it is what makes
    `complete` answerable at all (see `MembershipObservation`), and 5a1B is
    required to supply it. A normalizer that omitted it would hand 5a1B a
    reference implementation with the guard disabled.
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
        accepted_members=accepted_members,
    )


def observation_from_failure(accepted_members: frozenset[str] = frozenset()) -> MembershipObservation:
    """The mapping from *any* failed read to an observation. 5a1B inherits this.

    A refusal, a timeout, a transport error and an unparseable body all mean the
    same thing to the state machine: **this read proved nothing**. They do not
    mean "no cluster row was present", which is what a naive `except: return
    []` would produce and what would flip a corosync scope to standalone on a
    permissions regression.

    This exists because U1 measured the 403 and then asserted it only as a
    string. The measurement is worthless to 5a1B without the mapping.
    """
    return MembershipObservation(
        complete=False,
        has_cluster_row=False,
        member_count=0,
        accepted_members=accepted_members,
    )


class StandaloneShapeTests(SimpleTestCase):
    def test_the_standalone_membership_key_and_type_shape_is_pinned(self):
        row = STANDALONE_CLUSTER_STATUS[0]
        self.assertEqual(
            set(row),
            {"id", "ip", "level", "local", "name", "nodeid", "online", "type"},
        )
        self.assertEqual(
            {key: type(value).__name__ for key, value in row.items()},
            {
                "id": "str",
                "ip": "str",
                "level": "str",
                "local": "int",
                "name": "str",
                "nodeid": "int",
                "online": "int",
                "type": "str",
            },
        )
        self.assertNotIn(None, row.values())

    def test_the_one_node_corosync_key_and_type_shape_is_pinned(self):
        cluster_row, node_row = ONE_NODE_COROSYNC_CLUSTER_STATUS
        self.assertEqual(set(cluster_row), {"id", "name", "nodes", "quorate", "type", "version"})
        self.assertEqual(set(node_row), set(STANDALONE_CLUSTER_STATUS[0]))
        self.assertEqual(
            {key: type(value).__name__ for key, value in cluster_row.items()},
            {"id": "str", "name": "str", "nodes": "int", "quorate": "int", "type": "str", "version": "int"},
        )
        self.assertEqual(
            {key: type(value).__name__ for key, value in node_row.items()},
            {
                "id": "str",
                "ip": "str",
                "level": "str",
                "local": "int",
                "name": "str",
                "nodeid": "int",
                "online": "int",
                "type": "str",
            },
        )
        self.assertNotIn(None, cluster_row.values())
        self.assertNotIn(None, node_row.values())

    def test_all_three_successful_reads_have_exact_permissions_status_and_call_cost(self):
        self.assertEqual(
            STANDALONE_READ_CONTRACT,
            {
                "cluster/status": {
                    "status_code": 200,
                    "call_count": 1,
                    "permissions": {"kind": "check", "path": "/", "privileges": ("Sys.Audit",)},
                },
                "nodes": {
                    "status_code": 200,
                    "call_count": 1,
                    "permissions": {"kind": "user", "user": "all"},
                },
                "nodes/{node}/status": {
                    "status_code": 200,
                    "call_count": 1,
                    "permissions": {
                        "kind": "check",
                        "path": "/nodes/{node}",
                        "privileges": ("Sys.Audit",),
                    },
                },
            },
        )

    def test_permission_provenance_and_complete_evidence_call_ledger_are_pinned(self):
        self.assertEqual(
            PVE_API_SCHEMA_PROVENANCE,
            {
                "url": "https://pve.proxmox.com/pve-docs/api-viewer/apidoc.js",
                "sha256": "9def8f13611184ee1c7d0399713130dfc4a065701d0d91a69b9c03df929344e9",
                "captured_at": "2026-08-11",
            },
        )
        self.assertEqual(sum(row["call_count"] for row in U1_EVIDENCE_LEDGER), 10)
        self.assertEqual(
            [
                (row["date"], row["credential_state"], row["path"], row["status_code"], row["call_count"])
                for row in U1_EVIDENCE_LEDGER
            ],
            [
                ("2026-08-10", "administrator", "cluster/status", 200, 1),
                ("2026-08-10", "administrator", "nodes", 200, 1),
                ("2026-08-10", "administrator", "nodes/{node}/status", 200, 1),
                ("2026-08-10", "acl_less", "cluster/status", 403, 1),
                ("2026-08-10", "acl_less", "nodes", 200, 1),
                ("2026-08-10", "revoked", "nodes", 401, 1),
                ("2026-08-11", "administrator", "nodes/{node}/status", 200, 3),
                ("2026-08-11", "administrator", "nodes", 200, 1),
            ],
        )

    def test_the_successful_nodes_shape_is_pinned_without_values(self):
        self.assertEqual(
            STANDALONE_NODES_SHAPE,
            [
                {
                    "cpu": "float",
                    "disk": "int",
                    "id": "str",
                    "level": "str",
                    "maxcpu": "int",
                    "maxdisk": "int",
                    "maxmem": "int",
                    "mem": "int",
                    "node": "str",
                    "ssl_fingerprint": "str",
                    "status": "str",
                    "type": "str",
                    "uptime": "int",
                }
            ],
        )
        self.assertEqual(STANDALONE_NODES_NULLABLE_KEYS, frozenset())

    def test_the_successful_node_status_shape_is_pinned_without_values(self):
        self.assertEqual(
            STANDALONE_NODE_STATUS_SHAPE,
            {
                "boot-info": {"mode": "str", "secureboot": "int"},
                "cpu": "number",
                "cpuinfo": {
                    "cores": "int",
                    "cpus": "int",
                    "family": "str",
                    "flags": "str",
                    "hvm": "str",
                    "mhz": "str",
                    "model": "str",
                    "sockets": "int",
                    "user_hz": "int",
                    "vendor": "str",
                },
                "current-kernel": {"machine": "str", "release": "str", "sysname": "str", "version": "str"},
                "idle": "int",
                "ksm": {"shared": "int"},
                "kversion": "str",
                "loadavg": ["str"],
                "memory": {"available": "int", "free": "int", "total": "int", "used": "int"},
                "pveversion": "str",
                "rootfs": {"avail": "int", "free": "int", "total": "int", "used": "int"},
                "swap": {"free": "int", "total": "int", "used": "int"},
                "uptime": "int",
                "wait": "number",
            },
        )
        self.assertEqual(STANDALONE_NODE_STATUS_NULLABLE_KEYS, frozenset())

    def test_a_standalone_host_returns_no_cluster_row(self):
        self.assertEqual([row["type"] for row in STANDALONE_CLUSTER_STATUS], ["node"])

    def test_the_standalone_shape_classifies_as_standalone(self):
        self.assertIs(
            classify_role(observation_from(STANDALONE_CLUSTER_STATUS, complete=True)), TopologyRole.STANDALONE
        )

    def test_a_standalone_node_still_identifies_itself(self):
        # `local=1` is how a candidate endpoint proves which node it is. It was
        # verified on clustered hosts during 5a0A; this confirms the same proof
        # survives with no cluster to be a member of.
        observation = observation_from(STANDALONE_CLUSTER_STATUS, complete=True)
        self.assertEqual(observation.observed_from, "pve301")

    def test_the_one_node_cluster_shape_classifies_as_corosync(self):
        self.assertIs(
            classify_role(observation_from(ONE_NODE_COROSYNC_CLUSTER_STATUS, complete=True)), TopologyRole.COROSYNC
        )

    def test_the_two_shapes_differ_only_by_the_cluster_row(self):
        # Both have exactly one node, both quorum-irrelevant, both `local=1`.
        # Member count cannot tell them apart, which is why the rule reads the
        # cluster row and nothing else.
        standalone = observation_from(STANDALONE_CLUSTER_STATUS, complete=True)
        one_node = observation_from(ONE_NODE_COROSYNC_CLUSTER_STATUS, complete=True)
        self.assertEqual(standalone.member_count, one_node.member_count)
        self.assertIsNot(classify_role(standalone), classify_role(one_node))


#: `GET nodes` answered by a token with **no permissions at all**, captured
#: 2026-08-10 from `pve301` using a deliberately ACL-less API token.
#:
#: It returned **HTTP 200**, not 403, and the node is present. What is missing is
#: the metric fields: `cpu`, `mem`, `maxcpu`, `maxmem`, `disk`, `maxdisk` and
#: `uptime` are **absent keys**, not nulls.
#:
#: **Scope of this observation, stated because the conclusion drawn from it is
#: broader than the measurement:** one call, one zero-privilege token, one node,
#: one standalone host at 9.2.10. Field-level filtering is what was seen *here*.
#: Row-level filtering under a *partial* privilege on a *clustered* host is not
#: observed and would be worse -- a member omitted entirely rather than thinned.
#: Both mitigations point the same way: `/nodes` still cannot report a permission
#: failure, and an absent metric is still unknown rather than zero. The rule
#: 5a1C inherits holds under either mechanism; the mechanism itself is not
#: `verified` beyond this case.
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
        self.assertTrue(PERMISSION_DENIED_MESSAGE.startswith("403"), PERMISSION_DENIED_MESSAGE)
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
        self.assertEqual(
            set(PERMISSION_REDUCED_NODES[0]),
            {"id", "level", "node", "ssl_fingerprint", "status", "type"},
        )
        self.assertEqual(
            {key: type(value).__name__ for key, value in PERMISSION_REDUCED_NODES[0].items()},
            {"id": "str", "level": "str", "node": "str", "ssl_fingerprint": "str", "status": "str", "type": "str"},
        )
        self.assertNotIn(None, PERMISSION_REDUCED_NODES[0].values())

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
        self.assertTrue(CREDENTIAL_REVOKED_MESSAGE.startswith("401"), CREDENTIAL_REVOKED_MESSAGE)
        self.assertTrue(PERMISSION_DENIED_MESSAGE.startswith("403"), PERMISSION_DENIED_MESSAGE)
        self.assertFalse(PERMISSION_DENIED_MESSAGE.startswith("401"))

    def test_nodes_refuses_an_unauthenticated_read_though_not_an_unprivileged_one(self):
        # The asymmetry worth remembering: `nodes` answers 200 to a token with no
        # permissions and 401 to no valid token. Its silence is about
        # authorization only, so "it returned rows" proves authentication and
        # nothing else.
        self.assertTrue(CREDENTIAL_REVOKED_MESSAGE.startswith("401"), CREDENTIAL_REVOKED_MESSAGE)

    def test_the_reduced_row_still_identifies_the_node(self):
        # Identity survives the permission filter even when every metric is gone,
        # so a reduced response is genuinely partial data rather than a different
        # object. That is what makes silently publishing it plausible.
        full = {"node": "pve301", "status": "online", "type": "node"}
        self.assertTrue(full.items() <= PERMISSION_REDUCED_NODES[0].items())


class DegradedReadTests(SimpleTestCase):
    """A failed read must never flip a host between the Hosts and Clusters groups.

    U1 step 5 requires this proven for **timeout and 403**. The 403 half was
    measured and then asserted only as a string, which proves the provider's
    behaviour and nothing about ours. What 5a1B actually inherits is the
    *mapping*, and these tests are it.
    """

    def test_a_403_preserves_a_registered_corosync_role_as_stale(self):
        # The live refusal, carried into the state machine rather than left as a
        # recorded fact. The dangerous alternative is an adapter that catches
        # ProxmoxAPIError and returns [] -- an empty payload reads as "no cluster
        # row", which is standalone, which flips the group on a permissions
        # regression.
        decision = evaluate_role_transition(TopologyRole.COROSYNC, observation_from_failure())
        self.assertIs(decision.transition, RoleTransition.INDETERMINATE)
        self.assertIs(decision.role, TopologyRole.COROSYNC)
        self.assertFalse(decision.blocks_provider_work)

    def test_a_403_preserves_a_registered_standalone_role_too(self):
        decision = evaluate_role_transition(TopologyRole.STANDALONE, observation_from_failure())
        self.assertIs(decision.transition, RoleTransition.INDETERMINATE)
        self.assertIs(decision.role, TopologyRole.STANDALONE)

    def test_an_empty_payload_read_as_complete_would_flip_the_group(self):
        # Why `observation_from` has no `complete` default, stated as a test so
        # the reasoning cannot be edited away with the keyword. An empty response
        # believed complete classifies as standalone -- member count is
        # deliberately not an input to the rule.
        wrongly_trusted = observation_from([], complete=True)
        self.assertIs(classify_role(wrongly_trusted), TopologyRole.STANDALONE)
        # ...and the same payload, honestly reported, changes nothing.
        self.assertIs(classify_role(observation_from([], complete=False)), TopologyRole.UNKNOWN)

    def test_a_failed_read_does_not_disturb_a_pending_transition(self):
        decision = evaluate_role_transition(
            TopologyRole.STANDALONE, observation_from_failure(), pending_role=TopologyRole.COROSYNC
        )
        self.assertIs(decision.pending_role, TopologyRole.COROSYNC)
        self.assertTrue(decision.blocks_provider_work)

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

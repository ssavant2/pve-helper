"""What a guest NIC may attach to on a node.

Two surfaces asked this question independently and both answered it wrongly against
live production, in opposite directions:

* the migrate dialog **under-reported** — it filtered the plain interface listing by
  type and merged in the cluster vnet list, which misses every realized vnet that
  has no address, so it rejected legitimate migration targets;
* the create form **over-reported** — it appended every cluster vnet with no node or
  zone test, so it offered a vnet on a node whose zone excludes it, and a guest
  created that way lands on a bridge that is not there.

The fixture is the shape that produced both, taken from the 2026-08-13 live probe:
one zone spanning every node, one zone restricted to a subset, one realized vnet
carrying an address and one without. `pve3` is the node the restricted zone excludes.

Since 5a4B-ii the seam reads the published projection rather than the provider, so
the fixture is now expressed as **rows** — which is where the live answer lands, and
therefore where the zone restriction has to survive. The publisher's own fidelity to
`?type=any_bridge` is `tests_cluster_node_networks.py`; what is pinned here is that
the seam does not re-derive, re-merge or re-widen what it was handed.

Everything below also pins the property the live version could not have: an answer
that says whether it is an answer. `[]` from an unreachable node used to render as
"no bridges", which is a proven-absent claim built from silence.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import (
    ClusterNodeEnrollment,
    ClusterNodeInterface,
    ClusterProjectionCoverage,
    ProxmoxCluster,
)
from core.services.guest_create import create_options
from core.services.node_networks import (
    _REFUSAL_REASONS,
    attachable_bridges,
    attachable_bridges_by_node,
)
from core.services.proxmox import ProxmoxAPIError

#: `?type=any_bridge` per node, as live pve1/pve3 answered it. A vnet row is typed
#: `vnet` and carries `active` as a *string*; a bridge row carries it as an integer.
#: This is what 5a4B-i's publisher writes rows from, and the rows below mirror it.
ANY_BRIDGE = {
    "pve1": [
        {"iface": "vmbr0", "type": "bridge", "active": 1},
        {"iface": "vmbr1", "type": "bridge", "active": 1},
        # Realized, carries an address -> also visible in the plain listing, but
        # typed `unknown` there.
        {"iface": "server10", "type": "vnet", "active": "1"},
        # Realized, no address -> absent from the plain listing entirely.
        {"iface": "dmz50", "type": "vnet", "active": "1"},
        # Zone `External`, restricted to pve1/pve2.
        {"iface": "wan100", "type": "vnet", "active": "1"},
    ],
    "pve3": [
        {"iface": "vmbr0", "type": "bridge", "active": 1},
        {"iface": "server10", "type": "vnet", "active": "1"},
        {"iface": "dmz50", "type": "vnet", "active": "1"},
    ],
}

#: The plain listing. Note what is missing: `dmz50` and `wan100` are realized and do
#: not appear, and `server10` is `unknown`. Neither type nor presence classifies it.
PLAIN_NETWORK = {
    "pve1": [
        {"iface": "vmbr0", "type": "bridge"},
        {"iface": "vmbr1", "type": "bridge"},
        {"iface": "bond0", "type": "bond"},
        {"iface": "nic0", "type": "eth"},
        {"iface": "server10", "type": "unknown"},
    ],
    "pve3": [
        {"iface": "vmbr0", "type": "bridge"},
        {"iface": "bond0", "type": "bond"},
        {"iface": "server10", "type": "unknown"},
    ],
}

#: Cluster-scoped and carrying no node opinion whatsoever. This is what the create
#: form used to append verbatim.
SDN_VNETS = [
    {"vnet": "server10", "zone": "Internal"},
    {"vnet": "dmz50", "zone": "Internal"},
    {"vnet": "sync40", "zone": "Internal"},
    {"vnet": "wan100", "zone": "External"},
]

GENERATION = 7


class RecordingClient:
    """Answers the paths a create-options render still needs, and records them.

    Kept after the migration precisely so the bridge paths can be asserted *absent*:
    a client that cannot answer them would prove nothing about whether they are
    still being asked.
    """

    def __init__(self, *, fail: bool = False, payload=None):
        self.paths: list[str] = []
        self._fail = fail
        self._payload = payload

    def node_names(self, *, fallback=""):
        return ["pve1", "pve3"]

    def get(self, path, *, timeout=None):
        self.paths.append(path)
        if self._fail:
            raise ProxmoxAPIError(path)
        if self._payload is not None and path.endswith("?type=any_bridge"):
            return self._payload
        for node, rows in ANY_BRIDGE.items():
            if path == f"nodes/{node}/network?type=any_bridge":
                return rows
        for node, rows in PLAIN_NETWORK.items():
            if path == f"nodes/{node}/network":
                return rows
        if path == "cluster/sdn/vnets":
            return SDN_VNETS
        if path == "cluster/nextid":
            return 500
        if path.endswith("/storage"):
            return []
        raise ProxmoxAPIError(path)


class ProjectionFixture:
    """Publishes the live fixture as projection rows, the way 5a4B-i would have."""

    def publish(self, cluster, node: str, *, generation: int = GENERATION, complete: bool = True, error: str = ""):
        now = timezone.now()
        ClusterProjectionCoverage.objects.update_or_create(
            cluster=cluster,
            domain=ClusterProjectionCoverage.DOMAIN_NODE_NETWORK,
            node_name=node,
            defaults={
                "generation": generation,
                "based_on_generation": 3,
                "complete": complete,
                "attempted_at": now,
                "observed_at": now,
                "error_code": error,
            },
        )
        for row in ANY_BRIDGE.get(node, ()):
            ClusterNodeInterface.objects.update_or_create(
                cluster=cluster,
                node_name=node,
                iface=row["iface"],
                defaults={
                    "interface_type": row["type"],
                    "attachable": True,
                    "present": True,
                    "unreachable": False,
                    "observed_generation": generation,
                    "last_seen_at": now,
                },
            )
        # Interfaces the plain listing knows and `any_bridge` does not: they exist,
        # they are published, and they are not attachable. A seam that filtered on
        # type instead of the published flag would offer `bond0`.
        for row in PLAIN_NETWORK.get(node, ()):
            if any(row["iface"] == entry["iface"] for entry in ANY_BRIDGE.get(node, ())):
                continue
            ClusterNodeInterface.objects.update_or_create(
                cluster=cluster,
                node_name=node,
                iface=row["iface"],
                defaults={
                    "interface_type": row["type"],
                    "attachable": False,
                    "present": True,
                    "unreachable": False,
                    "observed_generation": generation,
                    "last_seen_at": now,
                },
            )


class NodeAttachableBridgeTests(TestCase):
    def setUp(self):
        self.cluster = ProxmoxCluster.objects.create(key="hq", display_name="HQ", enabled=True)
        self.fixture = ProjectionFixture()
        self.fixture.publish(self.cluster, "pve1")
        self.fixture.publish(self.cluster, "pve3")

    def test_it_returns_exactly_what_the_node_can_attach_to(self):
        answer = attachable_bridges(self.cluster, "pve1")

        self.assertTrue(answer.known)
        self.assertEqual(list(answer.bridges), ["dmz50", "server10", "vmbr0", "vmbr1", "wan100"])
        self.assertEqual(answer.reason, "")

    def test_a_realized_vnet_without_an_address_is_included(self):
        """The under-report. `dmz50` is attachable and absent from the plain listing,
        so any answer derived from that listing rejects a legitimate target."""
        self.assertIn("dmz50", attachable_bridges(self.cluster, "pve1"))
        self.assertNotIn("dmz50", {row["iface"] for row in PLAIN_NETWORK["pve1"]})

    def test_a_vnet_whose_zone_excludes_the_node_is_not_offered(self):
        """The over-report. `wan100` lives in a zone scoped to pve1/pve2; offering it
        on pve3 puts the guest on a bridge that does not exist there."""
        self.assertNotIn("wan100", attachable_bridges(self.cluster, "pve3"))
        self.assertIn("wan100", {entry["vnet"] for entry in SDN_VNETS})
        self.assertIn("wan100", attachable_bridges(self.cluster, "pve1"))

    def test_attachability_is_the_published_flag_and_not_the_interface_type(self):
        """`bond0` is a published, present interface a NIC cannot attach to."""
        self.assertTrue(
            ClusterNodeInterface.objects.filter(cluster=self.cluster, node_name="pve1", iface="bond0").exists()
        )
        self.assertNotIn("bond0", attachable_bridges(self.cluster, "pve1"))

    def test_a_node_the_sweep_has_never_reached_is_unknown_rather_than_empty(self):
        answer = attachable_bridges(self.cluster, "pve2")

        self.assertFalse(answer.known)
        self.assertEqual(answer.bridges, ())
        self.assertEqual(answer.reason, "its network has not been read yet")

    def test_a_failed_pass_reports_the_publishers_own_reason(self):
        self.fixture.publish(self.cluster, "pve1", complete=False, error="provider_timeout")

        answer = attachable_bridges(self.cluster, "pve1")

        self.assertFalse(answer.known)
        self.assertEqual(answer.bridges, ())
        self.assertEqual(answer.reason, "the node timed out")

    def test_a_failed_pass_withholds_the_bridges_it_still_holds_rows_for(self):
        """Incomplete coverage is not a weaker version of current — it is unknown.

        The rows are still there and still match the last complete generation; what
        is gone is the guarantee that they describe the node now."""
        self.fixture.publish(self.cluster, "pve1", complete=False, error="endpoints_exhausted")

        self.assertEqual(
            ClusterNodeInterface.objects.filter(cluster=self.cluster, node_name="pve1", attachable=True).count(),
            5,
        )
        self.assertEqual(attachable_bridges(self.cluster, "pve1").bridges, ())

    def test_a_reason_the_map_has_never_heard_of_still_says_something(self):
        self.fixture.publish(self.cluster, "pve1", complete=False, error="a_code_from_the_future")

        self.assertEqual(attachable_bridges(self.cluster, "pve1").reason, "its network could not be read")

    def test_a_row_that_says_absent_without_saying_unknown_is_not_offered(self):
        """The publisher can no longer write this pair -- a removed interface is
        deleted outright. Constructed directly and kept anyway, because the read owns
        its predicate: `present` is checked here, not inherited from what the writer
        happens to do today."""
        ClusterNodeInterface.objects.filter(cluster=self.cluster, node_name="pve1", iface="wan100").update(
            present=False, unreachable=False
        )

        self.assertNotIn("wan100", attachable_bridges(self.cluster, "pve1"))

    def test_an_unreachable_row_is_not_offered_even_if_it_still_looks_present(self):
        """The read does not inherit the publisher's belt-and-braces.

        `_mark_unreachable` clears `present` and `attachable` as well as setting
        `unreachable`, so three separate fields would each have to be honoured for
        this row to be withheld. The read owns its own predicate: a row that says
        the node did not answer is unknown whatever else it says."""
        ClusterNodeInterface.objects.filter(cluster=self.cluster, node_name="pve1", iface="wan100").update(
            present=True, attachable=True, unreachable=True
        )

        self.assertNotIn("wan100", attachable_bridges(self.cluster, "pve1"))

    def test_an_unreachable_row_as_the_publisher_actually_writes_it_is_not_offered(self):
        ClusterNodeInterface.objects.filter(cluster=self.cluster, node_name="pve1", iface="wan100").update(
            present=False, attachable=False, unreachable=True
        )

        self.assertNotIn("wan100", attachable_bridges(self.cluster, "pve1"))

    def test_a_row_left_behind_by_an_older_generation_is_not_offered(self):
        """Currency is generation equality. A row the last complete pass did not
        touch is a leftover, and offering it is offering a bridge that was there
        two sweeps ago."""
        ClusterNodeInterface.objects.filter(cluster=self.cluster, node_name="pve1", iface="vmbr1").update(
            observed_generation=GENERATION - 1
        )

        self.assertNotIn("vmbr1", attachable_bridges(self.cluster, "pve1"))
        self.assertIn("vmbr0", attachable_bridges(self.cluster, "pve1"))

    def test_a_hidden_node_reads_as_unknown_before_the_next_sweep_notices(self):
        """The publisher only learns a node was hidden on its next pass. Until then
        its rows sit at a matching generation, and reading them would be the
        enrollment boundary leaking on a delay."""
        for node, mode in (
            ("pve1", ClusterNodeEnrollment.Mode.SAFETY_ONLY),
            ("pve3", ClusterNodeEnrollment.Mode.MANAGED),
        ):
            ClusterNodeEnrollment.objects.create(
                cluster=self.cluster, node_name=node, mode=mode, enrolled_at=timezone.now()
            )
        self.cluster.enrollment_contract_version = 1
        self.cluster.save(update_fields=["enrollment_contract_version"])

        answer = attachable_bridges(self.cluster, "pve1")

        self.assertFalse(answer.known)
        self.assertEqual(answer.bridges, ())
        self.assertEqual(answer.reason, _REFUSAL_REASONS["node_not_published"])
        self.assertTrue(attachable_bridges(self.cluster, "pve3").known)

    def test_a_retired_cluster_refuses_every_node_rather_than_raising(self):
        self.cluster.retired_at = timezone.now()
        self.cluster.enabled = False
        self.cluster.retirement_mode = ProxmoxCluster.RetirementMode.VERIFIED
        self.cluster.save(update_fields=["retired_at", "enabled", "retirement_mode"])

        answers = attachable_bridges_by_node(self.cluster, ["pve1", "pve3"])

        self.assertEqual(sorted(answers), ["pve1", "pve3"])
        self.assertFalse(any(answer.known for answer in answers.values()))

    def test_an_empty_node_costs_no_query_and_answers_unknown(self):
        with self.assertNumQueries(0):
            answer = attachable_bridges(self.cluster, "")

        self.assertFalse(answer.known)
        self.assertEqual(answer.bridges, ())

    def test_the_bulk_read_is_flat_in_node_count(self):
        with self.assertNumQueries(4):
            attachable_bridges_by_node(self.cluster, ["pve1"])
        with self.assertNumQueries(4):
            attachable_bridges_by_node(self.cluster, ["pve1", "pve2", "pve3", "pve4", "pve5"])

    def test_the_bulk_read_answers_for_every_requested_node(self):
        answers = attachable_bridges_by_node(self.cluster, ["pve3", "pve1", "pve3", ""])

        self.assertEqual(sorted(answers), ["pve1", "pve3"])
        self.assertEqual(list(answers["pve3"].bridges), ["dmz50", "server10", "vmbr0"])


class NodeNetworkReasonCoverageTests(SimpleTestCase):
    """Every code the publisher can write has a sentence the seam can say.

    The reason map is spelled out in the read path rather than imported from the
    publisher, which buys a real risk: a publisher that grows a code the map has
    never heard of, silently degrading every surface to the generic fallback. This
    is what stops that being discovered on screen.
    """

    def test_every_publishable_error_code_is_mapped(self):
        from core.services import cluster_node_networks as publisher

        codes = {
            value for name, value in vars(publisher).items() if name.startswith("ERROR_") and isinstance(value, str)
        }

        self.assertTrue(codes)
        self.assertEqual(sorted(codes - set(_REFUSAL_REASONS)), [])


class CreateFormBridgeTests(TestCase):
    def setUp(self):
        self.cluster = ProxmoxCluster.objects.create(key="hq", display_name="HQ", enabled=True)
        ProjectionFixture().publish(self.cluster, "pve1")
        ProjectionFixture().publish(self.cluster, "pve3")

    def _options(self, node: str, client=None):
        client = client or RecordingClient()
        with patch("core.services.guest_create._first_client", return_value=client):
            return create_options("vm", node, cluster=self.cluster), client

    def test_the_create_form_offers_only_what_the_selected_node_has(self):
        options, client = self._options("pve3")

        self.assertEqual(options["bridges"], ["dmz50", "server10", "vmbr0"])
        self.assertNotIn("wan100", options["bridges"])
        self.assertNotIn("cluster/sdn/vnets", client.paths)

    def test_the_create_form_makes_no_network_call_at_all(self):
        _options, client = self._options("pve3")

        self.assertEqual([path for path in client.paths if "/network" in path], [])

    def test_the_form_says_whether_the_empty_list_is_an_answer(self):
        options, _client = self._options("pve3")
        self.assertTrue(options["bridges_known"])
        self.assertEqual(options["bridges_reason"], "")

        ClusterProjectionCoverage.objects.filter(
            cluster=self.cluster,
            domain=ClusterProjectionCoverage.DOMAIN_NODE_NETWORK,
            node_name="pve3",
        ).update(complete=False, error_code="provider_unauthorized")

        options, _client = self._options("pve3")
        self.assertEqual(options["bridges"], [])
        self.assertFalse(options["bridges_known"])
        self.assertEqual(options["bridges_reason"], "the connection's token was refused")


class MigrateDialogBridgeContractTests(TestCase):
    """The dialog's half of the fix, asserted where it is cheap to assert.

    The dialog view itself needs a live-guest fixture to exercise; what is pinned
    here is the contract the browser depends on — the response no longer carries a
    cluster-wide vnet list for the client to union back in, because that union was
    the create form's bug reimplemented in JavaScript.
    """

    def test_the_response_contract_carries_no_cluster_vnet_list(self):
        from core.views.guests import dialogs

        source = inspect.getsource(dialogs.guest_migrate_options)

        self.assertNotIn("cluster/sdn/vnets", source)
        self.assertNotIn('"sdn_vnets"', source)


@override_settings(APP_REQUIRE_LOGIN=False)
class MigrateDialogTargetGatingTests(TestCase):
    """The phase's headline behaviour, at the surface that makes the decision.

    A target whose network pve-helper cannot describe is *disabled with a reason*,
    not offered with an empty bridge list. The empty list is what the browser reads
    as "every one of this guest's bridges is missing from the target" — a warning
    about the node, produced by pve-helper's own blindness.
    """

    def setUp(self):
        self.cluster = ProxmoxCluster.objects.create(key="hq", display_name="HQ", enabled=True)
        fixture = ProjectionFixture()
        fixture.publish(self.cluster, "pve1")
        fixture.publish(self.cluster, "pve3")
        self.detail = SimpleNamespace(
            cluster=self.cluster,
            node="pve1",
            vmid=500,
            object_type="vm",
            status="running",
            config={"net0": "virtio=AA:BB,bridge=vmbr0"},
        )
        user = get_user_model().objects.create_user(username="mig", password="mig-pw")
        self.client = Client()
        self.client.force_login(user)

    class DialogClient:
        """Answers only what the dialog still asks the provider for."""

        def get(self, path, *, timeout=None):
            if path == "nodes":
                return [
                    {"node": "pve1", "status": "online"},
                    {"node": "pve2", "status": "online"},
                    {"node": "pve3", "status": "online"},
                ]
            if path.endswith("/migrate"):
                return {"allowed_nodes": ["pve2", "pve3"], "not_allowed_nodes": {}}
            raise ProxmoxAPIError(path)

    def _payload(self):
        with (
            patch("core.views.guests.dialogs._require_guest", return_value=self.detail),
            patch("core.views.common.cluster_scoped_clients", return_value=[self.DialogClient()]),
        ):
            response = self.client.get(reverse("core:guest_migrate_options", args=["hq", "vm", 500]))
        self.assertEqual(response.status_code, 200)
        return {entry["node"]: entry for entry in response.json()["nodes"]}, response.json()

    def test_a_node_with_no_network_coverage_is_not_a_selectable_target(self):
        """pve2 is online and Proxmox itself allows it. What pve-helper does not
        have is any statement about its network."""
        nodes, payload = self._payload()

        self.assertFalse(nodes["pve2"]["allowed"])
        self.assertIn("network state unknown", nodes["pve2"]["reason"])
        self.assertIn("its network has not been read yet", nodes["pve2"]["reason"])
        self.assertEqual(payload["bridges_by_node"]["pve2"], [])

    def test_a_node_with_current_coverage_stays_selectable(self):
        nodes, payload = self._payload()

        self.assertTrue(nodes["pve3"]["allowed"])
        self.assertEqual(nodes["pve3"]["reason"], "")
        self.assertEqual(payload["bridges_by_node"]["pve3"], ["dmz50", "server10", "vmbr0"])

    def test_an_already_blocked_target_keeps_the_reason_it_was_blocked_for(self):
        """Proxmox's own refusal is the more actionable one and must not be
        overwritten by a second sentence about the projection."""
        ClusterProjectionCoverage.objects.filter(
            cluster=self.cluster,
            domain=ClusterProjectionCoverage.DOMAIN_NODE_NETWORK,
            node_name="pve3",
        ).update(complete=False, error_code="provider_timeout")

        with patch.object(
            self.DialogClient,
            "get",
            lambda _self, path, timeout=None: (
                [{"node": "pve1", "status": "online"}, {"node": "pve3", "status": "online"}]
                if path == "nodes"
                else {"allowed_nodes": [], "not_allowed_nodes": {"pve3": {"unavailable_storages": ["fast-nvme"]}}}
                if path.endswith("/migrate")
                else (_ for _ in ()).throw(ProxmoxAPIError(path))
            ),
        ):
            nodes, _payload = self._payload()

        self.assertFalse(nodes["pve3"]["allowed"])
        self.assertIn("fast-nvme", nodes["pve3"]["reason"])
        self.assertNotIn("network state unknown", nodes["pve3"]["reason"])

    def test_the_dialog_makes_one_projection_read_for_every_candidate(self):
        with (
            patch("core.views.guests.dialogs._require_guest", return_value=self.detail),
            patch("core.views.common.cluster_scoped_clients", return_value=[self.DialogClient()]),
            patch(
                "core.views.guests._core.attachable_bridges_by_node",
                wraps=attachable_bridges_by_node,
            ) as reader,
        ):
            self.client.get(reverse("core:guest_migrate_options", args=["hq", "vm", 500]))

        reader.assert_called_once()
        self.assertEqual(sorted(reader.call_args[0][1]), ["pve1", "pve2", "pve3"])


class BridgeReaderSourceGuardTests(SimpleTestCase):
    """One reader, one verdict.

    Both original defects came from a *second* implementation of this question. The
    guard is cheap and the alternative is discovering the drift on live data again.
    """

    def _sources(self):
        root = Path(settings.BASE_DIR)
        for path in sorted((root / "core").rglob("*.py")):
            if "migrations" in path.parts or path.name.startswith("tests"):
                continue
            if path.name in {"node_networks.py", "cluster_node_networks.py"}:
                continue
            yield path.relative_to(root), path.read_text()

    def test_only_node_networks_reads_the_cluster_vnet_list(self):
        offenders = [str(name) for name, text in self._sources() if "cluster/sdn/vnets" in text]

        self.assertEqual(
            offenders,
            [],
            "A cluster-wide SDN vnet list carries no node opinion. Attachability is "
            "`core.services.node_networks.attachable_bridges`, which reads the "
            f"projection; 5a4C owns the SDN domain itself: {', '.join(offenders)}",
        )

    def test_the_browser_does_not_re_add_the_vnet_list(self):
        source = (Path(settings.BASE_DIR) / "static" / "js" / "app" / "guest-mobility-actions.js").read_text()

        self.assertNotIn("sdn_vnets", source)


class LegacyBridgeUrlTests(SimpleTestCase):
    """A guard that the fix did not change the surface it was reached through."""

    def test_the_migrate_options_route_is_unchanged(self):
        self.assertEqual(
            reverse("core:guest_migrate_options", args=["hq", "vm", 500]),
            "/vms/hq/vm/500/migrate-options/",
        )

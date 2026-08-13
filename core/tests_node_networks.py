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

These tests pin the *verdict*, not the merge that used to compute it — the merge is
gone, and reintroducing it is what the source guard at the end is for.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core.models import ProxmoxCluster
from core.services.guest_create import create_options
from core.services.node_networks import node_attachable_bridges
from core.services.proxmox import ProxmoxAPIError

#: `?type=any_bridge` per node, as live pve1/pve3 answered it. A vnet row is typed
#: `vnet` and carries `active` as a *string*; a bridge row carries it as an integer.
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


class RecordingClient:
    """Answers the probe's paths and records every one it was asked for."""

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


class NodeAttachableBridgeTests(SimpleTestCase):
    def test_it_returns_exactly_what_the_node_can_attach_to(self):
        self.assertEqual(
            node_attachable_bridges(RecordingClient(), "pve1"),
            ["dmz50", "server10", "vmbr0", "vmbr1", "wan100"],
        )

    def test_a_realized_vnet_without_an_address_is_included(self):
        """The under-report. `dmz50` is attachable and absent from the plain listing,
        so any answer derived from that listing rejects a legitimate target."""
        self.assertIn("dmz50", node_attachable_bridges(RecordingClient(), "pve1"))
        self.assertNotIn("dmz50", {row["iface"] for row in PLAIN_NETWORK["pve1"]})

    def test_a_vnet_whose_zone_excludes_the_node_is_not_offered(self):
        """The over-report. `wan100` lives in a zone scoped to pve1/pve2; offering it
        on pve3 puts the guest on a bridge that does not exist there."""
        self.assertNotIn("wan100", node_attachable_bridges(RecordingClient(), "pve3"))
        self.assertIn("wan100", {entry["vnet"] for entry in SDN_VNETS})

    def test_it_asks_one_endpoint_and_never_the_cluster_vnet_list(self):
        client = RecordingClient()

        node_attachable_bridges(client, "pve1")

        self.assertEqual(client.paths, ["nodes/pve1/network?type=any_bridge"])

    def test_the_node_name_is_quoted(self):
        client = RecordingClient(fail=True)

        node_attachable_bridges(client, "pve/1")

        self.assertEqual(client.paths, ["nodes/pve%2F1/network?type=any_bridge"])

    def test_a_node_that_cannot_answer_yields_nothing_rather_than_raising(self):
        self.assertEqual(node_attachable_bridges(RecordingClient(fail=True), "pve1"), [])

    def test_an_unexpected_payload_yields_nothing(self):
        self.assertEqual(node_attachable_bridges(RecordingClient(payload={"iface": "vmbr0"}), "pve1"), [])

    def test_rows_without_a_name_are_dropped_rather_than_stringified(self):
        payload = [{"iface": "vmbr0", "type": "bridge"}, {"type": "bridge"}, {"iface": "", "type": "bridge"}, "junk"]

        self.assertEqual(node_attachable_bridges(RecordingClient(payload=payload), "pve1"), ["vmbr0"])

    def test_an_empty_node_costs_no_call(self):
        client = RecordingClient()

        self.assertEqual(node_attachable_bridges(client, ""), [])
        self.assertEqual(client.paths, [])


class CreateFormBridgeTests(TestCase):
    def setUp(self):
        self.cluster = ProxmoxCluster.objects.create(key="hq", display_name="HQ", enabled=True)

    def test_the_create_form_offers_only_what_the_selected_node_has(self):
        client = RecordingClient()
        with patch("core.services.guest_create._first_client", return_value=client):
            options = create_options("vm", "pve3", cluster=self.cluster)

        self.assertEqual(options["bridges"], ["dmz50", "server10", "vmbr0"])
        self.assertNotIn("wan100", options["bridges"])
        self.assertNotIn("cluster/sdn/vnets", client.paths)


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
            if path.name == "node_networks.py":
                continue
            yield path.relative_to(root), path.read_text()

    def test_only_node_networks_reads_the_cluster_vnet_list(self):
        offenders = [str(name) for name, text in self._sources() if "cluster/sdn/vnets" in text]

        self.assertEqual(
            offenders,
            [],
            "A cluster-wide SDN vnet list carries no node opinion. Attachability is "
            "`core.services.node_networks.node_attachable_bridges`, which asks the "
            f"provider per node; 5a4C owns the SDN domain itself: {', '.join(offenders)}",
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

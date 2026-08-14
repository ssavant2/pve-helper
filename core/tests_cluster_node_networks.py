"""Node network projection. Module 5 phase 5a4B-i.

Every decision branch in `core.services.cluster_node_networks` has a test here that
fails when the branch is deleted -- the phase's exit criterion is mutation, not
approval. The branches that carry the most weight are the ones the 2026-08-13 entry
round found missing from the artifact:

* a node whose second read failed publishes **nothing**, rather than publishing the
  first read's rows with `attachable=False` and a covered coverage row, which would
  be a node with proven-zero bridges;
* `present=False, unreachable=False` (proven gone) and `unreachable=True` (unknown)
  are different row states, and only a complete read may produce the first;
* the lane has its own lock, so a slow network pass cannot skip a membership cycle;
* `node_network` coverage rows stay out of `read_cluster_projection`.

The fixture is live `clusterhq` in miniature: `server10` is a realized vnet carrying
an address (so it appears in the plain listing typed `unknown`), `dmz50` is realized
without one (so it is absent from the plain listing entirely), and `wan100` belongs
to a zone restricted to pve1 -- the case that made the create form offer a bridge
that does not exist on pve3.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import (
    ClusterMembershipState,
    ClusterNodeEnrollment,
    ClusterNodeInterface,
    ClusterNodeState,
    ClusterProjectionCoverage,
    ProxmoxCluster,
    ProxmoxEndpoint,
)
from core.services.cluster_node_networks import (
    ERROR_INVALID_PAYLOAD,
    ERROR_NODE_NOT_PUBLISHED,
    refresh_cluster_node_networks,
    refresh_node_network,
)
from core.services.cluster_node_runtime import (
    ERROR_ACQUISITION_DISABLED,
    ERROR_ACQUISITION_QUARANTINED,
    ERROR_NO_ENABLED_ENDPOINT,
    ERROR_NODE_ABSENT,
    ERROR_NODE_OFFLINE,
    ERROR_PROVIDER,
    ERROR_TOPOLOGY_TRANSITION_PENDING,
)
from core.services.proxmox import ProxmoxAPIError, ProxmoxTransportError

PLAIN = {
    "pve1": [
        {
            "iface": "vmbr0",
            "type": "bridge",
            "active": 1,
            "autostart": 1,
            "method": "static",
            "address": "10.0.0.1",
            "cidr": "10.0.0.1/24",
            "gateway": "10.0.0.254",
            "bridge_ports": "bond0",
            "bridge_vids": "2-4094",
            "bridge_vlan_aware": 1,
        },
        {"iface": "vmbr1", "type": "bridge", "active": 1, "bridge_ports": "bond1", "bridge_vids": "100"},
        {"iface": "bond0", "type": "bond", "active": 1, "bond_mode": "802.3ad", "slaves": "nic0 nic1"},
        {"iface": "nic0", "type": "eth", "active": 1},
        # Realized vnet carrying an address. The plain listing types it `unknown`.
        {"iface": "server10", "type": "unknown", "active": 1, "address": "10.10.0.1"},
    ],
    "pve3": [
        {"iface": "vmbr0", "type": "bridge", "active": 1, "bridge_ports": "bond0"},
        {"iface": "bond0", "type": "bond", "active": 1},
        {"iface": "server10", "type": "unknown", "active": 1},
    ],
}

ANY_BRIDGE = {
    "pve1": [
        {"iface": "vmbr0", "type": "bridge", "active": 1},
        {"iface": "vmbr1", "type": "bridge", "active": 1},
        {"iface": "server10", "type": "vnet", "active": "1", "vlanaware": 1},
        # Realized, no address: absent from the plain listing above.
        {"iface": "dmz50", "type": "vnet", "active": "1", "comments": "DMZ"},
        # Zone `External`, restricted to pve1.
        {"iface": "wan100", "type": "vnet", "active": "1"},
    ],
    "pve3": [
        {"iface": "vmbr0", "type": "bridge", "active": 1},
        {"iface": "server10", "type": "vnet", "active": "1"},
    ],
}


def _cluster(key: str = "clusterhq", **fields) -> ProxmoxCluster:
    return ProxmoxCluster.objects.create(key=key, display_name=key, **fields)


def _endpoint(cluster: ProxmoxCluster, name: str = "endpoint") -> ProxmoxEndpoint:
    return ProxmoxEndpoint.objects.create(cluster=cluster, name=name, url=f"https://{name}.{cluster.key}.test:8006")


def _publish_membership(cluster: ProxmoxCluster, nodes: dict[str, bool], *, generation: int = 1) -> None:
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


def _enroll(cluster: ProxmoxCluster, modes: dict[str, str], *, generation: int = 1) -> None:
    for node_name, mode in modes.items():
        ClusterNodeEnrollment.objects.update_or_create(
            cluster=cluster,
            node_name=node_name,
            defaults={"mode": mode, "enrolled_at": timezone.now()},
        )
    ProxmoxCluster.objects.filter(pk=cluster.pk).update(enrollment_contract_version=1, enrollment_generation=generation)
    cluster.refresh_from_db()


class RecordingClient:
    """Serves the fixture and records every path, so call counts are assertable."""

    def __init__(self, *, plain=None, any_bridge=None, fail_on: str = "", error=None):
        self.paths: list[str] = []
        self._plain = PLAIN if plain is None else plain
        self._any_bridge = ANY_BRIDGE if any_bridge is None else any_bridge
        self._fail_on = fail_on
        self._error = error or ProxmoxTransportError("unreachable")

    def get(self, path, **kwargs):
        self.paths.append(path)
        if self._fail_on and self._fail_on in path:
            raise self._error
        node = path.split("/")[1]
        if path.endswith("?type=any_bridge"):
            return self._any_bridge.get(node, [])
        return self._plain.get(node, [])


def _coverage(cluster, node_name) -> ClusterProjectionCoverage:
    return ClusterProjectionCoverage.objects.get(
        cluster=cluster,
        domain=ClusterProjectionCoverage.DOMAIN_NODE_NETWORK,
        node_name=node_name,
    )


def _ifaces(cluster, node_name) -> dict[str, ClusterNodeInterface]:
    return {row.iface: row for row in ClusterNodeInterface.objects.filter(cluster=cluster, node_name=node_name)}


class NodeNetworkPublicationTests(TestCase):
    def setUp(self):
        self.cluster = _cluster()
        _endpoint(self.cluster)
        _publish_membership(self.cluster, {"pve1": True, "pve3": True})

    def _sweep(self, client=None):
        client = client or RecordingClient()
        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=client):
            return refresh_cluster_node_networks(self.cluster), client

    def test_it_publishes_both_reads_composed_into_one_row_set(self):
        result, client = self._sweep()

        self.assertTrue(result.ran)
        self.assertEqual(result.published, 2)
        rows = _ifaces(self.cluster, "pve1")
        self.assertEqual(
            sorted(rows),
            ["bond0", "dmz50", "nic0", "server10", "vmbr0", "vmbr1", "wan100"],
        )
        self.assertEqual(client.paths.count("nodes/pve1/network"), 1)
        self.assertEqual(client.paths.count("nodes/pve1/network?type=any_bridge"), 1)

    def test_attachability_comes_from_the_any_bridge_answer_and_nothing_else(self):
        self._sweep()

        rows = _ifaces(self.cluster, "pve1")
        attachable = sorted(name for name, row in rows.items() if row.attachable)
        self.assertEqual(attachable, ["dmz50", "server10", "vmbr0", "vmbr1", "wan100"])
        # A bond and a physical NIC are in the plain listing and are not targets.
        self.assertFalse(rows["bond0"].attachable)
        self.assertFalse(rows["nic0"].attachable)

    def test_a_realized_vnet_without_an_address_is_published(self):
        """The under-report. `dmz50` exists only in the `any_bridge` answer."""
        self._sweep()

        row = _ifaces(self.cluster, "pve1")["dmz50"]
        self.assertTrue(row.attachable)
        self.assertEqual(row.interface_type, "vnet")
        self.assertNotIn("dmz50", {entry["iface"] for entry in PLAIN["pve1"]})

    def test_a_zone_restricted_vnet_is_not_published_on_the_excluded_node(self):
        """The over-report. `wan100` on pve3 is what put guests on a missing bridge."""
        self._sweep()

        self.assertIn("wan100", _ifaces(self.cluster, "pve1"))
        self.assertNotIn("wan100", _ifaces(self.cluster, "pve3"))

    def test_type_is_taken_from_any_bridge_where_the_reads_overlap(self):
        """`server10` is `unknown` in the plain listing; storing that is the bug."""
        self._sweep()

        self.assertEqual(_ifaces(self.cluster, "pve1")["server10"].interface_type, "vnet")

    def test_host_only_interfaces_keep_their_plain_listing_attributes(self):
        self._sweep()

        vmbr0 = _ifaces(self.cluster, "pve1")["vmbr0"]
        self.assertEqual(vmbr0.bridge_ports, "bond0")
        self.assertEqual(vmbr0.bridge_vids, "2-4094")
        self.assertEqual(vmbr0.cidr, "10.0.0.1/24")
        self.assertEqual(vmbr0.gateway, "10.0.0.254")
        self.assertEqual(_ifaces(self.cluster, "pve1")["bond0"].bond_slaves, "nic0 nic1")

    def test_coverage_is_complete_and_generations_agree_with_the_rows(self):
        self._sweep()

        coverage = _coverage(self.cluster, "pve1")
        self.assertTrue(coverage.complete)
        self.assertEqual(coverage.error_code, "")
        self.assertEqual(coverage.based_on_generation, 1)
        self.assertEqual(coverage.generation, 1)
        self.assertTrue(all(row.observed_generation == 1 for row in _ifaces(self.cluster, "pve1").values()))

    def test_a_second_pass_advances_the_generation_in_step(self):
        self._sweep()
        self._sweep()

        self.assertEqual(_coverage(self.cluster, "pve1").generation, 2)
        self.assertEqual(_ifaces(self.cluster, "pve1")["vmbr0"].observed_generation, 2)

    def test_rows_carry_the_enrollment_generation_they_were_published_under(self):
        _enroll(self.cluster, {"pve1": ClusterNodeEnrollment.Mode.MANAGED}, generation=7)
        self._sweep()

        self.assertEqual(_ifaces(self.cluster, "pve1")["vmbr0"].based_on_enrollment_generation, 7)

    def test_a_string_zero_flag_is_false(self):
        """A vnet writes `active` as a string; `bool("0")` is True."""
        any_bridge = {"pve1": [{"iface": "vnet0", "type": "vnet", "active": "0"}], "pve3": []}
        self._sweep(RecordingClient(plain={"pve1": [], "pve3": []}, any_bridge=any_bridge))

        self.assertIs(_ifaces(self.cluster, "pve1")["vnet0"].active, False)

    def test_an_absent_flag_stays_unknown_rather_than_false(self):
        any_bridge = {"pve1": [{"iface": "vnet0", "type": "vnet"}], "pve3": []}
        self._sweep(RecordingClient(plain={"pve1": [], "pve3": []}, any_bridge=any_bridge))

        self.assertIsNone(_ifaces(self.cluster, "pve1")["vnet0"].active)

    def test_nameless_rows_are_dropped(self):
        plain = {"pve1": [{"type": "bridge"}, {"iface": "", "type": "bridge"}, "junk"], "pve3": []}
        self._sweep(RecordingClient(plain=plain, any_bridge={"pve1": [], "pve3": []}))

        self.assertEqual(_ifaces(self.cluster, "pve1"), {})


class NodeNetworkPartialFailureTests(TestCase):
    """The entry round's central finding: a half-failed node must publish nothing."""

    def setUp(self):
        self.cluster = _cluster()
        _endpoint(self.cluster)
        _publish_membership(self.cluster, {"pve1": True})
        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=RecordingClient()):
            refresh_cluster_node_networks(self.cluster)

    def _fail(self, fail_on: str, error=None):
        client = RecordingClient(fail_on=fail_on, error=error)
        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=client):
            return refresh_cluster_node_networks(self.cluster), client

    def test_a_failed_attachability_read_publishes_nothing_for_that_node(self):
        """Publishing the plain read alone would mark every row `attachable=False`
        under a complete coverage row -- a node with proven-zero bridges."""
        self._fail("?type=any_bridge")

        rows = _ifaces(self.cluster, "pve1")
        self.assertTrue(rows["vmbr0"].attachable)
        self.assertTrue(rows["dmz50"].attachable)
        self.assertEqual(rows["vmbr0"].observed_generation, 1)

    def test_a_failed_attachability_read_leaves_coverage_incomplete(self):
        self._fail("?type=any_bridge")

        coverage = _coverage(self.cluster, "pve1")
        self.assertFalse(coverage.complete)
        self.assertEqual(coverage.error_code, ERROR_PROVIDER)
        self.assertEqual(coverage.generation, 1)

    def test_a_failed_plain_read_publishes_nothing_either(self):
        client = RecordingClient(fail_on="nodes/pve1/network")
        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=client):
            refresh_cluster_node_networks(self.cluster)

        self.assertFalse(_coverage(self.cluster, "pve1").complete)
        self.assertEqual(client.paths, ["nodes/pve1/network"])

    def test_a_failed_pass_does_not_mark_rows_unknown(self):
        """One failed read is not proof the node is gone. The generation comparison
        already tells a consumer the rows are not current."""
        self._fail("?type=any_bridge")

        row = _ifaces(self.cluster, "pve1")["vmbr0"]
        self.assertTrue(row.present)
        self.assertFalse(row.unreachable)

    def test_a_failed_pass_preserves_the_previous_observed_at(self):
        before = _coverage(self.cluster, "pve1").observed_at
        self._fail("?type=any_bridge")

        coverage = _coverage(self.cluster, "pve1")
        self.assertEqual(coverage.observed_at, before)
        self.assertIsNotNone(coverage.attempted_at)

    def test_a_non_list_payload_is_its_own_error_code(self):
        client = RecordingClient(plain={"pve1": {"iface": "vmbr0"}}, any_bridge={"pve1": []})
        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=client):
            refresh_cluster_node_networks(self.cluster)

        self.assertEqual(_coverage(self.cluster, "pve1").error_code, ERROR_INVALID_PAYLOAD)

    def test_an_interface_removed_under_a_complete_read_is_proven_gone(self):
        """`present=False, unreachable=False` -- the state a failed read may never
        produce, and the only one that means the bridge was actually removed."""
        plain = {"pve1": [entry for entry in PLAIN["pve1"] if entry["iface"] != "vmbr1"]}
        any_bridge = {"pve1": [entry for entry in ANY_BRIDGE["pve1"] if entry["iface"] != "vmbr1"]}
        with patch(
            "core.services.cluster_node_networks.client_for_endpoint",
            return_value=RecordingClient(plain=plain, any_bridge=any_bridge),
        ):
            refresh_cluster_node_networks(self.cluster)

        row = _ifaces(self.cluster, "pve1")["vmbr1"]
        self.assertFalse(row.present)
        self.assertFalse(row.unreachable)
        self.assertFalse(row.attachable)


class NodeNetworkBoundaryTests(TestCase):
    def setUp(self):
        self.cluster = _cluster()
        _endpoint(self.cluster)
        _publish_membership(self.cluster, {"pve1": True, "pve3": True})

    def test_a_safety_only_node_is_never_contacted(self):
        """N5 is narrowed, not borrowed: a hidden node's disk blocks a delete, but a
        hidden node's bridge list has no consumer, so buying its rows is waste."""
        _enroll(
            self.cluster,
            {
                "pve1": ClusterNodeEnrollment.Mode.MANAGED,
                "pve3": ClusterNodeEnrollment.Mode.SAFETY_ONLY,
            },
        )
        client = RecordingClient()
        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=client):
            refresh_cluster_node_networks(self.cluster)

        self.assertFalse(any("pve3" in path for path in client.paths))
        self.assertEqual(_coverage(self.cluster, "pve3").error_code, ERROR_NODE_NOT_PUBLISHED)
        self.assertFalse(_coverage(self.cluster, "pve3").complete)
        self.assertTrue(_coverage(self.cluster, "pve1").complete)

    def test_hiding_a_published_node_retracts_its_rows_to_unknown(self):
        _enroll(
            self.cluster,
            {"pve1": ClusterNodeEnrollment.Mode.MANAGED, "pve3": ClusterNodeEnrollment.Mode.MANAGED},
        )
        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=RecordingClient()):
            refresh_cluster_node_networks(self.cluster)
        self.assertTrue(_ifaces(self.cluster, "pve3")["vmbr0"].attachable)

        _enroll(
            self.cluster,
            {"pve1": ClusterNodeEnrollment.Mode.MANAGED, "pve3": ClusterNodeEnrollment.Mode.SAFETY_ONLY},
            generation=2,
        )
        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=RecordingClient()):
            refresh_cluster_node_networks(self.cluster)

        row = _ifaces(self.cluster, "pve3")["vmbr0"]
        self.assertTrue(row.unreachable)
        self.assertFalse(row.present)
        self.assertFalse(row.attachable)

    def test_an_offline_node_costs_no_provider_call(self):
        _publish_membership(self.cluster, {"pve1": True, "pve3": False}, generation=2)
        client = RecordingClient()
        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=client):
            refresh_cluster_node_networks(self.cluster)

        self.assertFalse(any("pve3" in path for path in client.paths))
        self.assertEqual(_coverage(self.cluster, "pve3").error_code, ERROR_NODE_OFFLINE)

    def test_a_node_with_no_member_row_writes_nothing_at_all(self):
        client = RecordingClient()
        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=client):
            result = refresh_node_network(self.cluster, "pve9")

        self.assertFalse(result.published)
        self.assertEqual(client.paths, [])
        self.assertFalse(
            ClusterProjectionCoverage.objects.filter(
                cluster=self.cluster,
                domain=ClusterProjectionCoverage.DOMAIN_NODE_NETWORK,
                node_name="pve9",
            ).exists()
        )

    def test_one_node_failing_leaves_its_sibling_complete(self):
        client = RecordingClient(fail_on="pve3")
        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=client):
            result = refresh_cluster_node_networks(self.cluster)

        self.assertEqual(result.published, 1)
        self.assertTrue(_coverage(self.cluster, "pve1").complete)
        self.assertFalse(_coverage(self.cluster, "pve3").complete)
        self.assertTrue(_ifaces(self.cluster, "pve1")["vmbr0"].attachable)

    def test_a_departed_node_stops_presenting_attachable_bridges(self):
        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=RecordingClient()):
            refresh_cluster_node_networks(self.cluster)

        ClusterNodeState.objects.filter(cluster=self.cluster, node_name="pve3").update(present=False)
        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=RecordingClient()):
            result = refresh_cluster_node_networks(self.cluster)

        row = _ifaces(self.cluster, "pve3")["vmbr0"]
        self.assertTrue(row.unreachable)
        self.assertFalse(row.attachable)
        self.assertEqual(_coverage(self.cluster, "pve3").error_code, ERROR_NODE_ABSENT)
        # A node membership dropped is no longer a sweep target, so the retraction
        # comes from the departed pass rather than from the node loop.
        self.assertEqual(result.retracted, 1)

    def test_the_departed_sweep_is_idempotent(self):
        ClusterNodeInterface.objects.create(cluster=self.cluster, node_name="pve9", iface="vmbr0")
        ClusterNodeState.objects.create(
            cluster=self.cluster, node_name="pve9", present=False, online=False, membership_generation=1
        )

        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=RecordingClient()):
            first = refresh_cluster_node_networks(self.cluster)
        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=RecordingClient()):
            second = refresh_cluster_node_networks(self.cluster)

        self.assertEqual(first.retracted, 1)
        self.assertEqual(second.retracted, 0)

    def test_a_node_hidden_mid_sweep_is_not_contacted(self):
        """The boundary is re-resolved per node, not snapshotted at the top.

        A twenty-node pass is minutes long, and the enrollment change that arrives
        during it is the operator saying "stop touching that node".
        """
        _enroll(
            self.cluster,
            {"pve1": ClusterNodeEnrollment.Mode.MANAGED, "pve3": ClusterNodeEnrollment.Mode.MANAGED},
        )
        client = RecordingClient()
        original_get = client.get

        def hide_pve3_after_the_first_node(path, **kwargs):
            if path.startswith("nodes/pve1/"):
                _enroll(
                    self.cluster,
                    {"pve1": ClusterNodeEnrollment.Mode.MANAGED, "pve3": ClusterNodeEnrollment.Mode.SAFETY_ONLY},
                    generation=2,
                )
            return original_get(path, **kwargs)

        client.get = hide_pve3_after_the_first_node
        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=client):
            refresh_cluster_node_networks(self.cluster)

        self.assertFalse(any("pve3" in path for path in client.paths))
        self.assertEqual(_coverage(self.cluster, "pve3").error_code, ERROR_NODE_NOT_PUBLISHED)

    def test_a_node_hidden_before_it_departs_still_reads_as_departed(self):
        """Idempotence keys on the coverage reason, not on rows touched.

        Keying on rows left this node reporting `node_not_published` -- "pve-helper
        chose not to publish this" -- for a node that had left the cluster.
        """
        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=RecordingClient()):
            refresh_cluster_node_networks(self.cluster)
        _enroll(
            self.cluster,
            {"pve1": ClusterNodeEnrollment.Mode.MANAGED, "pve3": ClusterNodeEnrollment.Mode.SAFETY_ONLY},
            generation=2,
        )
        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=RecordingClient()):
            refresh_cluster_node_networks(self.cluster)
        self.assertEqual(_coverage(self.cluster, "pve3").error_code, ERROR_NODE_NOT_PUBLISHED)

        ClusterNodeState.objects.filter(cluster=self.cluster, node_name="pve3").update(present=False)
        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=RecordingClient()):
            result = refresh_cluster_node_networks(self.cluster)

        self.assertEqual(_coverage(self.cluster, "pve3").error_code, ERROR_NODE_ABSENT)
        self.assertEqual(result.retracted, 1)
        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=RecordingClient()):
            self.assertEqual(refresh_cluster_node_networks(self.cluster).retracted, 0)

    def test_a_retired_cluster_is_a_zero_call_refusal(self):
        ProxmoxCluster.objects.filter(pk=self.cluster.pk).update(
            retired_at=timezone.now(),
            enabled=False,
            retirement_mode=ProxmoxCluster.RetirementMode.VERIFIED,
        )
        self.cluster.refresh_from_db()
        client = RecordingClient()

        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=client):
            result = refresh_cluster_node_networks(self.cluster)

        self.assertFalse(result.ran)
        self.assertEqual(client.paths, [])


class NodeNetworkClusterRefusalTests(TestCase):
    """A cluster-grain refusal must not leave the rows reading as current.

    Currency here is generation equality and nothing else -- no age rule -- so a
    refusal that writes nothing at all leaves coverage `complete` at the last good
    generation forever. A disabled, quarantined or endpoint-less connection still
    renders in the workspace (`managed_clusters()` keeps all three), so "forever" is
    not hypothetical; only retirement takes the projection away.
    """

    def setUp(self):
        self.cluster = _cluster()
        self.endpoint = _endpoint(self.cluster)
        _publish_membership(self.cluster, {"pve1": True, "pve3": True})
        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=RecordingClient()):
            refresh_cluster_node_networks(self.cluster)
        self.assertTrue(_coverage(self.cluster, "pve1").complete)

    def _sweep_again(self):
        client = RecordingClient()
        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=client):
            return refresh_cluster_node_networks(self.cluster), client

    def test_a_disabled_connection_stops_its_rows_reading_as_current(self):
        ProxmoxCluster.objects.filter(pk=self.cluster.pk).update(enabled=False)

        result, client = self._sweep_again()

        self.assertFalse(result.ran)
        self.assertEqual(client.paths, [])
        self.assertFalse(_coverage(self.cluster, "pve1").complete)
        self.assertEqual(_coverage(self.cluster, "pve1").error_code, ERROR_ACQUISITION_DISABLED)

    def test_a_quarantined_connection_stops_its_rows_reading_as_current(self):
        ProxmoxCluster.objects.filter(pk=self.cluster.pk).update(ingestion_quarantined=True)

        self._sweep_again()

        self.assertFalse(_coverage(self.cluster, "pve1").complete)
        self.assertEqual(_coverage(self.cluster, "pve1").error_code, ERROR_ACQUISITION_QUARANTINED)

    def test_losing_every_enabled_endpoint_stops_the_rows_reading_as_current(self):
        ProxmoxEndpoint.objects.filter(pk=self.endpoint.pk).update(enabled=False)

        self._sweep_again()

        self.assertFalse(_coverage(self.cluster, "pve1").complete)
        self.assertEqual(_coverage(self.cluster, "pve1").error_code, ERROR_NO_ENABLED_ENDPOINT)

    def test_a_topology_transition_stops_the_rows_reading_as_current(self):
        ClusterMembershipState.objects.filter(cluster=self.cluster).update(
            transition_pending=True, pending_topology_role="standalone"
        )

        self._sweep_again()

        self.assertFalse(_coverage(self.cluster, "pve1").complete)
        self.assertEqual(_coverage(self.cluster, "pve1").error_code, ERROR_TOPOLOGY_TRANSITION_PENDING)

    def test_the_previous_generation_and_rows_are_left_intact(self):
        """Demotion, not retraction: the rows keep their values and go non-current.

        A refused pass is not proof that a bridge is gone, and the consumer needs the
        difference between "stale" and "this node has no bridges".
        """
        before = _coverage(self.cluster, "pve1")
        ProxmoxCluster.objects.filter(pk=self.cluster.pk).update(enabled=False)

        self._sweep_again()

        after = _coverage(self.cluster, "pve1")
        self.assertEqual(after.generation, before.generation)
        self.assertEqual(after.observed_at, before.observed_at)
        row = _ifaces(self.cluster, "pve1")["vmbr0"]
        self.assertTrue(row.present)
        self.assertFalse(row.unreachable)
        self.assertEqual(row.observed_generation, before.generation)

    def test_a_retired_connection_writes_nothing(self):
        """The one refusal that must stay silent: retirement deletes this projection
        under the same lock, so a write here resurrects a row the finalizer counted."""
        ProxmoxCluster.objects.filter(pk=self.cluster.pk).update(
            retired_at=timezone.now(),
            enabled=False,
            retirement_mode=ProxmoxCluster.RetirementMode.VERIFIED,
        )

        self._sweep_again()

        self.assertTrue(_coverage(self.cluster, "pve1").complete)

    def test_the_single_node_seam_refuses_the_same_way(self):
        ProxmoxCluster.objects.filter(pk=self.cluster.pk).update(enabled=False)
        self.cluster.refresh_from_db()

        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=RecordingClient()):
            result = refresh_node_network(self.cluster, "pve1")

        self.assertEqual(result.error_code, ERROR_ACQUISITION_DISABLED)
        self.assertFalse(_coverage(self.cluster, "pve1").complete)


class NodeNetworkEndpointHealthTests(TestCase):
    """A dead relay costs one timeout per sweep, not one per node.

    This domain holds the cluster lifecycle lock across two reads per node, so an
    endpoint that will never answer is not merely slow: every wasted timeout is held
    lock, and the membership lane and retirement queue behind it.
    """

    def setUp(self):
        self.cluster = _cluster()
        self.dead = _endpoint(self.cluster, "dead")
        self.live = _endpoint(self.cluster, "live")
        _publish_membership(self.cluster, {"pve1": True, "pve3": True})

    def _sweep(self, error):
        dead = RecordingClient(fail_on="nodes/", error=error)
        live = RecordingClient()

        def route(endpoint):
            return dead if endpoint.name == "dead" else live

        with patch("core.services.cluster_node_networks.client_for_endpoint", side_effect=route):
            result = refresh_cluster_node_networks(self.cluster)
        return result, dead, live

    def test_an_unreachable_endpoint_is_tried_once_and_then_skipped(self):
        result, dead, _live = self._sweep(ProxmoxTransportError("connect refused", request_sent=False))

        self.assertEqual(result.published, 2)
        self.assertEqual(len(dead.paths), 1, "a condemned endpoint must not be asked again for the next node")

    def test_a_rejected_credential_condemns_the_endpoint_for_the_whole_sweep(self):
        result, dead, _live = self._sweep(ProxmoxAPIError("unauthorized", status_code=401))

        self.assertEqual(result.published, 2)
        self.assertEqual(len(dead.paths), 1)

    def test_an_ambiguous_failure_does_not_condemn_on_first_sight(self):
        """A delivered request that timed out says nothing certain about the relay;
        condemning it on one node would strand a cluster whose other endpoint is worse."""
        _result, dead, _live = self._sweep(ProxmoxTransportError("read timeout"))

        self.assertGreater(len(dead.paths), 1)


class NodeNetworkLaneTests(TestCase):
    """The lane decision, asserted rather than described."""

    def test_the_node_network_lock_is_not_the_host_projection_lock(self):
        from core.services.host_projection_singleflight import (
            HOST_PROJECTION_REFRESH_LOCK_ID,
            NODE_NETWORK_REFRESH_LOCK_ID,
        )

        self.assertNotEqual(HOST_PROJECTION_REFRESH_LOCK_ID, NODE_NETWORK_REFRESH_LOCK_ID)

    def test_a_running_pass_is_skipped_rather_than_queued(self):
        from core import tasks

        cluster = _cluster()
        with patch("core.tasks.node_network_refresh_lock") as lock:
            lock.return_value.__enter__.return_value = False
            result = tasks._refresh_cluster_node_networks(cluster)

        self.assertTrue(result["skipped"])

    def test_one_cluster_failing_does_not_stop_a_sibling(self):
        from core import tasks

        _cluster("clustera")
        _cluster("clusterb")
        calls = []

        def refresh(cluster):
            calls.append(cluster.key)
            if cluster.key == "clustera":
                raise ProxmoxAPIError("boom")
            return MagicMock(ran=True, refusal="", targets=0, published=0, failed=0, retracted=0)

        with patch("core.tasks.refresh_cluster_node_networks", side_effect=refresh):
            result = tasks.refresh_node_network_projection()

        self.assertEqual(calls, ["clustera", "clusterb"])
        self.assertEqual(result["clusters"][0]["error"], "unhandled")
        self.assertTrue(result["clusters"][1]["ran"])

    @override_settings(NODE_NETWORK_REFRESH_INTERVAL_MINUTES=15)
    def test_the_schedule_is_separate_and_slower_than_the_host_projection(self):
        from django.conf import settings

        from core.services.host_projection_refresh_schedule import (
            HOST_PROJECTION_REFRESH_SCHEDULE_NAME,
            NODE_NETWORK_REFRESH_FUNC,
            ensure_node_network_refresh_schedule,
        )

        schedule = ensure_node_network_refresh_schedule()

        self.assertEqual(schedule.func, NODE_NETWORK_REFRESH_FUNC)
        self.assertEqual(schedule.minutes, 15)
        self.assertNotEqual(schedule.name, HOST_PROJECTION_REFRESH_SCHEDULE_NAME)
        self.assertGreater(schedule.minutes, settings.HOST_PROJECTION_REFRESH_INTERVAL_MINUTES)


class NodeNetworkProjectionReadIsolationTests(TestCase):
    """The new domain's rows must not enter the membership/runtime read."""

    def test_node_network_coverage_is_not_loaded_by_the_projection_read(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from core.services.cluster_projection_read import read_cluster_projection

        cluster = _cluster()
        _endpoint(cluster)
        _publish_membership(cluster, {"pve1": True, "pve3": True})
        with patch("core.services.cluster_node_networks.client_for_endpoint", return_value=RecordingClient()):
            refresh_cluster_node_networks(cluster)

        with CaptureQueriesContext(connection) as captured:
            read_cluster_projection(cluster.key)

        coverage_reads = [
            query["sql"] for query in captured.captured_queries if "clusterprojectioncoverage" in query["sql"]
        ]
        self.assertTrue(coverage_reads)
        self.assertTrue(all("node_network" not in sql for sql in coverage_reads))

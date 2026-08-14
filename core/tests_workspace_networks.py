"""The workspace Networks tab (phase 5a4B-ii).

The tab composes an already-published projection, so what it can break is not the
sweep — it is what the composition *claims*. Three claims, each with a test:

* the grain is (node, interface) and never collapses across nodes, because `vmbr0`
  on two nodes is two devices that share a name;
* a node whose state is not current says why, rather than rendering an empty table
  that reads as "this node has no network";
* the publication boundary, asserted here as a leak test rather than re-derived.

The query test at the end is the one that would catch the regression this phase
exists to remove: a page that renders every node must not turn into a read per node.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import resolve, reverse
from django.utils import timezone

from core.models import (
    ClusterMembershipState,
    ClusterNodeEnrollment,
    ClusterNodeInterface,
    ClusterNodeState,
    ClusterProjectionCoverage,
    ProxmoxCluster,
)
from core.services.cluster_projection_read import NodeNetworkReadStatus
from core.services.workspace_networks import network_panel

MEMBERS = ("pve1", "pve2", "pve3")


@override_settings(APP_REQUIRE_LOGIN=False)
class WorkspaceNetworkTabTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.cluster = ProxmoxCluster.objects.create(key="hq", display_name="HQ", enabled=True)
        ClusterMembershipState.objects.create(
            cluster=self.cluster,
            membership_generation=3,
            member_count=3,
            quorate=True,
            observed_from="pve1",
            topology_role="corosync",
        )
        ClusterProjectionCoverage.objects.create(
            cluster=self.cluster,
            domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP,
            node_name=None,
            generation=3,
            based_on_generation=None,
            complete=True,
            attempted_at=self.now,
            observed_at=self.now,
        )
        for index, node in enumerate(MEMBERS, start=1):
            ClusterNodeState.objects.create(
                cluster=self.cluster,
                node_name=node,
                nodeid=index,
                present=True,
                online=True,
                membership_generation=3,
            )
        for node in MEMBERS:
            self._coverage(node)
            # The same name on every node, deliberately: this is the row a
            # cluster-wide table would merge into one.
            self._interface(node, "vmbr0", attachable=True, bridge_ports=f"{node}-eno1")
        self._interface("pve1", "bond0", attachable=False, interface_type="bond")

        user = get_user_model().objects.create_user(username="nets", password="nets-pw")
        self.client = Client()
        self.client.force_login(user)

    # ------------------------------------------------------------------ helpers

    def _coverage(self, node, *, generation=5, complete=True, error=""):
        ClusterProjectionCoverage.objects.update_or_create(
            cluster=self.cluster,
            domain=ClusterProjectionCoverage.DOMAIN_NODE_NETWORK,
            node_name=node,
            defaults={
                "generation": generation,
                "based_on_generation": 3,
                "complete": complete,
                "attempted_at": self.now,
                "observed_at": self.now,
                "error_code": error,
            },
        )

    def _interface(self, node, iface, **kwargs):
        defaults = {
            "interface_type": "bridge",
            "attachable": True,
            "active": True,
            "present": True,
            "unreachable": False,
            "observed_generation": 5,
            "last_seen_at": self.now,
        }
        defaults.update(kwargs)
        return ClusterNodeInterface.objects.update_or_create(
            cluster=self.cluster, node_name=node, iface=iface, defaults=defaults
        )[0]

    def _activate(self, **modes):
        for node_name, mode in modes.items():
            ClusterNodeEnrollment.objects.create(
                cluster=self.cluster, node_name=node_name, mode=mode, enrolled_at=self.now
            )
        self.cluster.enrollment_contract_version = 1
        self.cluster.save(update_fields=["enrollment_contract_version"])

    def _panel(self, **kwargs):
        return network_panel(self.cluster, members=MEMBERS, **kwargs)

    # ------------------------------------------------------------------- shape

    def test_the_cluster_scope_is_one_group_per_node_and_never_one_merged_list(self):
        panel = self._panel()

        self.assertEqual([group.node for group in panel.groups], list(MEMBERS))
        self.assertEqual([row.iface for row in panel.groups[0].interfaces], ["bond0", "vmbr0"])
        # Three nodes each carrying `vmbr0` is three rows, with three sets of ports.
        self.assertEqual(
            sorted(row.bridge_ports for group in panel.groups for row in group.interfaces if row.iface == "vmbr0"),
            ["pve1-eno1", "pve2-eno1", "pve3-eno1"],
        )

    def test_the_node_scope_shows_only_that_node(self):
        panel = self._panel(node="pve2")

        self.assertEqual([group.node for group in panel.groups], ["pve2"])
        self.assertEqual(panel.interface_count, 1)

    def test_attachability_is_read_off_the_published_flag(self):
        panel = self._panel(node="pve1")

        self.assertEqual([row.iface for row in panel.groups[0].attachable], ["vmbr0"])

    # -------------------------------------------------------------- not current

    def test_a_node_that_was_never_swept_says_so_instead_of_rendering_empty(self):
        ClusterProjectionCoverage.objects.filter(
            domain=ClusterProjectionCoverage.DOMAIN_NODE_NETWORK, node_name="pve2"
        ).delete()
        ClusterNodeInterface.objects.filter(node_name="pve2").delete()

        group = next(item for item in self._panel().groups if item.node == "pve2")

        self.assertIs(group.status, NodeNetworkReadStatus.MISSING)
        self.assertFalse(group.known)
        self.assertEqual(group.reason, "its network has not been read yet")

    def test_a_failed_pass_keeps_its_rows_and_marks_the_node_unknown(self):
        self._coverage("pve2", complete=False, error="provider_timeout")

        group = next(item for item in self._panel().groups if item.node == "pve2")

        self.assertIs(group.status, NodeNetworkReadStatus.FAILED)
        self.assertFalse(group.known)
        self.assertEqual(group.reason, "the node timed out")
        self.assertEqual([row.iface for row in group.interfaces], ["vmbr0"])

    def test_the_tab_and_the_migrate_dialog_give_one_reason_not_two(self):
        from core.services.node_networks import attachable_bridges

        self._coverage("pve2", complete=False, error="acquisition_quarantined")
        group = next(item for item in self._panel().groups if item.node == "pve2")

        self.assertEqual(group.reason, attachable_bridges(self.cluster, "pve2").reason)

    def test_a_tombstone_stays_visible_and_stays_distinguishable_from_silence(self):
        self._interface("pve1", "vmbr9", present=False, unreachable=False)
        self._interface("pve1", "vmbr8", present=False, unreachable=True)

        group = next(item for item in self._panel().groups if item.node == "pve1")

        self.assertIn("vmbr9", [row.iface for row in group.interfaces])
        self.assertEqual([row.iface for row in group.gone], ["vmbr9"])

    def test_a_tombstoned_bridge_is_no_longer_counted_as_attachable(self):
        """The heading counts what a guest could attach to today, not what it could
        have attached to last week. A bridge that is gone is not a target."""
        self._interface("pve1", "vmbr7", attachable=True, present=False, unreachable=False)

        group = next(item for item in self._panel().groups if item.node == "pve1")

        self.assertIn("vmbr7", [row.iface for row in group.interfaces])
        self.assertEqual([row.iface for row in group.attachable], ["vmbr0"])

    def test_a_row_from_an_older_generation_renders_as_not_current(self):
        self._interface("pve1", "vmbr0", observed_generation=4)

        group = next(item for item in self._panel().groups if item.node == "pve1")
        row = next(item for item in group.interfaces if item.iface == "vmbr0")

        self.assertFalse(row.current)

    # ------------------------------------------------------ publication boundary

    def test_a_hidden_nodes_interfaces_never_reach_the_tab(self):
        self._activate(
            pve1=ClusterNodeEnrollment.Mode.MANAGED,
            pve2=ClusterNodeEnrollment.Mode.SAFETY_ONLY,
            pve3=ClusterNodeEnrollment.Mode.MANAGED,
        )

        panel = self._panel()

        self.assertEqual([group.node for group in panel.groups], ["pve1", "pve3"])
        self.assertEqual(panel.unread_nodes, ())

    def test_a_member_this_connection_does_not_read_is_footnoted(self):
        self._activate(pve1=ClusterNodeEnrollment.Mode.MANAGED, pve2=ClusterNodeEnrollment.Mode.MANAGED)

        self.assertEqual(self._panel().unread_nodes, ("pve3",))

    def test_a_legacy_connection_footnotes_nothing(self):
        self.assertEqual(self._panel().unread_nodes, ())

    def test_a_hidden_nodes_network_tab_is_not_found(self):
        self._activate(
            pve1=ClusterNodeEnrollment.Mode.MANAGED,
            pve2=ClusterNodeEnrollment.Mode.SAFETY_ONLY,
            pve3=ClusterNodeEnrollment.Mode.MANAGED,
        )

        response = self.client.get(reverse("core:node_networks", args=["hq", "pve2"]))

        self.assertEqual(response.status_code, 404)

    # --------------------------------------------------------------- the surface

    def test_both_tabs_render_and_are_reachable_from_the_strip(self):
        for url, active in (
            (reverse("core:cluster_networks", args=["hq"]), "networks"),
            (reverse("core:node_networks", args=["hq", "pve1"]), "networks"),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)

                self.assertEqual(response.status_code, 200)
                tabs = {tab.key: tab for tab in response.context["tabs"]}
                self.assertTrue(tabs["networks"].enabled)
                self.assertTrue(tabs[active].active)
                self.assertContains(response, "vmbr0")

    def test_each_node_panel_uses_the_shared_gui_spacing(self):
        response = self.client.get(reverse("core:cluster_networks", args=["hq"]))

        self.assertContains(response, 'class="panel panel-spaced"', count=len(MEMBERS))

    def test_the_cluster_tab_names_every_node_it_could_not_read(self):
        self._coverage("pve3", complete=False, error="endpoints_exhausted")

        response = self.client.get(reverse("core:cluster_networks", args=["hq"]))

        self.assertContains(response, "no endpoint answered during the last sweep")

    def test_a_cluster_retired_between_the_two_reads_is_a_404_not_a_500(self):
        """The tab resolves the managed cluster twice. A retirement landing in
        between must end the request, not render a page about a gone object."""
        from core.services.cluster_projection_read import ClusterProjectionNotFound

        with patch(
            "core.views.clusters.workspace.network_panel",
            side_effect=ClusterProjectionNotFound("hq"),
        ):
            cluster = self.client.get(reverse("core:cluster_networks", args=["hq"]))
            node = self.client.get(reverse("core:node_networks", args=["hq", "pve1"]))

        self.assertEqual(cluster.status_code, 404)
        self.assertEqual(node.status_code, 404)

    def test_the_routes_resolve_to_the_workspace_views(self):
        self.assertEqual(resolve("/clusters/hq/networks/").view_name, "core:cluster_networks")
        self.assertEqual(resolve("/clusters/hq/nodes/pve1/networks/").view_name, "core:node_networks")

    def test_the_composition_is_flat_in_node_count(self):
        with self.assertNumQueries(4):
            network_panel(self.cluster, members=MEMBERS)

        for index in range(4, 12):
            name = f"pve{index}"
            ClusterNodeState.objects.create(
                cluster=self.cluster, node_name=name, nodeid=index, present=True, online=True, membership_generation=3
            )
            self._coverage(name)
            self._interface(name, "vmbr0")

        with self.assertNumQueries(4):
            network_panel(self.cluster, members=tuple(f"pve{index}" for index in range(1, 12)))

    def test_rendering_the_tab_makes_no_provider_call(self):
        """The default test settings block every unmocked Proxmox request, so a
        200 with eleven nodes on the page is the assertion."""
        for index in range(4, 12):
            name = f"pve{index}"
            ClusterNodeState.objects.create(
                cluster=self.cluster, node_name=name, nodeid=index, present=True, online=True, membership_generation=3
            )
            self._coverage(name)
            self._interface(name, "vmbr0")

        response = self.client.get(reverse("core:cluster_networks", args=["hq"]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "pve11")

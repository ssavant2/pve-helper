"""The Hosts & Clusters shell and tree (5a2A+B).

What this phase can break is navigation: an object that should not be reachable,
a node leaf that should not exist, or a route that shadows a sibling. Each of those
has a test here. The Summary *bodies* are 5a2C/5a2D and are deliberately not
asserted beyond the shell they hang in.
"""

from __future__ import annotations

from django.test import Client, TestCase, override_settings
from django.urls import resolve, reverse
from django.utils import timezone

from core.models import (
    ClusterMembershipState,
    ClusterNodeEnrollment,
    ClusterNodeState,
    ClusterProjectionCoverage,
    ProxmoxCluster,
)
from core.services.workspace_nav import cluster_nav_key, node_nav_key, workspace_nav


def _cluster(key: str, *, role: str = "corosync", nodes=("pve1", "pve2"), generation: int = 3, **kwargs):
    cluster = ProxmoxCluster.objects.create(
        key=key,
        display_name=key.upper(),
        enabled=kwargs.pop("enabled", True),
        **kwargs,
    )
    now = timezone.now()
    ClusterMembershipState.objects.create(
        cluster=cluster,
        membership_generation=generation,
        member_count=len(nodes),
        quorate=True,
        observed_from=nodes[0] if nodes else "",
        topology_role=role,
    )
    ClusterProjectionCoverage.objects.create(
        cluster=cluster,
        domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP,
        node_name=None,
        generation=generation,
        based_on_generation=None,
        complete=True,
        attempted_at=now,
        observed_at=now,
        error_code="",
    )
    for index, node in enumerate(nodes, start=1):
        ClusterNodeState.objects.create(
            cluster=cluster,
            node_name=node,
            nodeid=index,
            present=True,
            online=True,
            membership_generation=generation,
        )
    return cluster


def _retire(cluster) -> None:
    """A retired cluster is disabled and carries a mode; the DB enforces both."""
    cluster.enabled = False
    cluster.retired_at = timezone.now()
    cluster.retirement_mode = ProxmoxCluster.RetirementMode.VERIFIED
    cluster.save(update_fields=["enabled", "retired_at", "retirement_mode"])


def _activate(cluster, **modes):
    for node_name, mode in modes.items():
        ClusterNodeEnrollment.objects.create(
            cluster=cluster,
            node_name=node_name,
            mode=mode,
            enrolled_at=timezone.now(),
        )
    cluster.enrollment_contract_version = 1
    cluster.save(update_fields=["enrollment_contract_version"])


class WorkspaceNavTests(TestCase):
    def test_corosync_clusters_and_standalone_hosts_are_separate_groups(self):
        _cluster("hq", role="corosync", nodes=("pve1", "pve2"))
        _cluster("edge", role="standalone", nodes=("edge1",))

        tree = workspace_nav()

        self.assertEqual([entry.cluster_key for entry in tree["clusters"]], ["hq"])
        self.assertEqual([entry.cluster_key for entry in tree["hosts"]], ["edge"])

    def test_a_one_node_corosync_cluster_is_still_a_cluster(self):
        """The distinction is the corosync row, not the node count."""
        _cluster("tiny", role="corosync", nodes=("solo1",))

        tree = workspace_nav()

        self.assertEqual([entry.cluster_key for entry in tree["clusters"]], ["tiny"])
        self.assertEqual(tree["hosts"], [])

    def test_an_unreadable_topology_stays_under_clusters(self):
        """Unknown is an absence of evidence, not a third topology."""
        _cluster("murky", role="unknown", nodes=("pve1",))

        tree = workspace_nav()

        self.assertEqual([entry.cluster_key for entry in tree["clusters"]], ["murky"])

    def test_a_retired_cluster_never_enters_the_tree(self):
        retired = _cluster("gone", nodes=("pve1",))
        _retire(retired)

        tree = workspace_nav()

        self.assertEqual(tree["clusters"], [])
        self.assertEqual(tree["hosts"], [])

    def test_a_disabled_cluster_stays_with_its_reason(self):
        """Disabling prepares a verified retirement; hiding it would delete the
        very inventory the operator is deciding about."""
        _cluster("paused", nodes=("pve1",), enabled=False)

        [entry] = workspace_nav()["clusters"]

        self.assertEqual(entry.cluster_key, "paused")
        self.assertTrue(entry.degraded)

    def test_only_managed_nodes_become_leaves(self):
        cluster = _cluster("hq", nodes=("pve1", "pve2", "pve3"))
        _activate(cluster, pve1="managed", pve2="safety_only")

        [entry] = workspace_nav()["clusters"]

        self.assertEqual([node.node_name for node in entry.nodes], ["pve1"])

    def test_a_legacy_cluster_lists_every_member(self):
        _cluster("legacy", nodes=("pve1", "pve2"))

        [entry] = workspace_nav()["clusters"]

        self.assertEqual([node.node_name for node in entry.nodes], ["pve1", "pve2"])

    def test_an_absent_node_is_not_a_leaf(self):
        cluster = _cluster("hq", nodes=("pve1", "pve2"))
        ClusterNodeState.objects.filter(cluster=cluster, node_name="pve2").update(present=False)

        [entry] = workspace_nav()["clusters"]

        self.assertEqual([node.node_name for node in entry.nodes], ["pve1"])

    def test_node_nav_keys_are_cluster_qualified(self):
        """Two clusters routinely both have a pve1; a bare name would light both."""
        _cluster("a", nodes=("pve1",))
        _cluster("b", nodes=("pve1",))

        keys = {node.nav_key for entry in workspace_nav()["clusters"] for node in entry.nodes}

        self.assertEqual(keys, {node_nav_key("a", "pve1"), node_nav_key("b", "pve1")})


@override_settings(APP_REQUIRE_LOGIN=False)
class WorkspaceRouteTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.cluster = _cluster("hq", nodes=("pve1", "pve2"))

    def test_canonical_routes_reverse_and_resolve(self):
        from core.views.clusters import workspace

        cluster_url = reverse("core:cluster_summary", args=["hq"])
        node_url = reverse("core:node_summary", args=["hq", "pve1"])

        self.assertEqual(cluster_url, "/clusters/hq/summary/")
        self.assertEqual(node_url, "/clusters/hq/nodes/pve1/summary/")
        self.assertIs(resolve(cluster_url).func, workspace.cluster_summary)
        self.assertIs(resolve(node_url).func, workspace.node_summary)

    def test_the_new_routes_do_not_shadow_their_literal_siblings(self):
        """`connection/`, `nodes/add/` and `nodes/activate/` are exact literals and
        must keep winning over the node capture."""
        from core.views.clusters import connections, enrollment

        self.assertIs(resolve("/clusters/hq/connection/").func, connections.cluster_connection)
        self.assertIs(resolve("/clusters/hq/nodes/add/").func, enrollment.cluster_node_add)
        self.assertIs(resolve("/clusters/hq/nodes/activate/").func, enrollment.cluster_enrollment_activate)

    def test_a_node_named_like_a_literal_still_resolves(self):
        from core.views.clusters import workspace

        match = resolve("/clusters/hq/nodes/add/summary/")

        self.assertIs(match.func, workspace.node_summary)
        self.assertEqual(match.kwargs["node"], "add")

    def test_the_shell_renders_with_zero_provider_calls(self):
        from unittest.mock import patch

        with (
            patch("core.services.proxmox.ProxmoxClient.__init__", side_effect=AssertionError("provider call")),
            patch("core.services.proxmox.ProxmoxClient.get", side_effect=AssertionError("provider call")),
        ):
            cluster_response = self.client.get(reverse("core:cluster_summary", args=["hq"]))
            node_response = self.client.get(reverse("core:node_summary", args=["hq", "pve1"]))

        self.assertEqual(cluster_response.status_code, 200)
        self.assertEqual(node_response.status_code, 200)

    def test_only_summary_is_an_enabled_tab(self):
        response = self.client.get(reverse("core:cluster_summary", args=["hq"]))

        enabled = [tab.key for tab in response.context["tabs"] if tab.enabled]
        self.assertEqual(enabled, ["summary"])
        # The shape is still stated, so the workspace does not silently shrink.
        self.assertIn("datastores", [tab.key for tab in response.context["tabs"]])

    def test_the_active_leaf_is_the_object_being_viewed(self):
        response = self.client.get(reverse("core:node_summary", args=["hq", "pve1"]))

        self.assertEqual(response.context["workspace_nav_key"], node_nav_key("hq", "pve1"))
        self.assertEqual(
            self.client.get(reverse("core:cluster_summary", args=["hq"])).context["workspace_nav_key"],
            cluster_nav_key("hq"),
        )

    def test_a_retired_cluster_has_no_workspace_page(self):
        _retire(self.cluster)

        self.assertEqual(self.client.get(reverse("core:cluster_summary", args=["hq"])).status_code, 404)
        self.assertEqual(self.client.get(reverse("core:node_summary", args=["hq", "pve1"])).status_code, 404)

    def test_a_hidden_node_has_no_page_even_by_typed_url(self):
        """The filtered tree is navigation; this is the boundary. An operator who
        hid a node must not reach its runtime by typing the URL."""
        _activate(self.cluster, pve1="managed", pve2="safety_only")

        self.assertEqual(self.client.get(reverse("core:node_summary", args=["hq", "pve1"])).status_code, 200)
        self.assertEqual(self.client.get(reverse("core:node_summary", args=["hq", "pve2"])).status_code, 404)

    def test_a_cluster_retired_mid_request_is_404_not_a_half_rendered_page(self):
        """The path resolver and the projection read are two lookups, and a
        retirement can land between them. A workspace rendered for an object that
        no longer exists is worse than a 404."""
        from unittest.mock import patch

        from core.services.cluster_projection_read import ClusterProjectionNotFound

        with patch(
            "core.views.clusters.workspace.read_cluster_projection",
            side_effect=ClusterProjectionNotFound("retired mid-request"),
        ):
            cluster_response = self.client.get(reverse("core:cluster_summary", args=["hq"]))
            node_response = self.client.get(reverse("core:node_summary", args=["hq", "pve1"]))

        self.assertEqual(cluster_response.status_code, 404)
        self.assertEqual(node_response.status_code, 404)

    def test_an_unknown_node_is_404_not_an_empty_page(self):
        self.assertEqual(self.client.get(reverse("core:node_summary", args=["hq", "nope"])).status_code, 404)

    def test_the_sidebar_offers_the_workspace_from_any_page(self):
        """Click-through from `/` with no typed URL: the tree is in the shell."""
        response = self.client.get(reverse("core:vms"))

        self.assertContains(response, "Hosts &amp; Clusters")
        self.assertContains(response, reverse("core:cluster_summary", args=["hq"]))
        self.assertContains(response, reverse("core:node_summary", args=["hq", "pve1"]))

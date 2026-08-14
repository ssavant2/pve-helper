"""The Hosts & Clusters shell and tree (5a2A+B).

What this phase can break is navigation: an object that should not be reachable,
a node leaf that should not exist, or a route that shadows a sibling. Each of those
has a test here. The Summary *bodies* are 5a2C/5a2D and are deliberately not
asserted beyond the shell they hang in.
"""

from __future__ import annotations

import re
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse
from django.utils import timezone

from core.models import (
    ClusterMembershipState,
    ClusterNodeEnrollment,
    ClusterNodeState,
    ClusterProjectionCoverage,
    CurrentGuestInventory,
    ProxmoxCluster,
)
from core.services.cluster_projection_read import read_cluster_projection
from core.services.durations import format_uptime
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

    def test_a_standalone_host_renders_as_one_leaf_pointing_at_its_node(self):
        """A standalone host is its node. Showing the connection as a parent of the
        node states the same object twice, and the connection's display name is
        operator-chosen so the two lines need not even agree."""
        _cluster("edge", role="standalone", nodes=("edge1",))

        [entry] = workspace_nav()["hosts"]

        self.assertTrue(entry.standalone)
        self.assertIsNotNone(entry.primary)
        self.assertEqual(entry.primary.node_name, "edge1")
        self.assertEqual(
            entry.primary.url,
            reverse("core:node_summary", args=["edge", "edge1"]),
        )

    def test_a_cluster_never_collapses_into_a_single_leaf(self):
        """One node does not make a corosync cluster a host."""
        _cluster("tiny", role="corosync", nodes=("solo1",))

        [entry] = workspace_nav()["clusters"]

        self.assertFalse(entry.standalone)
        self.assertIsNone(entry.primary)

    def test_a_standalone_whose_node_is_not_published_has_no_leaf_target(self):
        """Membership has not published the node yet, so there is nothing to point
        at; the object still belongs under Hosts."""
        cluster = _cluster("edge", role="standalone", nodes=("edge1",))
        ClusterNodeState.objects.filter(cluster=cluster).update(present=False)

        [entry] = workspace_nav()["hosts"]

        self.assertTrue(entry.standalone)
        self.assertIsNone(entry.primary)

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

    def test_the_routed_tabs_are_exactly_the_ones_with_views(self):
        """A tab is enabled by gaining a route, never by a flag. Cluster scope has
        Summary, Hosts, VMs, Datastores and Networks; the node scope has no Hosts tab."""
        cluster_tabs = self.client.get(reverse("core:cluster_summary", args=["hq"])).context["tabs"]
        node_tabs = self.client.get(reverse("core:node_summary", args=["hq", "pve1"])).context["tabs"]

        self.assertEqual(
            [tab.key for tab in cluster_tabs if tab.enabled],
            ["summary", "hosts", "vms", "datastores", "networks"],
        )
        self.assertEqual([tab.key for tab in node_tabs if tab.enabled], ["summary", "vms", "datastores", "networks"])
        self.assertNotIn("hosts", [tab.key for tab in node_tabs])
        # The unbuilt shape is still stated, so the workspace does not silently shrink.
        self.assertIn("updates", [tab.key for tab in cluster_tabs])
        self.assertFalse(next(tab for tab in cluster_tabs if tab.key == "updates").enabled)

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


class StickyTabTreeTests(TestCase):
    """Switching objects in the tree keeps the tab you are standing on.

    The mechanism is `nav_tags.sticky_object_url`, which guests and datastores have
    always used; this phase's tree was the surface that bypassed it by building
    Summary URLs directly. The tests are against rendered hrefs rather than the tag,
    because a tag that works and a template that does not call it look identical
    from the tag's own tests.
    """

    def setUp(self):
        self.client = Client()
        self.hq = _cluster("hq", nodes=("pve1", "pve2"))
        self.edge = _cluster("edge", nodes=("edge1",))

    def _hrefs(self, response) -> set[str]:
        return set(re.findall(r'href="([^"]+)"', response.content.decode()))

    def test_switching_node_from_a_node_tab_stays_on_that_tab(self):
        response = self.client.get(reverse("core:node_vms", args=["hq", "pve1"]))

        self.assertIn(reverse("core:node_vms", args=["hq", "pve2"]), self._hrefs(response))
        self.assertNotIn(reverse("core:node_summary", args=["hq", "pve2"]), self._hrefs(response))

    def test_the_tab_carries_across_clusters_and_between_scopes(self):
        response = self.client.get(reverse("core:node_vms", args=["hq", "pve1"]))
        hrefs = self._hrefs(response)

        self.assertIn(reverse("core:node_vms", args=["edge", "edge1"]), hrefs)
        self.assertIn(reverse("core:cluster_vms", args=["hq"]), hrefs)

    def test_a_tab_the_target_does_not_have_falls_back_to_summary(self):
        """Hosts is cluster-only, so a node leaf cannot stay on it."""

        response = self.client.get(reverse("core:cluster_hosts", args=["hq"]))
        hrefs = self._hrefs(response)

        self.assertIn(reverse("core:node_summary", args=["hq", "pve1"]), hrefs)
        self.assertNotIn("/clusters/hq/nodes/pve1/hosts/", hrefs)

    def test_a_page_that_is_not_an_object_tab_leaves_the_tree_on_summary(self):
        response = self.client.get(reverse("core:dashboard"))
        hrefs = self._hrefs(response)

        self.assertIn(reverse("core:cluster_summary", args=["hq"]), hrefs)
        self.assertIn(reverse("core:node_summary", args=["hq", "pve1"]), hrefs)

    def test_a_standalone_host_leaf_is_sticky_too(self):
        _cluster("solo", role="standalone", nodes=("solo1",))

        response = self.client.get(reverse("core:node_vms", args=["hq", "pve1"]))

        self.assertIn(reverse("core:node_vms", args=["solo", "solo1"]), self._hrefs(response))


class SummaryCompositionTests(TestCase):
    """5a2C+D. What the two Summary bodies may claim, and what they must not."""

    def setUp(self):
        self.client = Client()
        self.cluster = _cluster("hq", nodes=("pve1", "pve2", "pve3"))

    def _runtime(self, node_name, *, generation=3, complete=True, error_code="", **metrics):
        """Publish one node's runtime coverage so its status resolves to current."""
        now = timezone.now()
        ClusterNodeState.objects.filter(cluster=self.cluster, node_name=node_name).update(
            runtime_generation=generation, **metrics
        )
        ClusterProjectionCoverage.objects.update_or_create(
            cluster=self.cluster,
            domain=ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME,
            node_name=node_name,
            defaults={
                "generation": generation,
                "based_on_generation": 3,
                "complete": complete,
                "attempted_at": now,
                "observed_at": now,
                "error_code": error_code,
            },
        )

    def _guest(self, node, vmid, status="running", published=True):
        return CurrentGuestInventory.objects.create(
            cluster=self.cluster,
            node=node,
            object_type="vm",
            vmid=vmid,
            name=f"vm{vmid}",
            status=status,
            config={},
            observed_at=timezone.now(),
            published=published,
        )

    def _summary(self):
        from core.services.publication_scope import publication_scope
        from core.services.workspace_summary import cluster_summary

        projection = read_cluster_projection(self.cluster.key)
        scope = publication_scope(self.cluster)
        nodes = tuple(n for n in projection.nodes if n.present and scope.publishes(n.node_name))
        return cluster_summary(self.cluster, projection, nodes)

    def test_capacity_totals_only_nodes_whose_own_runtime_is_current(self):
        """A failed sibling lowers coverage; it never contributes stale numbers."""
        for node in ("pve1", "pve2"):
            self._runtime(node, memory_total_bytes=16, memory_used_bytes=4, cpu_cores=8)
        self._runtime("pve3", complete=False, error_code="read_failed", memory_total_bytes=999, cpu_cores=99)

        capacity = self._summary().capacity

        self.assertEqual((capacity.contributing, capacity.total), (2, 3))
        self.assertFalse(capacity.complete)
        self.assertEqual(capacity.missing, 1)
        self.assertEqual(capacity.memory_total_bytes, 32)
        self.assertEqual(capacity.cpu_cores, 16)

    def test_a_fully_covered_cluster_reports_complete(self):
        for node in ("pve1", "pve2", "pve3"):
            self._runtime(node, memory_total_bytes=8, cpu_cores=4)

        capacity = self._summary().capacity

        self.assertTrue(capacity.complete)
        self.assertEqual(capacity.contributing, 3)

    def test_no_current_node_totals_nothing_rather_than_guessing(self):
        capacity = self._summary().capacity

        self.assertEqual(capacity.contributing, 0)
        self.assertEqual(capacity.memory_total_bytes, 0)

    def test_guest_counts_come_from_the_published_projection(self):
        self._guest("pve1", 100)
        self._guest("pve1", 101, status="stopped")
        self._guest("pve2", 200, published=False)

        summary = self._summary()

        self.assertEqual(summary.guests_total, 2)
        self.assertEqual(summary.guests_running, 1)

    def test_a_node_with_no_guests_reports_zero_not_a_blank(self):
        summary = self._summary()

        self.assertEqual({row.node.node_name for row in summary.rows}, {"pve1", "pve2", "pve3"})
        self.assertTrue(all(row.placement.total == 0 for row in summary.rows))

    def test_guests_on_an_unlisted_node_are_counted_separately(self):
        """A node that left membership can still have guest rows. Dropping them
        would make the total disagree with the rows for no stated reason."""
        self._guest("pve1", 100)
        self._guest("departed-node", 300)

        summary = self._summary()

        self.assertEqual(summary.guests_total, 1)
        self.assertEqual(summary.guests_off_listed_nodes, 1)

    def test_a_hidden_member_is_named_rather_than_subtracted_in_silence(self):
        """Quorum counts a hidden node; the node total does not. Say so.

        Hiding is a pve-helper decision that never reaches Proxmox, so pve2 keeps
        voting after it stops being listed. "2 of 2, quorate" is two true rows whose
        pairing claims something neither says.
        """
        _activate(self.cluster, pve1="managed", pve2="safety_only", pve3="managed")

        response = self.client.get(reverse("core:cluster_summary", args=["hq"]))
        summary = response.context["summary"]

        self.assertEqual(summary.node_count, 2)
        self.assertEqual(summary.members_not_listed, 1)
        self.assertContains(response, "1 of 3 cluster members")
        self.assertContains(response, "hidden from this page")
        self.assertContains(response, "2 of 2 listed")

    def test_a_fully_listed_cluster_says_nothing_extra(self):
        _activate(self.cluster, pve1="managed", pve2="managed", pve3="managed")

        response = self.client.get(reverse("core:cluster_summary", args=["hq"]))

        self.assertEqual(response.context["summary"].members_not_listed, 0)
        self.assertNotContains(response, "hidden from this page")

    def test_a_stale_membership_generation_invents_no_hidden_member(self):
        """`member_count` from an older read minus today's nodes is not a count."""

        _activate(self.cluster, pve1="managed", pve2="safety_only", pve3="managed")
        ClusterProjectionCoverage.objects.filter(
            cluster=self.cluster,
            domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP,
        ).update(generation=1)

        response = self.client.get(reverse("core:cluster_summary", args=["hq"]))

        self.assertFalse(response.context["projection"].membership_current)
        self.assertEqual(response.context["summary"].members_not_listed, 0)

    def test_node_summary_never_borrows_cluster_freshness(self):
        """pve1 is current and pve3 failed. Each page states its own node's truth."""
        from core.services.workspace_summary import node_summary

        self._runtime("pve1", memory_total_bytes=16)
        self._runtime("pve3", complete=False, error_code="read_failed")
        projection = read_cluster_projection(self.cluster.key)
        by_name = {node.node_name: node for node in projection.nodes}

        self.assertTrue(node_summary(self.cluster, by_name["pve1"]).node.runtime_current)
        self.assertFalse(node_summary(self.cluster, by_name["pve3"]).node.runtime_current)

    def test_node_placement_counts_only_that_node(self):
        from core.services.workspace_summary import node_summary

        self._guest("pve1", 100)
        self._guest("pve2", 200)
        projection = read_cluster_projection(self.cluster.key)
        pve1 = next(node for node in projection.nodes if node.node_name == "pve1")

        self.assertEqual(node_summary(self.cluster, pve1).placement.total, 1)


class ResourceMeterTests(TestCase):
    """5a2G. Two claims a bar can make that a definition list could not.

    * **Unknown is not the low end of the scale.** Every branch below exists so a
      node that said nothing cannot be drawn as a node using nothing.
    * **The breakdown and the total tell one story.** A node excluded from the
      cluster roll-up shows no bars in the row that explains it.
    """

    def setUp(self):
        self.client = Client()
        self.cluster = _cluster("hq", nodes=("pve1", "pve2", "pve3"))

    _runtime = SummaryCompositionTests._runtime
    _summary = SummaryCompositionTests._summary

    def _meter(self, used, total):
        from core.services.workspace_summary import Meter

        return Meter(used=used, total=total)

    def test_a_value_the_node_never_reported_has_no_percentage_at_all(self):
        self.assertFalse(self._meter(None, 16).known)
        self.assertIsNone(self._meter(None, 16).percent)

    def test_zero_used_is_a_real_reading_and_keeps_its_bar(self):
        """The distinction the whole dataclass exists for, asserted directly."""

        meter = self._meter(0, 16)

        self.assertTrue(meter.known)
        self.assertEqual(meter.percent, 0.0)

    def test_a_zero_total_is_unknown_rather_than_a_division(self):
        self.assertFalse(self._meter(0, 0).known)
        self.assertIsNone(self._meter(0, 0).percent)

    def test_a_full_meter_is_a_hundred_percent(self):
        self.assertEqual(self._meter(8, 16).percent, 50.0)
        self.assertEqual(self._meter(16, 16).percent, 100.0)

    def test_cpu_is_metered_against_cores_and_needs_both_halves(self):
        from core.services.workspace_summary import node_meters

        with_cores = node_meters(SimpleNamespace(cpu_usage=0.25, cpu_cores=16, **_NO_BYTES))
        without = node_meters(SimpleNamespace(cpu_usage=0.25, cpu_cores=None, **_NO_BYTES))

        self.assertEqual(with_cores.cpu.used, 4.0)
        self.assertEqual(with_cores.cpu.percent, 25.0)
        self.assertFalse(without.cpu.known)

    def test_one_absent_metric_does_not_take_the_others_with_it(self):
        from core.services.workspace_summary import node_meters

        meters = node_meters(
            SimpleNamespace(
                cpu_usage=None,
                cpu_cores=8,
                memory_used_bytes=4,
                memory_total_bytes=16,
                swap_used_bytes=None,
                swap_total_bytes=None,
                rootfs_used_bytes=1,
                rootfs_total_bytes=4,
            )
        )

        self.assertFalse(meters.cpu.known)
        self.assertFalse(meters.swap.known)
        self.assertTrue(meters.memory.known)
        self.assertEqual(meters.rootfs.percent, 25.0)

    def test_cluster_cpu_is_core_weighted_and_not_an_average_of_fractions(self):
        """A busy 4-core node beside an idle 64-core one is not a half-busy cluster."""

        self._runtime("pve1", cpu_usage=1.0, cpu_cores=4)
        self._runtime("pve2", cpu_usage=0.0, cpu_cores=64)
        self._runtime("pve3", complete=False, error_code="read_failed", cpu_usage=1.0, cpu_cores=32)

        capacity = self._summary().capacity

        self.assertEqual(capacity.cpu_used_cores, 4.0)
        self.assertEqual(capacity.cpu_cores, 68)
        self.assertAlmostEqual(capacity.cpu.percent, 100.0 * 4 / 68)

    def test_a_node_excluded_from_the_totals_shows_no_bars_in_its_own_row(self):
        self._runtime("pve1", memory_total_bytes=16, memory_used_bytes=4, cpu_cores=8, cpu_usage=0.5)
        self._runtime("pve3", complete=False, error_code="read_failed", memory_total_bytes=999, cpu_cores=99)

        rows = {row.node.node_name: row for row in self._summary().rows}

        self.assertIsNotNone(rows["pve1"].meters)
        self.assertEqual(rows["pve1"].meters.memory.percent, 25.0)
        self.assertIsNone(rows["pve3"].meters)

    def test_the_node_page_keeps_its_bars_because_it_states_its_own_staleness(self):
        """Node Summary carries a banner naming the stale runtime; the row cannot."""
        from core.services.workspace_summary import node_summary

        self._runtime("pve3", complete=False, error_code="read_failed", memory_total_bytes=16, memory_used_bytes=8)
        projection = read_cluster_projection(self.cluster.key)
        pve3 = next(node for node in projection.nodes if node.node_name == "pve3")

        summary = node_summary(self.cluster, pve3)

        self.assertFalse(summary.node.runtime_current)
        self.assertEqual(summary.meters.memory.percent, 50.0)

    def test_the_cluster_page_draws_a_track_only_for_the_node_it_counted(self):
        self._runtime("pve1", memory_total_bytes=16, memory_used_bytes=4, cpu_cores=8, cpu_usage=0.5)
        self._runtime("pve3", complete=False, error_code="read_failed")

        response = self.client.get(reverse("core:cluster_summary", args=["hq"]))

        # The roll-up's own bars, then the row that explains why one node is absent
        # from them. The byte figure can only come from the Capacity panel: the
        # per-node cells render a percentage and no absolute value. `filesizeformat`
        # joins with a non-breaking space, so the literal one does not match.
        self.assertContains(response, "meter-fill")
        self.assertContains(response, "of 16\u00a0bytes")
        self.assertContains(response, "of 8 cores")
        self.assertContains(response, "Not counted while runtime is")

    def test_the_node_page_names_an_absent_metric_instead_of_drawing_an_empty_bar(self):
        """pve1 reports memory and no swap: one track, one stated absence."""

        self._runtime("pve1", memory_total_bytes=16, memory_used_bytes=4)

        response = self.client.get(reverse("core:node_summary", args=["hq", "pve1"]))
        body = response.content.decode()

        self.assertContains(response, "not reported")
        self.assertEqual(body.count("meter-track"), 1)

    def test_load_average_stays_text_because_it_has_no_ceiling(self):
        """A queue length metered against cores would invent a maximum it lacks."""

        self._runtime("pve1", load_average_1m=9.5, cpu_cores=4)

        response = self.client.get(reverse("core:node_summary", args=["hq", "pve1"]))

        self.assertContains(response, "Load average")
        self.assertContains(response, "9.50")


#: Byte metrics a CPU-only meter fixture has to carry and does not care about.
_NO_BYTES = {
    "memory_used_bytes": None,
    "memory_total_bytes": None,
    "swap_used_bytes": None,
    "swap_total_bytes": None,
    "rootfs_used_bytes": None,
    "rootfs_total_bytes": None,
}


class ObservationAgeTests(TestCase):
    """How old the numbers are, on a 24-hour clock.

    Two separate claims. The Summary panels answer *when was this read* with an
    age rather than the projection's generation counter, which only ever climbs.
    And every timestamp this app renders is `Y-m-d H:i:s`, including one written
    as a bare `{{ value }}` — that is the format module's job, not the template's.
    """

    def setUp(self):
        self.client = Client()
        self.cluster = _cluster("hq", nodes=("pve1", "pve2"))

    _runtime = SummaryCompositionTests._runtime

    def _stamp(self, **coverage):
        observed = ClusterProjectionCoverage.objects.get(cluster=self.cluster, **coverage).observed_at
        return timezone.localtime(observed).strftime("%Y-%m-%d %H:%M:%S")

    def test_the_node_page_states_an_age_instead_of_a_generation_counter(self):
        self._runtime("pve1", generation=3, memory_total_bytes=16)

        response = self.client.get(reverse("core:node_summary", args=["hq", "pve1"]))

        self.assertContains(response, "Runtime observed just now")
        self.assertNotContains(response, "Runtime generation")

    def test_the_cluster_page_states_an_age_instead_of_a_generation_counter(self):
        response = self.client.get(reverse("core:cluster_summary", args=["hq"]))

        self.assertContains(response, "Observed just now from pve1")
        self.assertNotContains(response, "Generation 3")

    def test_an_older_reading_is_named_in_the_largest_unit_that_still_counts(self):
        self._runtime("pve1", memory_total_bytes=16)
        ClusterProjectionCoverage.objects.filter(
            cluster=self.cluster,
            domain=ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME,
            node_name="pve1",
        ).update(observed_at=timezone.now() - timedelta(hours=5))

        response = self.client.get(reverse("core:node_summary", args=["hq", "pve1"]))

        self.assertContains(response, "Runtime observed 5 hours ago")

    def test_a_node_that_was_attempted_and_never_observed_says_so(self):
        """Coverage without an `observed_at` is a read that has only ever failed.

        `timesince` of nothing is the empty string, so the age line cannot be the
        one that renders here.
        """
        self._runtime("pve1", complete=False, error_code="read_failed")
        ClusterProjectionCoverage.objects.filter(
            cluster=self.cluster,
            domain=ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME,
            node_name="pve1",
        ).update(observed_at=None)

        response = self.client.get(reverse("core:node_summary", args=["hq", "pve1"]))

        self.assertContains(response, "attempted but never observed")
        self.assertNotContains(response, "Runtime observed")

    def test_the_age_phrase_covers_its_units_and_refuses_the_future(self):
        """`timesince` would answer `0 minutes` for the first case and for the last."""
        from core.services.durations import format_age

        now = timezone.now()

        self.assertEqual(format_age(now - timedelta(seconds=40), now=now), "just now")
        self.assertEqual(format_age(now - timedelta(minutes=1), now=now), "1 minute ago")
        self.assertEqual(format_age(now - timedelta(minutes=59), now=now), "59 minutes ago")
        self.assertEqual(format_age(now - timedelta(hours=2), now=now), "2 hours ago")
        self.assertEqual(format_age(now - timedelta(days=3), now=now), "3 days ago")
        self.assertEqual(format_age(now + timedelta(minutes=5), now=now), "just now")
        self.assertEqual(format_age(None, now=now), "")

    def test_a_node_with_no_coverage_at_all_keeps_its_own_sentence(self):
        response = self.client.get(reverse("core:node_summary", args=["hq", "pve1"]))

        self.assertContains(response, "never been published")

    def test_both_summary_pages_print_the_observation_on_a_24_hour_clock(self):
        """The timestamps beside the ages are bare `{{ value }}` renders."""

        self._runtime("pve1", memory_total_bytes=16)

        node = self.client.get(reverse("core:node_summary", args=["hq", "pve1"]))
        cluster = self.client.get(reverse("core:cluster_summary", args=["hq"]))

        self.assertContains(
            node,
            self._stamp(domain=ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME, node_name="pve1"),
        )
        self.assertContains(
            cluster,
            self._stamp(domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP, node_name=None),
        )
        for response in (node, cluster):
            body = response.content.decode()
            self.assertNotIn("p.m.", body)
            self.assertNotIn("a.m.", body)

    def test_the_default_datetime_format_is_the_one_the_app_writes_by_hand(self):
        """30-odd templates say `|date:"Y-m-d H:i:s"`; the default now agrees."""
        from django.utils.formats import get_format

        self.assertEqual(get_format("DATETIME_FORMAT"), "Y-m-d H:i:s")
        self.assertEqual(get_format("DATE_FORMAT"), "Y-m-d")
        self.assertEqual(get_format("TIME_FORMAT"), "H:i:s")

        # What the `en` locale would have rendered, so this test fails if the
        # format module stops being reached rather than passing on a coincidence.
        # `FORMAT_MODULE_PATH` is not one of the settings Django resets the format
        # cache for, hence the explicit reset on both sides of the override.
        from django.utils.formats import reset_format_cache

        try:
            with override_settings(FORMAT_MODULE_PATH=[]):
                reset_format_cache()
                self.assertEqual(get_format("DATETIME_FORMAT"), "N j, Y, P")
        finally:
            reset_format_cache()

    def test_overriding_the_clock_did_not_move_the_decimal_point(self):
        """`en-us` is why `floatformat` emits `53.9`. The format module adds only
        date and time names, so the number formats still come from the locale."""
        from django.template import Context, Template

        rendered = Template("{{ value|floatformat:1 }}").render(Context({"value": 53.87}))

        self.assertEqual(rendered, "53.9")


@override_settings(APP_REQUIRE_LOGIN=False)
class UptimeLabelTests(TestCase):
    """`868338s` is the stored value and nobody reads it as ten days."""

    def test_a_long_uptime_reads_as_days_and_hours(self):
        self.assertEqual(format_uptime(868338), "10d 1h")

    def test_short_uptimes_keep_the_unit_that_still_says_something(self):
        self.assertEqual(format_uptime(11_100), "3h 5m")
        self.assertEqual(format_uptime(2_700), "45m")
        self.assertEqual(format_uptime(30), "<1m")

    def test_absent_or_zero_uptime_is_a_dash_not_a_zero(self):
        self.assertEqual(format_uptime(None), "-")
        self.assertEqual(format_uptime(0), "-")

    def test_the_node_page_renders_the_duration_and_not_the_seconds(self):
        cluster = _cluster("hq", nodes=("pve1",))
        ClusterNodeState.objects.filter(cluster=cluster, node_name="pve1").update(uptime_seconds=868338)

        response = Client().get(reverse("core:node_summary", args=["hq", "pve1"]))

        self.assertContains(response, "10d 1h")
        self.assertNotContains(response, "868338")


class SummaryQueryBudgetTests(TestCase):
    """The contract allows each Summary 6 queries. Measured, not asserted in prose.

    The 6 is a **delta**, per the measurement protocol in
    `tests_shell_query_budget.py`: the page total minus that page's pre-phase total.
    5a2A+B already paid for the shell and the projection read; what 5a2C+D adds is
    the composition, so that is what is measured against the allowance. The absolute
    page figure is asserted too, because a shell regression would otherwise hide
    behind a healthy-looking delta.
    """

    #: Composition delta allowed by the contract row for each Summary.
    COMPOSITION_BUDGET = 6
    #: Measured 2026-08-13, warm, three nodes: 11 shell + 6 projection + 1 guest
    #: aggregate. Raise only with a recorded reason.
    CLUSTER_SUMMARY_PAGE = 18
    NODE_SUMMARY_PAGE = 18

    def setUp(self):
        self.client = Client()
        self.cluster = _cluster("hq", nodes=("pve1", "pve2", "pve3"))

    def _page_queries(self, url: str) -> int:
        self.client.get(url)  # warm any per-process cache the shell keeps
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(captured)

    def _composition_queries(self, compose) -> int:
        """What this phase added: the composition, with the projection already read."""
        from core.services.publication_scope import publication_scope

        projection = read_cluster_projection(self.cluster.key)
        scope = publication_scope(self.cluster)
        nodes = tuple(n for n in projection.nodes if n.present and scope.publishes(n.node_name))
        with CaptureQueriesContext(connection) as captured:
            compose(projection, nodes)
        return len(captured)

    def test_cluster_summary_composition_stays_inside_its_budget(self):
        from core.services.workspace_summary import cluster_summary

        measured = self._composition_queries(lambda projection, nodes: cluster_summary(self.cluster, projection, nodes))

        self.assertLessEqual(
            measured,
            self.COMPOSITION_BUDGET,
            f"Cluster Summary composition costs {measured} queries against {self.COMPOSITION_BUDGET}.",
        )
        # One aggregate, and it stays one: the guest counts are the only read this
        # phase added, and a per-node version of it is the fan-out to watch for.
        self.assertEqual(measured, 1)

    def test_node_summary_composition_stays_inside_its_budget(self):
        from core.services.workspace_summary import node_summary

        measured = self._composition_queries(lambda projection, nodes: node_summary(self.cluster, nodes[0]))

        self.assertLessEqual(
            measured,
            self.COMPOSITION_BUDGET,
            f"Node Summary composition costs {measured} queries against {self.COMPOSITION_BUDGET}.",
        )
        self.assertEqual(measured, 1)

    def test_the_rendered_pages_stay_at_their_recorded_cost(self):
        cluster = self._page_queries(reverse("core:cluster_summary", args=["hq"]))
        node = self._page_queries(reverse("core:node_summary", args=["hq", "pve1"]))

        self.assertEqual(
            (cluster, node),
            (self.CLUSTER_SUMMARY_PAGE, self.NODE_SUMMARY_PAGE),
            f"Summary page cost changed (cluster={cluster}, node={node}). A silent "
            "increase here is the regression this figure exists to catch.",
        )

    def test_summary_cost_is_flat_in_node_count(self):
        """Twenty nodes must cost what three do; a per-node query here is the
        fan-out the whole projection exists to prevent."""
        _cluster("big", nodes=tuple(f"n{index:02d}" for index in range(20)))

        three = self._page_queries(reverse("core:cluster_summary", args=["hq"]))
        twenty = self._page_queries(reverse("core:cluster_summary", args=["big"]))

        self.assertEqual(three, twenty, f"3 nodes cost {three}, 20 nodes cost {twenty}")


@override_settings(APP_REQUIRE_LOGIN=False)
class RenderPathFanOutCacheTests(TestCase):
    """The mitigation the 5a0A ledger records as load-bearing, finally measured.

    Two render-path readers are affordable only because they are cached:
    `_fetch_live_guest_locks_uncached` at `2N + 1` calls behind a 3-second window,
    and the live-inventory fallback at `2N` behind a 30-second one. The ledger
    recorded a coverage gap here in as many words — "nothing now proves the caches
    actually suppress the render-path fan-outs this ledger records as cached" — and
    assigned it to this phase. These tests warm and re-read, and count.

    They assert the *shape* (a second read costs nothing more), not a call total, so
    a legitimate change to how many endpoints a single pass touches does not fail
    them while a removed cache still does.
    """

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.cluster = _cluster("fanout", nodes=("pve1", "pve2"))

    def _counting_client(self, calls):
        class _Client:
            endpoint = "https://pve1:8006"

            def get(self, path, **kwargs):
                calls.append(path)
                if path == "nodes":
                    return [
                        {"node": "pve1", "status": "online"},
                        {"node": "pve2", "status": "online"},
                    ]
                if path.startswith("cluster/resources"):
                    return []
                return []

        return _Client()

    def test_the_guest_lock_read_costs_nothing_on_a_second_render(self):
        from core.services import proxmox

        calls: list[str] = []
        client = self._counting_client(calls)
        with patch(
            "core.services.cluster_resolver.cluster_wide_read",
            side_effect=lambda cluster, *, operation, call: SimpleNamespace(
                value=call(client), complete=True, client=client, answering_endpoint=None
            ),
        ):
            proxmox.fetch_live_guest_locks(cluster=self.cluster)
            warm = len(calls)
            self.assertGreater(warm, 0, "the cold read must actually reach the provider")

            proxmox.fetch_live_guest_locks(cluster=self.cluster)

        self.assertEqual(
            len(calls),
            warm,
            "The second render re-read the provider: the 3-second cache that makes "
            "this 2N + 1 fan-out affordable is not suppressing it.",
        )

    def test_the_cache_is_per_cluster_not_global(self):
        """A warm cache for one cluster must not answer for another; that would be
        worse than the fan-out it replaces."""
        from core.services import proxmox

        other = _cluster("fanout-b", nodes=("pve1",))
        calls: list[str] = []
        client = self._counting_client(calls)
        with patch(
            "core.services.cluster_resolver.cluster_wide_read",
            side_effect=lambda cluster, *, operation, call: SimpleNamespace(
                value=call(client), complete=True, client=client, answering_endpoint=None
            ),
        ):
            proxmox.fetch_live_guest_locks(cluster=self.cluster)
            warm = len(calls)
            proxmox.fetch_live_guest_locks(cluster=other)

        self.assertGreater(len(calls), warm, "the second cluster was answered from the first cluster's cache")

    def test_the_workspace_tables_never_reach_the_provider(self):
        """The tables are the surface the fan-outs were reachable from. They read
        the projection and nothing else."""
        with (
            patch("core.services.proxmox.ProxmoxClient.__init__", side_effect=AssertionError("provider call")),
            patch("core.services.proxmox.ProxmoxClient.get", side_effect=AssertionError("provider call")),
        ):
            for url in (
                reverse("core:cluster_hosts", args=["fanout"]),
                reverse("core:cluster_vms", args=["fanout"]),
                reverse("core:node_vms", args=["fanout", "pve1"]),
            ):
                self.assertEqual(self.client.get(url).status_code, 200, url)


@override_settings(APP_REQUIRE_LOGIN=False)
class WorkspaceTableTests(TestCase):
    """5a2E+F. The two tables, and the identity collisions they must survive."""

    def setUp(self):
        self.client = Client()
        self.hq = _cluster("hq", nodes=("pve1", "pve2"))
        self.other = _cluster("other", nodes=("pve1",))

    def _guest(self, cluster, node, vmid, *, name="", status="running", published=True):
        return CurrentGuestInventory.objects.create(
            cluster=cluster,
            node=node,
            object_type="vm",
            vmid=vmid,
            name=name or f"vm{vmid}",
            status=status,
            config={},
            observed_at=timezone.now(),
            published=published,
        )

    def test_duplicate_node_names_link_to_their_own_cluster(self):
        """Both clusters have a pve1. Each Hosts table must link to its own.

        Asserted against the table's own rows rather than the page body: the sidebar
        legitimately lists every cluster's pve1, so a whole-response match would
        pass or fail for reasons that have nothing to do with this table.
        """
        hq = self.client.get(reverse("core:cluster_hosts", args=["hq"]))
        other = self.client.get(reverse("core:cluster_hosts", args=["other"]))

        self.assertEqual([row.node.node_ref for row in hq.context["summary"].rows], ["nr1:hq:pve1", "nr1:hq:pve2"])
        self.assertEqual([row.node.node_ref for row in other.context["summary"].rows], ["nr1:other:pve1"])
        self.assertContains(hq, reverse("core:node_summary", args=["hq", "pve1"]))

    def test_duplicate_type_vmid_stays_two_rows_across_clusters(self):
        self._guest(self.hq, "pve1", 100, name="hq-100")
        self._guest(self.other, "pve1", 100, name="other-100")

        hq = self.client.get(reverse("core:cluster_vms", args=["hq"]))

        self.assertEqual(len(hq.context["guests"]), 1)
        self.assertEqual(hq.context["guests"][0].name, "hq-100")
        self.assertContains(hq, reverse("core:guest_summary", args=["hq", "vm", 100]))

    def test_cluster_vms_reuses_the_rich_overview_without_an_unscoped_filter(self):
        self._guest(self.hq, "pve1", 100, name="hq-100")

        response = self.client.get(reverse("core:cluster_vms", args=["hq"]))

        self.assertContains(response, 'data-column="provisioned"')
        self.assertContains(response, 'data-column="guest-os"')
        self.assertContains(response, "data-vm-overview-row")
        self.assertNotContains(response, 'data-cluster-filter=""')

    def test_the_vms_table_excludes_unpublished_guests(self):
        self._guest(self.hq, "pve1", 100)
        self._guest(self.hq, "pve2", 200, published=False)

        response = self.client.get(reverse("core:cluster_vms", args=["hq"]))

        self.assertEqual([guest.vmid for guest in response.context["guests"]], [100])

    def test_the_node_vms_table_is_scoped_to_that_node(self):
        self._guest(self.hq, "pve1", 100)
        self._guest(self.hq, "pve2", 200)

        response = self.client.get(reverse("core:node_vms", args=["hq", "pve1"]))

        self.assertEqual([guest.vmid for guest in response.context["guests"]], [100])

    def test_a_guest_on_an_unlisted_node_is_shown_without_a_dead_link(self):
        """The row is real; the node page is not. Linking it would 404 from a table
        whose job is to be a reliable jumping-off point."""
        self._guest(self.hq, "departed", 300)

        response = self.client.get(reverse("core:cluster_vms", args=["hq"]))

        [row] = [guest for guest in response.context["guests"] if guest.vmid == 300]
        self.assertEqual(row.node, "departed")
        self.assertNotContains(response, reverse("core:node_summary", args=["hq", "departed"]))

    def test_a_hidden_nodes_table_is_404_not_an_empty_list(self):
        _activate(self.hq, pve1="managed", pve2="safety_only")

        self.assertEqual(self.client.get(reverse("core:node_vms", args=["hq", "pve1"])).status_code, 200)
        self.assertEqual(self.client.get(reverse("core:node_vms", args=["hq", "pve2"])).status_code, 404)

    def test_the_hosts_table_lists_managed_nodes_only(self):
        _activate(self.hq, pve1="managed", pve2="safety_only")

        response = self.client.get(reverse("core:cluster_hosts", args=["hq"]))

        self.assertEqual([row.node.node_name for row in response.context["summary"].rows], ["pve1"])

    def test_the_guest_card_links_back_to_the_workspace(self):
        from core.views.clusters.workspace import workspace_object_urls

        urls = workspace_object_urls(self.hq, "pve1")

        self.assertEqual(urls["cluster_url"], reverse("core:cluster_summary", args=["hq"]))
        self.assertEqual(urls["node_url"], reverse("core:node_summary", args=["hq", "pve1"]))

    def test_the_guest_card_does_not_link_a_hidden_node(self):
        from core.views.clusters.workspace import workspace_object_urls

        _activate(self.hq, pve1="managed", pve2="safety_only")

        urls = workspace_object_urls(self.hq, "pve2")

        self.assertEqual(urls["cluster_url"], reverse("core:cluster_summary", args=["hq"]))
        self.assertEqual(urls["node_url"], "", "a hidden node has no page to link to")

    def test_the_guest_card_does_not_link_a_retired_cluster(self):
        from core.views.clusters.workspace import workspace_object_urls

        _retire(self.hq)

        self.assertEqual(workspace_object_urls(self.hq, "pve1"), {"cluster_url": "", "node_url": ""})

    def test_the_tables_are_flat_in_guest_count(self):
        for vmid in range(100, 140):
            self._guest(self.hq, "pve1", vmid)
        url = reverse("core:cluster_vms", args=["hq"])
        self.client.get(url)

        with CaptureQueriesContext(connection) as many:
            self.client.get(url)
        CurrentGuestInventory.objects.filter(cluster=self.hq, vmid__gte=120).delete()
        self.client.get(url)
        with CaptureQueriesContext(connection) as fewer:
            self.client.get(url)

        self.assertEqual(len(many), len(fewer), "the VMs table costs a query per guest")

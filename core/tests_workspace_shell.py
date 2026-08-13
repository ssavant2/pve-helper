"""The Hosts & Clusters shell and tree (5a2A+B).

What this phase can break is navigation: an object that should not be reachable,
a node leaf that should not exist, or a route that shadows a sibling. Each of those
has a test here. The Summary *bodies* are 5a2C/5a2D and are deliberately not
asserted beyond the shell they hang in.
"""

from __future__ import annotations

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

    def test_the_routed_tabs_are_exactly_the_ones_with_views(self):
        """A tab is enabled by gaining a route, never by a flag. Cluster scope has
        Summary, Hosts and VMs; the node scope has no Hosts tab at all."""
        cluster_tabs = self.client.get(reverse("core:cluster_summary", args=["hq"])).context["tabs"]
        node_tabs = self.client.get(reverse("core:node_summary", args=["hq", "pve1"])).context["tabs"]

        self.assertEqual([tab.key for tab in cluster_tabs if tab.enabled], ["summary", "hosts", "vms"])
        self.assertEqual([tab.key for tab in node_tabs if tab.enabled], ["summary", "vms"])
        self.assertNotIn("hosts", [tab.key for tab in node_tabs])
        # The unbuilt shape is still stated, so the workspace does not silently shrink.
        self.assertIn("datastores", [tab.key for tab in cluster_tabs])

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


class SummaryCompositionTests(TestCase):
    """5a2C+D. What the two Summary bodies may claim, and what they must not."""

    def setUp(self):
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


@override_settings(APP_REQUIRE_LOGIN=False)
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
        self.assertEqual(row.node_url, "")

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

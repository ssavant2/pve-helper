"""The shared shell's database cost must not scale with the infrastructure.

5a0A / U0 item 4. Module 5 is about to hang a node tree, cluster Summary and node
Summary off `base.html`, and every one of them renders inside the same context
processor that already composes navigation for every HTML response in the app. The
failure this pins is the one the Round 7 lesson describes: a per-object query
inside a shared surface, invisible at lab scale and quadratic at real scale.

The numbers below are a *baseline*, recorded before Module 5 adds its surfaces, so
the delta each new phase costs is measurable rather than argued about. They are
deliberately exact: "bounded" is what a budget says when nobody measured it.

The shape that matters is not the absolute count -- it is that the count is flat in
node count. `app_settings` loops over clusters and calls `datastore_nav` per
cluster, so cluster count is an accepted linear axis; nodes are not.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from core.models import ClusterStorage, ClusterStorageNodeState, ProxmoxCluster

# Measured 2026-08-07, warm caches, one cluster of three nodes.
#
# The first version of this file measured only "/" and reported its 24 queries as
# "the shared shell". Independent review falsified that: "/" is the dashboard, and
# roughly half of those queries are the dashboard *view's* own reads. Measuring
# three real pages separates the two costs:
#
#   /           24   dashboard  (shell + its own scan/audit/storage reads)
#   /clusters/  14   Connections
#   /vms/       12   guest overview -- the floor, and the closest thing to the
#                    shell's own cost on a page that adds little of its own
#
# So the shared shell is ~12, not 24, and the lesson drawn from the bad number
# ("navigation is the smaller half") described the dashboard, not the shell.
# Module 5 hangs its tree off `app_settings`, which every one of these pays --
# including the ones the original test never looked at.
#
# Raise these only with a recorded reason; a silent increase is the regression this
# file exists to catch.
SHELL_FLOOR_BUDGET = 12
PAGE_QUERY_BUDGETS = {"/": 24, "/clusters/": 14, "/vms/": 12}
SHELL_QUERY_BUDGET_PER_EXTRA_CLUSTER = 4

# Allowances for the surfaces Module 5 has not built yet, expressed as a delta over
# the shell floor so the budget is a number rather than "bounded" (U0 item 4). Each
# is the phase's entry gate; exceeding it is a review reject, not a tuning task.
MODULE5_QUERY_ALLOWANCES = {
    "navigation tree (5a2B)": 4,
    "cluster Summary (5a2C)": 6,
    "node Summary (5a2D)": 6,
    "first diagnostics read (5a1F)": 4,
}


def _seed_cluster(key: str, *, nodes: int, storages_per_node: int = 2) -> ProxmoxCluster:
    """One cluster with `nodes` nodes, each publishing node-local datastores.

    Node-local rows are what make `datastore_nav` produce per-node groups, which is
    the part of the shell that could plausibly grow with node count.
    """
    cluster = ProxmoxCluster.objects.create(key=key, display_name=key.upper(), enabled=True)
    shared = ClusterStorage.objects.create(
        cluster=cluster,
        storage_id=f"{key}-shared",
        storage_type="nfs",
        shared=True,
        present=True,
    )
    for index in range(nodes):
        node = f"node{index:02d}"
        ClusterStorageNodeState.objects.create(cluster_storage=shared, node=node, active=True, present=True)
        for local_index in range(storages_per_node):
            local = ClusterStorage.objects.create(
                cluster=cluster,
                storage_id=f"{key}-{node}-local{local_index}",
                storage_type="dir",
                shared=False,
                present=True,
            )
            ClusterStorageNodeState.objects.create(cluster_storage=local, node=node, active=True, present=True)
    return cluster


@override_settings(APP_REQUIRE_LOGIN=False)
class SharedShellQueryBudgetTests(TestCase):
    """The shell's cost is flat in nodes and linear in clusters, by measurement."""

    def setUp(self):
        user = get_user_model().objects.create_user(username="budget", password="budget-pw")
        self.client = Client()
        self.client.force_login(user)

    def _shell_queries(self, path: str) -> int:
        # Warm first: `datastore_nav` caches per cluster for 60s, so a cold render
        # measures cache population rather than the steady state an operator sees.
        self.client.get(path)
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        return len(captured)

    def _reset(self):
        # ClusterStorage.cluster is PROTECT, so the clusters cannot be dropped
        # until their catalog rows are. Order matters; a bare cluster delete raises.
        ClusterStorageNodeState.objects.all().delete()
        ClusterStorage.objects.all().delete()
        ProxmoxCluster.objects.all().delete()

    def test_the_shell_does_not_query_per_node(self):
        # Checked on the floor page, so a per-node query in `app_settings` cannot
        # hide behind the dashboard's own reads.
        _seed_cluster("one", nodes=1)
        one_node = self._shell_queries("/vms/")

        self._reset()
        _seed_cluster("three", nodes=3)
        three_nodes = self._shell_queries("/vms/")

        self._reset()
        _seed_cluster("twenty", nodes=20)
        twenty_nodes = self._shell_queries("/vms/")

        self.assertEqual(
            (one_node, three_nodes, twenty_nodes),
            (one_node, one_node, one_node),
            "The shared shell issued a different number of queries at 1, 3 and 20 "
            f"nodes ({one_node}/{three_nodes}/{twenty_nodes}). Navigation must "
            "compose nodes in bulk; a per-node query here multiplies across every "
            "HTML response in the app.",
        )

    def test_every_measured_page_stays_inside_its_budget(self):
        _seed_cluster("solo", nodes=3)

        measured = {path: self._shell_queries(path) for path in PAGE_QUERY_BUDGETS}
        over = {
            path: (count, PAGE_QUERY_BUDGETS[path])
            for path, count in measured.items()
            if count > PAGE_QUERY_BUDGETS[path]
        }

        self.assertEqual(
            over,
            {},
            f"Page query budgets exceeded (measured/allowed): {over}. Module 5 hangs "
            "its tree and Summary panels off the shared context processor, so growth "
            "there is paid by every page in this table.",
        )

    def test_the_shared_shell_floor_holds(self):
        _seed_cluster("solo", nodes=3)

        floor = min(self._shell_queries(path) for path in PAGE_QUERY_BUDGETS)

        self.assertLessEqual(
            floor,
            SHELL_FLOOR_BUDGET,
            f"The cheapest page now costs {floor} queries against a shell floor of "
            f"{SHELL_FLOOR_BUDGET}. The floor is the shell's own cost: every page "
            "pays it, so it is the number Module 5's allowances are measured from.",
        )

    def test_extra_clusters_cost_a_bounded_amount_each(self):
        _seed_cluster("alpha", nodes=3)
        one_cluster = self._shell_queries("/vms/")

        _seed_cluster("beta", nodes=3)
        two_clusters = self._shell_queries("/vms/")

        delta = two_clusters - one_cluster
        self.assertLessEqual(
            delta,
            SHELL_QUERY_BUDGET_PER_EXTRA_CLUSTER,
            f"A second cluster added {delta} queries to every page render, above the "
            f"{SHELL_QUERY_BUDGET_PER_EXTRA_CLUSTER} allowed. `app_settings` loops "
            "clusters deliberately; the per-cluster body is what must stay small.",
        )

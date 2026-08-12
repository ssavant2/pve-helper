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
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.db import connection
from django.test import Client, RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from core.context_processors import app_settings
from core.models import ClusterStorage, ClusterStorageNodeState, ProxmoxCluster

# Measured 2026-08-07, warm caches, one cluster of three nodes.
#
# This number has been wrong twice, in the same direction both times: attributing a
# page's own reads to the shell. Version one measured only "/" and called its 24
# queries "the shared shell" -- "/" is the dashboard. Version two used `/vms/`'s 12
# as "the shell's own cost" -- `/vms/` still adds its own guest and cluster-scope
# reads. Three separate figures, measured, not inferred:
#
#   app_settings + task bar     8   what EVERY HTML response pays
#   /vms/                      12   cheapest observed page; an upper bound on 8
#   /clusters/                 14   Connections
#   /                          24   dashboard
#
# 8 is the shell. 12 is a page. Module 5's allowances are deltas measured on the
# page named in each row, not over the floor -- the floor drifts with whatever the
# cheapest page happens to read.
#
# The lesson version one drew, "navigation is the smaller half of the shell",
# described the dashboard and is withdrawn.
#
# Raise these only with a recorded reason; a silent increase is the regression this
# file exists to catch.
SHELL_CONTEXT_PROCESSOR_QUERIES = 8
SHELL_FLOOR_BUDGET = 12  # cheapest observed page; an upper bound on the shell
PAGE_QUERY_BUDGETS = {"/": 24, "/clusters/": 14, "/vms/": 12}
SHELL_QUERY_BUDGET_PER_EXTRA_CLUSTER = 4

# Allowances for the surfaces Module 5 has not built yet (U0 item 4 requires
# numbers, not "bounded"). Each is an entry gate: exceeding it is a review reject,
# not a tuning task -- which only works if the number is *derived*, so each row
# carries the query plan it was costed from. A phase that needs a different plan
# argues the plan, not the number.
#
# Measurement protocol, so a phase and its reviewer cannot reach different figures
# in good faith: measure the page total warm, on the page named in the row, and
# subtract that page's pre-phase total. Not "delta over the floor" -- the floor
# contains `/vms/`'s own reads and would drift the gate.
MODULE5_QUERY_ALLOWANCES = {
    # 1 clusters + 1 membership rows + 1 node states + 1 guest counts, all bulk.
    # Paid on every page, so this is the row to defend hardest.
    "navigation tree (5a2B)": (4, "/vms/"),
    # tree cost + 1 cluster row + 1 coverage/generation + 1 guest aggregate.
    "cluster Summary (5a2C)": (6, "/clusters/<key>/"),
    # tree cost + 1 node row + 1 coverage + 1 guest-by-node aggregate.
    "node Summary (5a2D)": (6, "/clusters/<key>/nodes/<node>/"),
    # 1 managed cluster + 1 membership state + 1 node-state bulk read +
    # 1 all-domain coverage bulk read, no provider I/O.
    "first diagnostics read (5a1F)": (4, "service-level, no page"),
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
        # The LocMem cache is process-wide and Django does not reset it between
        # tests, while `datastore_nav` entries live 60s and the suite runs ~165s.
        # These tests both seed and warm cache entries, so they clear on the way in
        # and out rather than leaving keys for whatever runs next. This does not fix
        # the suite-wide isolation gap -- it just declines to widen it.
        cache.clear()
        self.addCleanup(cache.clear)
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

    def test_the_cheapest_page_bounds_the_shell(self):
        _seed_cluster("solo", nodes=3)

        floor = min(self._shell_queries(path) for path in PAGE_QUERY_BUDGETS)

        self.assertLessEqual(
            floor,
            SHELL_FLOOR_BUDGET,
            f"The cheapest page now costs {floor} queries against {SHELL_FLOOR_BUDGET}. "
            "This bounds the shell from above; it is not the shell's own cost, which "
            f"is {SHELL_CONTEXT_PROCESSOR_QUERIES} and is pinned separately below.",
        )

    def test_the_context_processor_is_the_real_shell_cost(self):
        """8, not the cheapest page -- every HTML response pays exactly this.

        Pinned on its own because the page figure has twice been mistaken for it,
        and because Module 5's tree lands here: growth in `app_settings` is paid by
        responses that render no page at all, including dialog fragments.
        """
        _seed_cluster("solo", nodes=3)
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        app_settings(request)  # warm the per-cluster datastore_nav cache

        with CaptureQueriesContext(connection) as captured:
            context = app_settings(request)
            list(context["app_recent_tasks"])  # the task bar is lazy; force it

        self.assertLessEqual(
            len(captured),
            SHELL_CONTEXT_PROCESSOR_QUERIES,
            f"`app_settings` now costs {len(captured)} queries against "
            f"{SHELL_CONTEXT_PROCESSOR_QUERIES}. This is the number every HTML "
            "response in the app pays, so it is the one Module 5 spends from.",
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

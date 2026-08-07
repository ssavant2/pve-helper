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

# Measured 2026-08-07, warm `datastore_nav` cache, one cluster of three nodes:
# **24 queries**, composed as
#
#   6  core_scanrun          \ Recent Tasks taskbar: 11 of 24, the dominant cost
#   5  core_auditevent       /
#   3  core_proxmoxcluster   \
#   2  core_clusterstorage    | navigation: 6
#   1  each of node-state, storage mount, cluster-storage mount, volume coverage
#   2  django_q_schedule
#   1  each of django_session, auth_user
#
# The number to notice is that navigation is the *smaller* half. Module 5 hangs its
# tree off the same context processor, so its budget is the headroom between 24 and
# whatever a page can afford -- not "one more query per node".
#
# Raise these only with a recorded reason; a silent increase is the regression this
# file exists to catch.
SHELL_QUERY_BUDGET = 24
SHELL_QUERY_BUDGET_PER_EXTRA_CLUSTER = 4


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

    def _shell_queries(self, path: str = "/") -> int:
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
        _seed_cluster("one", nodes=1)
        one_node = self._shell_queries()

        self._reset()
        _seed_cluster("three", nodes=3)
        three_nodes = self._shell_queries()

        self._reset()
        _seed_cluster("twenty", nodes=20)
        twenty_nodes = self._shell_queries()

        self.assertEqual(
            (one_node, three_nodes, twenty_nodes),
            (one_node, one_node, one_node),
            "The shared shell issued a different number of queries at 1, 3 and 20 "
            f"nodes ({one_node}/{three_nodes}/{twenty_nodes}). Navigation must "
            "compose nodes in bulk; a per-node query here multiplies across every "
            "HTML response in the app.",
        )

    def test_the_shell_stays_inside_its_measured_budget(self):
        _seed_cluster("solo", nodes=3)

        queries = self._shell_queries()

        self.assertLessEqual(
            queries,
            SHELL_QUERY_BUDGET,
            f"The shared shell now costs {queries} queries against a budget of "
            f"{SHELL_QUERY_BUDGET}. Module 5 hangs its tree and Summary panels off "
            "this same context processor, so growth here is paid on every page.",
        )

    def test_extra_clusters_cost_a_bounded_amount_each(self):
        _seed_cluster("alpha", nodes=3)
        one_cluster = self._shell_queries()

        _seed_cluster("beta", nodes=3)
        two_clusters = self._shell_queries()

        delta = two_clusters - one_cluster
        self.assertLessEqual(
            delta,
            SHELL_QUERY_BUDGET_PER_EXTRA_CLUSTER,
            f"A second cluster added {delta} queries to every page render, above the "
            f"{SHELL_QUERY_BUDGET_PER_EXTRA_CLUSTER} allowed. `app_settings` loops "
            "clusters deliberately; the per-cluster body is what must stay small.",
        )

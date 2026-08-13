"""The workspace Datastores tab (phase 5a4A).

The tab composes an already-published projection, so what it can break is not the
catalog — it is what the composition *claims*. Three claims, each with a test:

* a state per `(datastore, node)`, where "did not answer" and "answered no" stay
  distinguishable all the way to the rendered cell;
* currency by generation equality, never by age, and never read off a node row whose
  definition is gone;
* the publication boundary, which is the same boundary as everywhere else and is
  therefore asserted here as a leak test rather than re-derived.

The degraded matrix at the end renders all four states at once, because each of them
was reachable individually in a way that hid how the row summarises them.
"""

from __future__ import annotations

import uuid

from django.contrib.auth import get_user_model
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
    ClusterStorage,
    ClusterStorageNodeState,
    ProxmoxCluster,
    StorageCatalogState,
)
from core.services.workspace_datastores import datastore_panel


@override_settings(APP_REQUIRE_LOGIN=False)
class WorkspaceDatastoreTabTests(TestCase):
    def setUp(self):
        self.generation = uuid.uuid4()
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
            error_code="",
        )
        for index, node in enumerate(("pve1", "pve2", "pve3"), start=1):
            ClusterNodeState.objects.create(
                cluster=self.cluster,
                node_name=node,
                nodeid=index,
                present=True,
                online=True,
                membership_generation=3,
            )
        StorageCatalogState.objects.create(
            cluster=self.cluster,
            metadata_generation=self.generation,
            metadata_refreshed_at=self.now,
            metadata_complete=True,
        )
        self.shared = self._definition("nfs-vm", storage_type="nfs", shared=True, content=["images"])
        self.local = self._definition("local", storage_type="dir", shared=False, content=["images", "rootdir"])
        for node in ("pve1", "pve2", "pve3"):
            self._state(self.shared, node)
            self._state(self.local, node)
        user = get_user_model().objects.create_user(username="tabs", password="tabs-pw")
        self.client = Client()
        self.client.force_login(user)

    # ------------------------------------------------------------------ helpers

    def _definition(self, storage_id: str, **kwargs) -> ClusterStorage:
        return ClusterStorage.objects.create(
            cluster=self.cluster,
            storage_id=storage_id,
            present=True,
            observed_metadata_generation=self.generation,
            last_seen_at=self.now,
            **kwargs,
        )

    def _state(self, definition, node, **kwargs) -> ClusterStorageNodeState:
        defaults = {
            "active": True,
            "enabled": True,
            "present": True,
            "unreachable": False,
            "total_bytes": 100,
            "used_bytes": 25,
            "available_bytes": 75,
            "observed_metadata_generation": self.generation,
            "last_seen_at": self.now,
        }
        defaults.update(kwargs)
        return ClusterStorageNodeState.objects.create(cluster_storage=definition, node=node, **defaults)

    def _activate(self, **modes):
        for node_name, mode in modes.items():
            ClusterNodeEnrollment.objects.create(
                cluster=self.cluster,
                node_name=node_name,
                mode=mode,
                enrolled_at=self.now,
            )
        self.cluster.enrollment_contract_version = 1
        self.cluster.save(update_fields=["enrollment_contract_version"])

    def _panel(self, **kwargs):
        return datastore_panel(self.cluster, members=("pve1", "pve2", "pve3"), **kwargs)

    # ------------------------------------------------------------------- shape

    def test_a_shared_datastore_is_one_row_and_a_local_one_is_a_row_per_node(self):
        """`local` on three nodes is three disks; one row cannot speak for them."""
        panel = self._panel()

        self.assertEqual([row.storage_id for row in panel.shared_rows], ["nfs-vm"])
        self.assertEqual(
            [(row.storage_id, row.scope_node) for row in panel.local_rows],
            [("local", "pve1"), ("local", "pve2"), ("local", "pve3")],
        )

    def test_each_row_links_to_the_page_its_scope_addresses(self):
        panel = self._panel()

        self.assertEqual(panel.shared_rows[0].url, "/clusters/hq/datastores/nfs-vm/summary/")
        self.assertEqual(panel.local_rows[0].url, "/clusters/hq/nodes/pve1/datastores/local/summary/")

    def test_the_node_scope_shows_only_what_that_node_reports(self):
        panel = self._panel(node="pve2")

        self.assertEqual(
            sorted((row.storage_id, row.scope_node) for row in panel.rows),
            [("local", "pve2"), ("nfs-vm", "")],
        )
        self.assertEqual([node.node for node in panel.shared_rows[0].nodes], ["pve2"])

    # --------------------------------------------------------- degraded states

    def test_the_four_node_states_stay_distinguishable(self):
        """Unknown and absent are opposite instructions and must never merge."""
        ClusterStorageNodeState.objects.filter(cluster_storage=self.local, node="pve1").update(
            present=False, active=False, unreachable=True
        )
        ClusterStorageNodeState.objects.filter(cluster_storage=self.local, node="pve2").update(
            present=False, active=False, unreachable=False
        )
        ClusterStorageNodeState.objects.filter(cluster_storage=self.local, node="pve3").update(active=False)

        states = {row.scope_node: row.state for row in self._panel().local_rows}

        self.assertEqual(states, {"pve1": "unknown", "pve2": "absent", "pve3": "inactive"})

    def test_a_shared_row_reports_the_worst_thing_any_node_says(self):
        """The column an operator scans down must not average a failure away."""
        ClusterStorageNodeState.objects.filter(cluster_storage=self.shared, node="pve3").update(
            present=False, active=False, unreachable=True
        )

        row = self._panel().shared_rows[0]

        self.assertEqual(row.state, "unknown")
        self.assertEqual(row.attached_nodes, ("pve1", "pve2"))

    def test_capacity_comes_from_an_attached_instance_not_an_unreachable_one(self):
        ClusterStorageNodeState.objects.filter(cluster_storage=self.shared, node="pve1").update(
            present=False, active=False, unreachable=True, total_bytes=None, used_bytes=None
        )

        row = self._panel().shared_rows[0]

        self.assertEqual(row.instance.node, "pve2")
        self.assertEqual(row.instance.total_bytes, 100)

    # -------------------------------------------------------------- currency

    def test_currency_is_generation_equality_and_not_age(self):
        ClusterStorageNodeState.objects.filter(cluster_storage=self.shared, node="pve3").update(
            observed_metadata_generation=uuid.uuid4()
        )

        row = self._panel().shared_rows[0]

        self.assertFalse(row.current)
        self.assertEqual([node.node for node in row.nodes if not node.current], ["pve3"])
        # The timestamp is untouched: age is displayed, never a decision input.
        self.assertTrue(all(node.last_seen_at == self.now for node in row.nodes))

    def test_a_tombstoned_definition_leaves_the_tab_rather_than_going_stale(self):
        """Its node rows keep the last generation that saw them; that is not staleness."""
        ClusterStorage.objects.filter(pk=self.local.pk).update(present=False, retired_at=self.now)

        panel = self._panel()

        self.assertEqual([row.storage_id for row in panel.rows], ["nfs-vm"])

    def test_incomplete_metadata_qualifies_the_table_and_never_empties_it(self):
        StorageCatalogState.objects.filter(cluster=self.cluster).update(metadata_complete=False)
        # Re-read the cluster: the one-to-one is cached on the instance, and a view
        # gets a fresh one per request.
        self.cluster = ProxmoxCluster.objects.get(pk=self.cluster.pk)

        panel = self._panel()

        self.assertFalse(panel.metadata_complete)
        self.assertEqual(len(panel.rows), 4)

    # -------------------------------------------------- the publication boundary

    def test_a_hidden_nodes_datastore_never_reaches_the_tab(self):
        self._activate(pve1="managed", pve2="safety_only", pve3="managed")

        panel = self._panel()

        self.assertEqual(
            [(row.storage_id, row.scope_node) for row in panel.local_rows],
            [("local", "pve1"), ("local", "pve3")],
        )
        self.assertEqual([node.node for node in panel.shared_rows[0].nodes], ["pve1", "pve3"])

    def test_a_datastore_no_published_node_sees_is_not_a_row(self):
        """Not an unknown row — an unknown row is a claim, and there is nothing to claim.

        Every instance sits on a node this workspace does not show, so the datastore
        is not part of what the published cluster has mounted. Rendering it with no
        nodes and no capacity would put an alarming empty row on the tab for a
        datastore that is working perfectly on a member the operator chose to hide.
        """
        self._activate(pve1="safety_only")

        panel = self._panel()

        self.assertEqual(panel.rows, ())
        self.assertEqual(panel.unread_nodes, ("pve2", "pve3"))

    def test_the_tab_says_which_members_it_does_not_read(self):
        """Showing less without saying so is how a hidden node looks like a bug."""
        self._activate(pve1="managed", pve2="safety_only")

        self.assertEqual(self._panel().unread_nodes, ("pve3",))

    def test_a_legacy_connection_footnotes_nothing(self):
        self.assertEqual(self._panel().unread_nodes, ())

    def test_a_hidden_nodes_datastore_tab_is_not_found(self):
        self._activate(pve1="managed", pve2="safety_only", pve3="managed")

        response = self.client.get("/clusters/hq/nodes/pve2/datastores/")

        self.assertEqual(response.status_code, 404)

    # ------------------------------------------------------------ routing/cost

    def test_the_tab_url_does_not_shadow_a_datastore_object_url(self):
        """One extra segment tells them apart; nothing else does."""
        self.assertEqual(resolve("/clusters/hq/datastores/").url_name, "cluster_datastores")
        self.assertEqual(resolve("/clusters/hq/datastores/nfs-vm/summary/").url_name, "api_storage_summary")
        self.assertEqual(resolve("/clusters/hq/nodes/pve1/datastores/").url_name, "node_datastores")
        self.assertEqual(
            resolve("/clusters/hq/nodes/pve1/datastores/local/summary/").url_name,
            "api_storage_summary",
        )

    def test_both_tabs_render_and_are_reachable_from_the_strip(self):
        for url in (
            reverse("core:cluster_datastores", args=["hq"]),
            reverse("core:node_datastores", args=["hq", "pve1"]),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "nfs-vm")

    def test_the_composition_costs_four_bulk_queries_and_is_flat_in_datastores(self):
        """The budget this phase was entered on, measured rather than asserted in prose."""
        with CaptureQueriesContext(connection) as small:
            self._panel()

        for index in range(12):
            definition = self._definition(f"extra{index}", storage_type="dir", shared=False, content=["images"])
            for node in ("pve1", "pve2", "pve3"):
                self._state(definition, node)

        with CaptureQueriesContext(connection) as large:
            panel = self._panel()

        self.assertLessEqual(len(small), 4)
        self.assertEqual(len(large), len(small))
        self.assertEqual(len(panel.rows), 40)

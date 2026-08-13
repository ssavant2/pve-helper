"""N5 — the storage half of the node-enrollment boundary (phase 5a4A).

Enrollment already decided which nodes' *guests* pve-helper publishes. Storage was
the half still missing, and it is not the same rule applied to a second table:

* ``managed`` is read and shown.
* ``safety_only`` is **read and not shown**. It contributes the volume evidence
  that keeps absence provable, so hiding a node must not turn a covered datastore
  into an unknown one. Every test below that pins "does not lower confidence" is
  guarding that asymmetry.
* unenrolled is **not read at all**, and the consequence is the interesting one:
  an absence claim over a storage that member could have mounted is no longer a
  claim about the cluster. It degrades to unknown, and unknown is answerable —
  never a refusal, because the node it names is one the operator took out of scope
  and the file would otherwise be stranded behind a decision made elsewhere.

A contract-0 connection publishes exactly as it did before enrollment existed;
the last test here is the one that says so.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import Http404
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from core.models import (
    ClusterNodeEnrollment,
    ClusterNodeState,
    ClusterStorage,
    ClusterStorageMount,
    ClusterStorageNodeState,
    CurrentGuestInventory,
    FileInventory,
    ProxmoxCluster,
    ScanRun,
    StorageMount,
)
from core.services.datastore_nav import datastore_nav
from core.services.file_actions import file_action_risk
from core.services.publication_scope import ENROLLMENT_CONTRACT_ACTIVE, publication_scope
from core.services.storage_catalog import (
    UsageState,
    node_storage_rows,
    refresh_storage_metadata,
    refresh_storage_volumes,
    storage_view,
    unobserved_eligible_nodes,
    usage_preflight,
)
from core.tests_storage_catalog import FakeStorageClient
from core.views.dashboard import _storage_catalog_rows
from core.views.storage.api import _resolve_datastore_scope


class _RecordingClient(FakeStorageClient):
    """A fake that remembers what it was asked, so "never contacted" is testable."""

    def __init__(self, responses):
        super().__init__(responses)
        self.paths: list[str] = []

    def get(self, path):
        self.paths.append(path)
        return super().get(path)


@override_settings(APP_REQUIRE_LOGIN=False)
class StorageEnrollmentBoundaryTests(TestCase):
    def setUp(self):
        self.cluster = ProxmoxCluster.objects.create(key="cluster-n5", display_name="Cluster N5")
        self.responses = {
            "storage": [
                {"storage": "shared", "type": "rbd", "shared": 1, "content": "images"},
                {"storage": "local", "type": "dir", "shared": 0, "content": "images,rootdir"},
            ],
            "nodes": [
                {"node": "pve1", "status": "online"},
                {"node": "pve2", "status": "online"},
                {"node": "pve3", "status": "online"},
            ],
        }
        for node in ("pve1", "pve2", "pve3"):
            self.responses[f"nodes/{node}/storage"] = [
                {"storage": "shared", "active": 1, "enabled": 1, "total": 100, "used": 10, "avail": 90},
                {"storage": "local", "active": 1, "enabled": 1, "total": 50, "used": 5, "avail": 45},
            ]
            self.responses[f"nodes/{node}/storage/shared/content"] = [
                {"volid": "shared:100/vm-100-disk-0.qcow2", "vmid": 100, "content": "images", "size": 10},
            ]
            self.responses[f"nodes/{node}/storage/local/content"] = [
                {"volid": f"local:vm-10-disk-0@{node}", "vmid": 10, "content": "images", "size": 20},
            ]
            ClusterNodeState.objects.create(
                cluster=self.cluster,
                node_name=node,
                present=True,
                online=True,
            )
        self.client = _RecordingClient(self.responses)

    # ------------------------------------------------------------------ helpers

    def _enroll(self, **modes: str) -> None:
        """Activate the contract with an exact set. Absent names are unenrolled."""
        for node, mode in modes.items():
            ClusterNodeEnrollment.objects.create(
                cluster=self.cluster,
                node_name=node,
                mode=mode,
                enrolled_at=timezone.now(),
            )
        self.cluster.enrollment_contract_version = ENROLLMENT_CONTRACT_ACTIVE
        self.cluster.enrollment_generation = 1
        self.cluster.save(update_fields=["enrollment_contract_version", "enrollment_generation"])

    def _refresh(self):
        with patch("core.services.storage_catalog.cluster_clients", return_value=[self.client]):
            metadata = refresh_storage_metadata(self.cluster)
            volumes = refresh_storage_volumes(self.cluster)
        return metadata, volumes

    def _definition(self, storage_id: str) -> ClusterStorage:
        return (
            ClusterStorage.objects.filter(cluster=self.cluster, storage_id=storage_id)
            .select_related("cluster__storage_catalog_state")
            .prefetch_related("node_states", "mount_bindings__mount", "volume_coverages")
            .get()
        )

    # ------------------------------------------------------- the read boundary

    def test_an_unenrolled_member_is_never_contacted(self):
        self._enroll(pve1="managed", pve2="safety_only")

        self._refresh()

        self.assertNotIn("nodes/pve3/storage", self.client.paths)
        self.assertNotIn("nodes/pve3/storage/local/content", self.client.paths)
        # ...and nothing it might have said becomes a row.
        self.assertFalse(ClusterStorageNodeState.objects.filter(cluster_storage__cluster=self.cluster, node="pve3"))

    def test_a_safety_only_member_is_contacted_and_hidden(self):
        """The asymmetry in one test: read for evidence, absent from the view."""
        self._enroll(pve1="managed", pve2="safety_only")

        self._refresh()

        self.assertIn("nodes/pve2/storage", self.client.paths)
        view = storage_view(self._definition("shared"))
        self.assertEqual([row.node for row in view.observed_nodes], ["pve1", "pve2"])
        self.assertEqual([row.node for row in view.nodes], ["pve1"])

    def test_hiding_a_node_does_not_lower_coverage(self):
        """`safety_only` withholds nothing, so the datastore stays fully covered."""
        self._enroll(pve1="safety_only", pve2="managed")

        _, volumes = self._refresh()

        self.assertTrue(volumes.volume_complete)
        self.assertTrue(storage_view(self._definition("shared")).coverage_complete)

    def test_coverage_survives_when_every_node_holding_it_is_hidden(self):
        """The sharp version of the rule above.

        With no published instance left, a view that made its capability decisions
        from the published set would report "No permitted active node" — a hidden
        member turned into a coverage failure, which is precisely what `safety_only`
        exists not to be. The datastore has nothing to show and everything to prove.
        """
        self._enroll(pve1="safety_only")

        _, volumes = self._refresh()

        view = storage_view(self._definition("shared"))
        self.assertEqual(view.nodes, ())
        self.assertEqual([row.node for row in view.observed_nodes], ["pve1"])
        self.assertTrue(view.coverage_complete)
        self.assertEqual(view.coverage_reason, "")
        self.assertTrue(volumes.volume_complete)

    def test_a_hidden_nodes_own_disk_is_still_answerable_about(self):
        """The safety path addresses a node-local storage on a node it will not show.

        Asked about `local` on the hidden node, the view must answer from evidence
        rather than refuse the scope as inactive: refusing is how a hidden node's
        disk becomes an orphan candidate.
        """
        self._enroll(pve1="managed", pve2="safety_only")
        self._refresh()

        view = storage_view(self._definition("local"), node="pve2")

        self.assertTrue(view.coverage_complete)
        self.assertEqual(view.coverage_reason, "")
        self.assertNotIn("pve2", [row.node for row in view.nodes])

    def test_unenrolling_a_node_reports_unknown_rather_than_removal(self):
        """Not asking is not proof. The row survives as an explicit unknown."""
        self._enroll(pve1="managed", pve2="managed", pve3="managed")
        self._refresh()
        self.assertTrue(
            ClusterStorageNodeState.objects.get(
                cluster_storage__cluster=self.cluster, cluster_storage__storage_id="shared", node="pve3"
            ).present
        )

        ClusterNodeEnrollment.objects.filter(cluster=self.cluster, node_name="pve3").delete()
        self._refresh()

        state = ClusterStorageNodeState.objects.get(
            cluster_storage__cluster=self.cluster, cluster_storage__storage_id="shared", node="pve3"
        )
        self.assertFalse(state.present)
        self.assertTrue(state.unreachable, "An unread node's storage is unknown, not proven absent.")

    def test_a_node_that_answered_that_the_storage_is_gone_keeps_its_proof(self):
        """The other half of the sweep: absence with proof must stay distinguishable."""
        self._enroll(pve1="managed", pve2="managed", pve3="managed")
        self._refresh()

        self.responses["storage"] = [{"storage": "shared", "type": "rbd", "shared": 1, "content": "images"}]
        self._refresh()

        state = ClusterStorageNodeState.objects.get(
            cluster_storage__cluster=self.cluster, cluster_storage__storage_id="local", node="pve1"
        )
        self.assertFalse(state.present)
        self.assertFalse(state.unreachable)

    def test_a_nodes_restriction_narrows_the_observed_set_and_never_widens_it(self):
        """`nodes=` is configuration, not an enrollment override.

        No dev cluster carries a `nodes=`-restricted definition, so this branch is
        covered by fixture alone — recorded as such in 5a4A's readiness pack.
        """
        self.responses["storage"] = [
            {"storage": "shared", "type": "rbd", "shared": 1, "content": "images", "nodes": "pve1,pve3"},
        ]
        self._enroll(pve1="managed", pve2="managed")

        self._refresh()

        self.assertEqual(
            sorted(
                ClusterStorageNodeState.objects.filter(cluster_storage__cluster=self.cluster).values_list(
                    "node", flat=True
                )
            ),
            ["pve1"],
        )

    # ------------------------------------------------------- absence confidence

    def test_an_eligible_unenrolled_member_makes_absence_unknown(self):
        self._enroll(pve1="managed", pve2="managed")
        self._refresh()

        result = usage_preflight(
            self._definition("shared"),
            volid="shared:100/vm-999-disk-0.qcow2",
            fresh=False,
        )

        self.assertIs(result.state, UsageState.UNKNOWN)
        self.assertIn("pve3", result.reason)

    def test_a_safety_only_member_leaves_absence_provable(self):
        self._enroll(pve1="managed", pve2="safety_only", pve3="safety_only")
        self._refresh()

        result = usage_preflight(
            self._definition("shared"),
            volid="shared:100/vm-999-disk-0.qcow2",
            fresh=False,
        )

        self.assertIs(result.state, UsageState.UNREFERENCED)

    def test_a_hidden_nodes_guest_still_holds_its_disk(self):
        """The regression that replaces the old unscanned-node orphan test."""
        self._enroll(pve1="managed", pve2="safety_only", pve3="safety_only")
        self._refresh()
        CurrentGuestInventory.objects.create(
            cluster=self.cluster,
            node="pve2",
            object_type="vm",
            vmid=100,
            name="hidden",
            status="running",
            observed_at=timezone.now(),
            published=False,
            disk_references=["shared:100/vm-100-disk-0.qcow2"],
        )

        result = usage_preflight(
            self._definition("shared"),
            volid="shared:100/vm-100-disk-0.qcow2",
            fresh=False,
        )

        self.assertIs(result.state, UsageState.REFERENCED)

    def test_a_reference_outranks_an_unread_member(self):
        """An unread node can withhold evidence; it cannot erase what was found."""
        self._enroll(pve1="managed", pve2="managed")
        self._refresh()
        CurrentGuestInventory.objects.create(
            cluster=self.cluster,
            node="pve1",
            object_type="vm",
            vmid=100,
            name="visible",
            status="running",
            observed_at=timezone.now(),
            published=True,
            disk_references=["shared:100/vm-100-disk-0.qcow2"],
        )

        result = usage_preflight(
            self._definition("shared"),
            volid="shared:100/vm-100-disk-0.qcow2",
            fresh=False,
        )

        self.assertIs(result.state, UsageState.REFERENCED)

    def test_an_unenrolled_member_outside_the_nodes_restriction_is_irrelevant(self):
        self.responses["storage"] = [
            {"storage": "shared", "type": "rbd", "shared": 1, "content": "images", "nodes": "pve1,pve2"},
        ]
        self._enroll(pve1="managed", pve2="managed")
        self._refresh()

        self.assertEqual(unobserved_eligible_nodes(self._definition("shared")), ())

    # -------------------------------------------------------- publication seams

    def test_a_hidden_nodes_datastore_leaves_the_navigation_tree(self):
        self._enroll(pve1="managed", pve2="safety_only")
        self._refresh()

        tree = datastore_nav(cluster=self.cluster, use_cache=False)

        self.assertEqual([group["node"] for group in tree["nodes"]], ["pve1"])
        self.assertEqual([entry["storage_id"] for entry in tree["shared"]], ["shared"])

    def test_a_hidden_nodes_datastore_page_is_not_found(self):
        """404, not a refusal: from this workspace the disk is simply not there."""
        self._enroll(pve1="managed", pve2="safety_only")
        self._refresh()

        self.assertEqual(_resolve_datastore_scope(self.cluster, "local", "pve1")[1], "pve1")
        with self.assertRaises(Http404):
            _resolve_datastore_scope(self.cluster, "local", "pve2")

    def test_operation_forms_are_offered_no_hidden_node(self):
        self._enroll(pve1="managed", pve2="safety_only")
        self._refresh()

        self.assertTrue(node_storage_rows(self.cluster, "pve1"))
        self.assertEqual(node_storage_rows(self.cluster, "pve2"), [])

    def test_the_dashboard_drops_a_card_no_published_node_can_see(self):
        """A card is a cluster-wide heading; its capacity must not be a hidden node's."""
        self._enroll(pve1="managed", pve2="safety_only")
        self._refresh()

        rows = _storage_catalog_rows()

        self.assertEqual(
            sorted((row["definition"].storage_id, row["node"]) for row in rows),
            [("local", "pve1"), ("shared", "pve1")],
        )

    def test_the_dashboard_drops_every_card_when_no_node_is_published(self):
        self._enroll(pve1="safety_only")

        self._refresh()

        self.assertEqual(_storage_catalog_rows(), [])

    def test_a_mount_cannot_be_registered_against_a_hidden_node(self):
        """The picker and the submit check are the same rule, stated twice.

        Binding a host mount to a hidden node would put that node back on screen
        through the mount — a second door into the thing enrollment closed.
        """
        self._enroll(pve1="managed", pve2="safety_only")
        self._refresh()
        self.cluster.enabled = True
        self.cluster.save(update_fields=["enabled"])
        user = get_user_model().objects.create_user(username="mounts", password="mounts-pw")
        client = Client()
        client.force_login(user)

        response = client.get("/settings/storage/")

        options = {
            option["label"].split(" · ")[1].split(" ")[0]: option["nodes"]
            for option in response.context["definition_options"]
        }
        self.assertEqual(options["local"], ["pve1"])

        # ...and the submit path refuses it independently, because a form rendered
        # before the node was hidden is still sitting in somebody's browser.
        submitted = client.post(
            "/settings/storage/",
            {
                "cluster_storage": str(self._definition("local").pk),
                "relative_path": "nowhere",
                "node": "pve2",
                "display_name": "hidden-local",
                "backend_identity": "/dev/nowhere",
            },
        )

        self.assertContains(
            submitted,
            "Choose the node-local storage instance this mount represents.",
        )

    # ------------------------------------------------------- the file-action gate

    def _file_entry(self):
        """One guest disk on a host mount bound to the shared datastore."""
        mount = StorageMount.objects.create(
            storage_id="nfs-vm",
            display_name="nfs-vm",
            export="truenas.example.com:/mnt/tank/vm",
            path="/storages/nfs-vm",
            backend_identity="truenas.example.com:/mnt/tank/vm",
        )
        ClusterStorageMount.objects.create(
            cluster_storage=self._definition("shared"),
            mount=mount,
            scope=ClusterStorageMount.Scope.SHARED,
        )
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED, storage_gate_status={})
        return FileInventory.objects.create(
            scan_run=scan,
            storage=mount,
            path="images/100/vm-100-disk-0.qcow2",
            size_bytes=1,
            content_category="vm_disk",
            classification=FileInventory.Classification.REFERENCED,
        )

    def test_an_unenrolled_member_is_answerable_and_never_a_refusal(self):
        """The gate escalates rather than strands.

        The node it names is one the operator took out of scope, so a refusal here
        would demand a repair in a place the file action cannot reach — and the
        operator would have no way back to their own disk.
        """
        self._enroll(pve1="managed", pve2="managed")
        self._refresh()

        risk = file_action_risk(self._file_entry())

        self.assertFalse(risk.blocked)
        self.assertTrue(risk.acknowledgeable)
        self.assertIn("pve3", risk.unverified_nodes)
        self.assertIn("pve3", risk.warning_message)

    def test_a_fully_enrolled_cluster_asks_nothing_extra(self):
        self._enroll(pve1="managed", pve2="safety_only", pve3="safety_only")
        self._refresh()

        risk = file_action_risk(self._file_entry())

        self.assertEqual(risk.unverified_nodes, ())

    # ------------------------------------------------------------- legacy scope

    def test_a_contract_zero_connection_is_unchanged(self):
        """No enrollment rows, no filtering, every node read and published."""
        self._refresh()

        self.assertIn("nodes/pve3/storage", self.client.paths)
        view = storage_view(self._definition("shared"))
        self.assertEqual([row.node for row in view.nodes], ["pve1", "pve2", "pve3"])
        self.assertEqual(view.nodes, view.observed_nodes)
        self.assertFalse(publication_scope(self.cluster).filtering)
        self.assertEqual(unobserved_eligible_nodes(self._definition("shared")), ())

    def test_a_contract_zero_connection_does_not_read_the_membership_table(self):
        """A legacy connection publishes everything, so the question is not asked.

        The answer would be the same either way — this pins the *cost*, because the
        preflight path calls it per definition and a legacy installation must not
        start paying a membership read per datastore to be told nothing changed.
        """
        self._refresh()
        definition = self._definition("shared")
        scope = publication_scope(self.cluster)

        with self.assertNumQueries(0):
            unobserved_eligible_nodes(definition, scope=scope)

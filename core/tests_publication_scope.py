"""The publication filter: what an activated cluster may show, and what it must keep.

The decisions under test are that legacy clusters are untouched, that an activated
cluster is authoritative even when empty, that hiding a node marks its rows instead of
deleting them, and that both reconcilers apply the same seam — filtering one and not
the other is the same as not filtering at all.
"""

from __future__ import annotations

from types import SimpleNamespace

from django.test import TestCase
from django.utils import timezone

from core.models import (
    ClusterNodeEnrollment,
    CurrentGuestInventory,
    ProxmoxCluster,
    ProxmoxEndpoint,
    ScanRun,
)
from core.services.cluster_enrollment import activate_cluster_enrollment, change_enrollment_mode
from core.services.current_guest_inventory import (
    ScanGuestObservation,
    reconcile_live_guest_inventory,
    reconcile_scan_guest_inventory,
)
from core.services.proxmox import ProxmoxGuestSummary, VerifiedGuestInventory
from core.services.publication_scope import apply_publication_scope, publication_scope
from core.tests_cluster_node_enrollment import _publish_membership


class PublicationScopeTests(TestCase):
    def setUp(self):
        self.cluster = ProxmoxCluster.objects.create(key="pub", display_name="Pub", enabled=True)
        self.endpoint = ProxmoxEndpoint.objects.create(name="pve1", url="https://pve1:8006", cluster=self.cluster)
        self.scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)

    def _enroll(self, node, mode):
        return ClusterNodeEnrollment.objects.create(
            cluster=self.cluster,
            node_name=node,
            mode=mode,
            enrolled_at=timezone.now(),
        )

    def _activate(self, version=1):
        self.cluster.enrollment_contract_version = version
        self.cluster.save(update_fields=["enrollment_contract_version"])

    def _guest_row(self, node, vmid):
        return CurrentGuestInventory.objects.create(
            cluster=self.cluster,
            node=node,
            object_type="vm",
            vmid=vmid,
            name=f"vm{vmid}",
            config={},
            observed_at=timezone.now(),
        )

    def _scan_guest(self, node, vmid):
        return SimpleNamespace(
            node=node,
            object_type="vm",
            vmid=vmid,
            name=f"vm{vmid}",
            status="running",
            config={},
            disk_references=[],
        )

    def _reconcile_scan(self, *guests, nodes):
        return reconcile_scan_guest_inventory(
            scan=self.scan,
            observations=[ScanGuestObservation(self.endpoint, guest) for guest in guests],
            attempted_endpoints=[self.endpoint],
            successful_endpoints=[self.endpoint],
            attempted_nodes={self.cluster.pk: set(nodes)},
            covered_nodes={self.cluster.pk: set(nodes)},
            errors={},
        )

    def _reconcile_live(self, *placements):
        reconcile_live_guest_inventory(
            VerifiedGuestInventory(
                cluster_key=self.cluster.key,
                guests=tuple(
                    ProxmoxGuestSummary(node=node, object_type="vm", vmid=vmid, name=f"vm{vmid}", status="running")
                    for node, vmid in placements
                ),
                attempted_endpoints=(self.endpoint.url,),
                successful_endpoints=(self.endpoint.url,),
                errors=(),
            )
        )

    # --- legacy ----------------------------------------------------------------

    def test_legacy_cluster_publishes_every_node(self):
        scope = publication_scope(self.cluster)
        self.assertFalse(scope.filtering)
        self.assertTrue(scope.publishes("never-heard-of-it"))
        self.assertTrue(scope.observes("never-heard-of-it"))

    def test_scan_on_a_legacy_cluster_publishes_unenrolled_nodes(self):
        self._reconcile_scan(self._scan_guest("pve2", 100), nodes={"pve2"})
        self.assertTrue(CurrentGuestInventory.objects.get(vmid=100).published)

    # --- activated -------------------------------------------------------------

    def test_activated_cluster_publishes_only_managed_nodes(self):
        self._enroll("pve1", ClusterNodeEnrollment.Mode.MANAGED)
        self._enroll("pve2", ClusterNodeEnrollment.Mode.SAFETY_ONLY)
        self._activate()

        scope = publication_scope(self.cluster)
        self.assertTrue(scope.publishes("pve1"))
        self.assertFalse(scope.publishes("pve2"))
        self.assertFalse(scope.publishes("pve3"))
        # Safety evidence survives hiding; it never survives being unknown.
        self.assertTrue(scope.observes("pve2"))
        self.assertFalse(scope.observes("pve3"))

    def test_activated_cluster_with_no_enrollments_publishes_nothing(self):
        self._activate()
        scope = publication_scope(self.cluster)
        self.assertTrue(scope.filtering)
        self.assertFalse(scope.publishes("pve1"))

    def test_scan_marks_unenrolled_rows_unpublished_without_deleting_them(self):
        self._enroll("pve1", ClusterNodeEnrollment.Mode.MANAGED)
        self._enroll("pve2", ClusterNodeEnrollment.Mode.SAFETY_ONLY)
        self._activate()

        self._reconcile_scan(
            self._scan_guest("pve1", 100),
            self._scan_guest("pve2", 200),
            nodes={"pve1", "pve2"},
        )

        self.assertTrue(CurrentGuestInventory.objects.get(vmid=100).published)
        hidden = CurrentGuestInventory.objects.get(vmid=200)
        self.assertFalse(hidden.published)
        self.assertEqual(hidden.node, "pve2")

    def test_live_reconcile_applies_the_same_filter_as_the_scan(self):
        """The reconcile that runs every minute is the one that would undo the filter."""
        self._enroll("pve1", ClusterNodeEnrollment.Mode.MANAGED)
        self._enroll("pve2", ClusterNodeEnrollment.Mode.SAFETY_ONLY)
        self._activate()
        self._reconcile_scan(self._scan_guest("pve1", 100), self._scan_guest("pve2", 200), nodes={"pve1", "pve2"})

        self._reconcile_live(("pve1", 100), ("pve2", 200))

        self.assertTrue(CurrentGuestInventory.objects.get(vmid=100).published)
        self.assertFalse(CurrentGuestInventory.objects.get(vmid=200).published)

    def test_rows_carry_the_generation_they_were_published_under(self):
        self._enroll("pve1", ClusterNodeEnrollment.Mode.MANAGED)
        self._activate()
        self.cluster.enrollment_generation = 7
        self.cluster.save(update_fields=["enrollment_generation"])

        self._reconcile_scan(self._scan_guest("pve1", 100), nodes={"pve1"})

        self.assertEqual(CurrentGuestInventory.objects.get(vmid=100).based_on_enrollment_generation, 7)

    # --- re-stamping on enrollment change --------------------------------------

    def test_apply_restamps_stored_rows_without_a_reconcile(self):
        self._guest_row("pve1", 100)
        self._guest_row("pve2", 200)
        self._enroll("pve1", ClusterNodeEnrollment.Mode.MANAGED)
        self._activate()

        apply_publication_scope(self.cluster)

        self.assertTrue(CurrentGuestInventory.objects.get(vmid=100).published)
        self.assertFalse(CurrentGuestInventory.objects.get(vmid=200).published)

    def test_hiding_a_node_takes_effect_at_the_click_not_at_the_next_reconcile(self):
        self._guest_row("pve1", 100)
        self._guest_row("pve2", 200)
        self._enroll("pve1", ClusterNodeEnrollment.Mode.MANAGED)
        self._enroll("pve2", ClusterNodeEnrollment.Mode.MANAGED)
        self._activate()
        apply_publication_scope(self.cluster)
        self.assertTrue(CurrentGuestInventory.objects.get(vmid=200).published)

        change_enrollment_mode(self.cluster, node_name="pve2", mode=ClusterNodeEnrollment.Mode.SAFETY_ONLY)

        hidden = CurrentGuestInventory.objects.get(vmid=200)
        self.assertFalse(hidden.published)
        self.assertTrue(CurrentGuestInventory.objects.filter(vmid=200).exists())
        self.assertTrue(CurrentGuestInventory.objects.get(vmid=100).published)

    def test_activation_filters_immediately(self):
        """Activation bumps the version before committing the generation.

        Committed the other way round, the re-stamp would run under the legacy
        contract and activation would appear to do nothing until the next change.
        """
        self._guest_row("pve1", 100)
        self._guest_row("pve2", 200)
        _publish_membership(self.cluster, "pve1", "pve2")

        activate_cluster_enrollment(
            self.cluster,
            selections={"pve1": ClusterNodeEnrollment.Mode.MANAGED},
        )

        self.assertTrue(CurrentGuestInventory.objects.get(vmid=100).published)
        self.assertFalse(CurrentGuestInventory.objects.get(vmid=200).published)

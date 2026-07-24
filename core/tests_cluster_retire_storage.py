"""R2 storage-owner contract for cluster retirement."""

from __future__ import annotations

import uuid
from unittest.mock import patch

from django.db import models, transaction
from django.test import TestCase
from django.utils import timezone

from core.models import (
    ClusterStorage,
    ClusterStorageMount,
    ClusterStorageNodeState,
    ClusterStorageVolumeCoverage,
    ClusterStorageVolumeObservation,
    ProxmoxCluster,
    ProxmoxStorageConsumer,
    StorageCatalogState,
    StorageMount,
    StorageSpaceSnapshot,
)
from core.services.datastore_nav import datastore_nav
from core.services.storage_retirement import (
    StorageRetirementConsumersBlock,
    StorageRetirementImpactChanged,
    cluster_retirement_storage_preflight,
    finalize_cluster_retirement_storage,
)
from core.tasks import _storage_gate_status
from core.tests_cluster_retire import make_cluster, shared_storage_with_consumers


def _published_storage(cluster: ProxmoxCluster, storage_id: str = "shared"):
    metadata_generation = uuid.uuid4()
    volume_generation = uuid.uuid4()
    StorageCatalogState.objects.create(
        cluster=cluster,
        metadata_generation=metadata_generation,
        metadata_refreshed_at=timezone.now(),
        metadata_complete=True,
        volume_refreshed_at=timezone.now(),
        volume_complete=True,
    )
    definition = ClusterStorage.objects.create(
        cluster=cluster,
        storage_id=storage_id,
        storage_type="nfs",
        content=["images"],
        shared=True,
        present=True,
        observed_metadata_generation=metadata_generation,
        last_seen_at=timezone.now(),
    )
    ClusterStorageNodeState.objects.create(
        cluster_storage=definition,
        node="pve1",
        active=True,
        enabled=True,
        present=True,
        observed_metadata_generation=metadata_generation,
        last_seen_at=timezone.now(),
    )
    ClusterStorageVolumeCoverage.objects.create(
        cluster_storage=definition,
        scope=ClusterStorageVolumeCoverage.Scope.SHARED,
        node=None,
        volume_generation=volume_generation,
        based_on_metadata_generation=metadata_generation,
        refreshed_at=timezone.now(),
        complete=True,
        agreeing_nodes=["pve1"],
    )
    ClusterStorageVolumeObservation.objects.create(
        cluster_storage=definition,
        node="",
        volid=f"{storage_id}:100/vm-100-disk-0.qcow2",
        vmid=100,
        content="images",
        volume_format="qcow2",
        observed_volume_generation=volume_generation,
        based_on_metadata_generation=metadata_generation,
        last_seen_at=timezone.now(),
    )
    mount = StorageMount.objects.create(
        storage_id=f"{storage_id}-mount",
        display_name=f"{storage_id} mount",
        relative_path=f"{storage_id}-mount",
        backend_identity=f"nas:/{storage_id}",
    )
    binding = ClusterStorageMount.objects.create(
        cluster_storage=definition,
        mount=mount,
        scope=ClusterStorageMount.Scope.SHARED,
        node=None,
    )
    return definition, mount, binding


class StorageRetirementPreflightTests(TestCase):
    def test_verified_preflight_enumerates_each_consumer_and_blocks(self):
        storage, cluster, _other = shared_storage_with_consumers()
        consumer = ProxmoxStorageConsumer.objects.get(storage=storage, cluster=cluster)
        consumer.last_successful_inventory_scan = timezone.now()
        consumer.last_gate_status = "unavailable"
        consumer.save()

        preflight = cluster_retirement_storage_preflight(
            cluster,
            mode=ProxmoxCluster.RetirementMode.VERIFIED,
        )

        self.assertFalse(preflight.consumer_gate_clear)
        self.assertEqual(len(preflight.consumers), 1)
        impact = preflight.consumers[0]
        self.assertEqual(impact.pk, consumer.pk)
        self.assertEqual(impact.storage_id, "shared-nfs")
        self.assertEqual(impact.node_ref, f"nr1:{cluster.key}:pve1")
        self.assertEqual(impact.last_gate_status, "unavailable")
        with transaction.atomic():
            with self.assertRaises(StorageRetirementConsumersBlock):
                finalize_cluster_retirement_storage(
                    cluster,
                    mode=ProxmoxCluster.RetirementMode.VERIFIED,
                    expected_digest=preflight.impact_digest,
                )
        self.assertTrue(ProxmoxStorageConsumer.objects.filter(pk=consumer.pk).exists())

    def test_changed_storage_impact_rejects_the_stale_digest_without_cleanup(self):
        cluster = make_cluster("stale-storage")
        definition, _mount, binding = _published_storage(cluster)
        preflight = cluster_retirement_storage_preflight(
            cluster,
            mode=ProxmoxCluster.RetirementMode.VERIFIED,
        )
        binding.scope = ClusterStorageMount.Scope.NODE
        binding.node = "pve1"
        binding.save(update_fields=["scope", "node"])

        with transaction.atomic():
            with self.assertRaises(StorageRetirementImpactChanged):
                finalize_cluster_retirement_storage(
                    cluster,
                    mode=ProxmoxCluster.RetirementMode.VERIFIED,
                    expected_digest=preflight.impact_digest,
                )

        definition.refresh_from_db()
        self.assertIsNone(definition.unmanaged_at)
        self.assertTrue(ClusterStorageMount.objects.filter(pk=binding.pk).exists())
        self.assertTrue(StorageCatalogState.objects.filter(cluster=cluster).exists())


class StorageRetirementFinalizerTests(TestCase):
    def test_finalizer_tombstones_identity_and_removes_only_current_projection(self):
        cluster = make_cluster("retiring-storage")
        definition, mount, binding = _published_storage(cluster)
        snapshot = StorageSpaceSnapshot.objects.create(
            cluster=cluster,
            node="pve1",
            api_storage_id=definition.storage_id,
            recorded_at=timezone.now(),
            total_bytes=1000,
            used_bytes=400,
            available_bytes=600,
        )
        other = make_cluster("remaining-storage")
        other_definition, _other_mount, other_binding = _published_storage(other, "other")
        preflight = cluster_retirement_storage_preflight(
            cluster,
            mode=ProxmoxCluster.RetirementMode.VERIFIED,
        )

        with transaction.atomic():
            result = finalize_cluster_retirement_storage(
                cluster,
                mode=ProxmoxCluster.RetirementMode.VERIFIED,
                expected_digest=preflight.impact_digest,
            )

        definition.refresh_from_db()
        self.assertIsNotNone(definition.unmanaged_at)
        self.assertEqual(result.definitions_unmanaged, 1)
        self.assertEqual(result.node_states_deleted, 1)
        self.assertEqual(result.coverages_deleted, 1)
        self.assertEqual(result.observations_deleted, 1)
        self.assertEqual(result.bindings_deleted, 1)
        self.assertEqual(result.catalog_states_deleted, 1)
        self.assertEqual(result.mount_ref_count, 1)
        self.assertFalse(ClusterStorageMount.objects.filter(pk=binding.pk).exists())
        self.assertFalse(StorageCatalogState.objects.filter(cluster=cluster).exists())
        self.assertTrue(StorageSpaceSnapshot.objects.filter(pk=snapshot.pk).exists())

        other_definition.refresh_from_db()
        self.assertIsNone(other_definition.unmanaged_at)
        self.assertTrue(ClusterStorageMount.objects.filter(pk=other_binding.pk).exists())
        self.assertTrue(StorageCatalogState.objects.filter(cluster=other).exists())

    def test_forced_finalizer_releases_only_the_retired_clusters_consumers(self):
        storage, retiring, remaining = shared_storage_with_consumers()
        storage.expected_consumers = ["pve1"]
        storage.save(update_fields=["expected_consumers"])
        retiring_consumer = ProxmoxStorageConsumer.objects.get(storage=storage, cluster=retiring)
        remaining_consumer = ProxmoxStorageConsumer.objects.get(storage=storage, cluster=remaining)
        preflight = cluster_retirement_storage_preflight(
            retiring,
            mode=ProxmoxCluster.RetirementMode.FORCED,
        )

        with transaction.atomic():
            result = finalize_cluster_retirement_storage(
                retiring,
                mode=ProxmoxCluster.RetirementMode.FORCED,
                expected_digest=preflight.impact_digest,
            )

        self.assertEqual(result.consumers_deleted, 1)
        self.assertEqual(
            result.consumer_refs,
            (f"{storage.mount_ref}@nr1:{retiring.key}:pve1",),
        )
        self.assertEqual(result.consumers[0].storage_name, "Shared NFS")
        self.assertFalse(ProxmoxStorageConsumer.objects.filter(pk=retiring_consumer.pk).exists())
        self.assertTrue(ProxmoxStorageConsumer.objects.filter(pk=remaining_consumer.pk).exists())

        gate = _storage_gate_status(
            [storage],
            {remaining.pk: {"pve1"}},
            timezone.now(),
        )[storage.storage_id]
        self.assertTrue(gate["ok"])
        self.assertEqual(gate["missing_node_refs"], [])

    def test_inner_savepoint_rolls_back_a_mid_finalizer_failure(self):
        cluster = make_cluster("storage-fault")
        definition, _mount, binding = _published_storage(cluster)
        preflight = cluster_retirement_storage_preflight(
            cluster,
            mode=ProxmoxCluster.RetirementMode.VERIFIED,
        )
        original_delete = models.QuerySet.delete

        def fail_on_coverage(queryset):
            if queryset.model is ClusterStorageVolumeCoverage:
                raise RuntimeError("injected storage finalizer fault")
            return original_delete(queryset)

        with transaction.atomic():
            with patch.object(models.QuerySet, "delete", new=fail_on_coverage):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    finalize_cluster_retirement_storage(
                        cluster,
                        mode=ProxmoxCluster.RetirementMode.VERIFIED,
                        expected_digest=preflight.impact_digest,
                    )

        definition.refresh_from_db()
        self.assertIsNone(definition.unmanaged_at)
        self.assertTrue(ClusterStorageVolumeObservation.objects.filter(cluster_storage=definition).exists())
        self.assertTrue(ClusterStorageMount.objects.filter(pk=binding.pk).exists())

    def test_unmanaged_definitions_leave_current_datastore_navigation(self):
        cluster = make_cluster("storage-nav")
        definition, _mount, _binding = _published_storage(cluster)
        self.assertTrue(datastore_nav(cluster=cluster, use_cache=False)["shared"])

        definition.unmanaged_at = timezone.now()
        definition.save(update_fields=["unmanaged_at"])

        self.assertEqual(datastore_nav(cluster=cluster, use_cache=False), {"shared": [], "nodes": []})

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.models import (
    ClusterStorage,
    ClusterStorageMount,
    ClusterStorageNodeState,
    ClusterStorageVolumeCoverage,
    ClusterStorageVolumeObservation,
    CurrentGuestInventory,
    FileInventory,
    ProxmoxCluster,
    ScanRun,
    StorageCatalogState,
    StorageMount,
)
from core.services.proxmox import _fetch_live_guest_lineage_uncached
from core.services.refs import (
    ClusterStorageRef,
    MountRef,
    StorageInstanceRef,
    VolumeRef,
)
from core.services.storage_backends import backend_profile
from core.services.storage_catalog import (
    MountedVolumeClassifier,
    StorageCatalogChanged,
    StorageOperationScope,
    UsageState,
    _candidate_nodes,
    _metadata_semantics,
    classify_mounted_volume,
    refresh_storage_metadata,
    refresh_storage_volumes,
    storage_view,
    storage_volumes,
    usage_preflight,
)
from core.services.storage_mounts import (
    StorageMountError,
    bind_storage_mount,
    derived_backend_identity,
    mount_health,
    near_match_mounts,
    normalized_backend_identity,
    resolve_storage_mount,
)


class StorageReferenceTests(SimpleTestCase):
    def test_storage_reference_family_round_trips(self):
        definition = ClusterStorageRef("cluster-a", "local:lvm")
        instance = StorageInstanceRef("cluster-a", "local:lvm", "pve1")
        volume = VolumeRef(instance, "local-lvm:vm-500-disk-0")
        mount = MountRef("3998c440-c0d6-4110-950f-d78a55ee86f1")

        self.assertEqual(ClusterStorageRef.parse(definition.serialize()), definition)
        self.assertEqual(StorageInstanceRef.parse(instance.serialize()), instance)
        self.assertEqual(VolumeRef.parse(volume.serialize()), volume)
        self.assertEqual(MountRef.parse(mount.serialize()), mount)

    def test_backend_identity_normalizes_only_the_server_component(self):
        self.assertEqual(
            normalized_backend_identity("NFS.EXAMPLE:/CaseSensitive/Export"),
            "nfs.example:/CaseSensitive/Export",
        )
        with self.assertRaises(StorageMountError):
            normalized_backend_identity("smb://user:secret@server/share")


class StorageReadModelSourceInvariantTests(SimpleTestCase):
    def test_broad_storage_reads_have_one_owner(self):
        root = Path(__file__).resolve().parent
        offenders: list[str] = []
        allowed = {root / "services" / "storage_catalog.py", root / "views" / "guests" / "mutations.py"}
        for path in root.rglob("*.py"):
            if path in allowed or path.name.startswith("tests") or "migrations" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            if "get(f\"nodes/{quote(node, safe='')}/storage" in source:
                offenders.append(str(path.relative_to(root)))
            if "content?content=" in source and ".get(" in source:
                offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [])

    def test_compose_contract_uses_one_propagated_root(self):
        repository = Path(__file__).resolve().parent.parent
        for name in ("docker-compose.example.yml", "docker-compose.production.yml"):
            source = (repository / name).read_text(encoding="utf-8")
            self.assertIn("target: /storages", source)
            self.assertIn("propagation: rslave", source)
            self.assertIn("propagation: rprivate", source)
            self.assertIn("recursive: readonly", source)
            self.assertIn("storage_accel_state:/storage-accel-state", source)
            self.assertNotIn("TRUENAS_", source)

    def test_catalog_refresh_is_owned_by_the_operation_scope(self):
        """Preflight is an operation-grain contract; no fan-out may refresh per file."""
        root = Path(__file__).resolve().parent
        actions = (root / "services" / "storage_actions.py").read_text(encoding="utf-8")
        self.assertNotIn("fresh=True", actions)
        self.assertIn("scope: StorageOperationScope | None = None", actions)

        views = (root / "views" / "storage.py").read_text(encoding="utf-8")
        for marker in ("scope = StorageOperationScope()", "scope=scope"):
            self.assertIn(marker, views)

    def test_cluster_volume_summary_is_never_an_authorization_input(self):
        root = Path(__file__).resolve().parent
        telemetry_owners = {
            root / "admin.py",
            root / "models.py",
            root / "tasks.py",
            root / "services" / "storage_catalog.py",
        }
        offenders = []
        for path in root.rglob("*.py"):
            if path in telemetry_owners or path.name.startswith("tests") or "migrations" in path.parts:
                continue
            if "volume_complete" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(root)))

        self.assertEqual(
            offenders,
            [],
            "Cluster-level volume_complete is summary telemetry only; authorization must use scoped coverage.",
        )


class FakeStorageClient:
    def __init__(self, responses):
        self.responses = responses

    def get(self, path):
        value = self.responses[path]
        if isinstance(value, Exception):
            raise value
        return value


class StorageCatalogTests(TestCase):
    def setUp(self):
        self.cluster = ProxmoxCluster.objects.create(key="cluster-a", display_name="Cluster A")
        self.responses = {
            "storage": [
                {"storage": "shared", "type": "nfs", "shared": 1, "content": "images,iso"},
                {"storage": "local", "type": "lvmthin", "shared": 0, "content": "images,rootdir"},
            ],
            "nodes": [
                {"node": "pve1", "status": "online"},
                {"node": "pve2", "status": "online"},
                {"node": "pve3", "status": "offline"},
            ],
            "nodes/pve1/storage": [
                {"storage": "shared", "active": 1, "enabled": 1, "total": 100, "used": 10, "avail": 90},
                {"storage": "local", "active": 1, "enabled": 1, "total": 50, "used": 5, "avail": 45},
            ],
            "nodes/pve2/storage": [
                {"storage": "shared", "active": 1, "enabled": 1, "total": 100, "used": 10, "avail": 90},
                {"storage": "local", "active": 1, "enabled": 1, "total": 75, "used": 15, "avail": 60},
            ],
            "nodes/pve1/storage/shared/content": [
                {"volid": "shared:100/vm-100-disk-0.qcow2", "vmid": 100, "content": "images", "size": 10},
            ],
            "nodes/pve2/storage/shared/content": [
                {"volid": "shared:100/vm-100-disk-0.qcow2", "vmid": 100, "content": "images", "size": 10},
            ],
            "nodes/pve1/storage/local/content": [
                {"volid": "local:vm-101-disk-0", "vmid": 101, "content": "images", "size": 20},
            ],
            "nodes/pve2/storage/local/content": [
                {"volid": "local:vm-202-disk-0", "vmid": 202, "content": "images", "size": 30},
            ],
        }
        self.client = FakeStorageClient(self.responses)

    def _metadata(self):
        with patch("core.services.storage_catalog.cluster_clients", return_value=[self.client]):
            return refresh_storage_metadata(self.cluster)

    def _volumes(self):
        with patch("core.services.storage_catalog.cluster_clients", return_value=[self.client]):
            return refresh_storage_volumes(self.cluster)

    def test_complete_generations_keep_shared_and_node_local_semantics(self):
        metadata = self._metadata()
        volumes = self._volumes()

        self.assertTrue(metadata.metadata_complete)
        self.assertTrue(volumes.volume_complete)
        shared = ClusterStorage.objects.get(cluster=self.cluster, storage_id="shared")
        local = ClusterStorage.objects.get(cluster=self.cluster, storage_id="local")
        self.assertEqual(shared.node_states.filter(active=True).count(), 2)
        self.assertFalse(local.shared)
        self.assertEqual(
            set(local.volume_observations.values_list("node", "volid")),
            {
                ("pve1", "local:vm-101-disk-0"),
                ("pve2", "local:vm-202-disk-0"),
            },
        )
        self.assertEqual(len(storage_volumes(storage_view(shared))), 1)
        self.assertEqual(len(storage_volumes(storage_view(local, node="pve2"))), 1)

    def test_steady_state_refresh_writes_no_observation_rows(self):
        """The projection must stop rewriting itself to prove nothing changed."""
        self._metadata()
        self._volumes()
        shared = ClusterStorage.objects.get(cluster=self.cluster, storage_id="shared")
        stamps = dict(shared.volume_observations.values_list("pk", "updated_at"))

        for _ in range(3):
            self._metadata()
            self._volumes()

        self.assertEqual(dict(shared.volume_observations.values_list("pk", "updated_at")), stamps)

    def test_content_list_order_is_not_a_semantic_change(self):
        """Proxmox reorders the content list between responses; that changes nothing."""
        self._metadata()
        self._volumes()
        shared = ClusterStorage.objects.get(cluster=self.cluster, storage_id="shared")
        coverage = shared.volume_coverages.get(node__isnull=True)
        generation = coverage.volume_generation

        self.responses["storage"] = [
            {"storage": "shared", "type": "nfs", "shared": 1, "content": "iso,images"},
            {"storage": "local", "type": "lvmthin", "shared": 0, "content": "rootdir,images"},
        ]
        self._metadata()

        coverage.refresh_from_db()
        self.assertEqual(coverage.volume_generation, generation)
        self.assertTrue(coverage.complete)
        self.assertEqual(ClusterStorage.objects.get(pk=shared.pk).content, ["images", "iso"])

    def test_unchanged_volume_refresh_writes_nothing_and_keeps_the_generation(self):
        """A published generation identifies a set, not a refresh attempt."""
        self._metadata()
        self._volumes()
        shared = ClusterStorage.objects.get(cluster=self.cluster, storage_id="shared")
        coverage = shared.volume_coverages.get(node__isnull=True)
        generation = coverage.volume_generation
        pks = set(shared.volume_observations.values_list("pk", flat=True))

        self._volumes()

        coverage.refresh_from_db()
        self.assertEqual(coverage.volume_generation, generation)
        # Same rows, not re-created: nothing observable changed.
        self.assertEqual(set(shared.volume_observations.values_list("pk", flat=True)), pks)

    def test_shared_storage_publishes_one_logical_set_with_recorded_agreement(self):
        self._metadata()
        self._volumes()
        shared = ClusterStorage.objects.get(cluster=self.cluster, storage_id="shared")

        rows = list(shared.volume_observations.values_list("node", "volid"))

        # Two nodes answered identically; the agreement is the proof, so it is
        # recorded once rather than duplicated per node.
        self.assertEqual(rows, [("", "shared:100/vm-100-disk-0.qcow2")])
        coverage = shared.volume_coverages.get(node__isnull=True)
        self.assertEqual(coverage.agreeing_nodes, ["pve1", "pve2"])
        self.assertEqual(len(storage_volumes(storage_view(shared))), 1)

    def test_size_drift_alone_does_not_republish_the_generation(self):
        """A thin volume's used size changes constantly and changes no membership fact."""
        self._metadata()
        self._volumes()
        shared = ClusterStorage.objects.get(cluster=self.cluster, storage_id="shared")
        generation = shared.volume_coverages.get(node__isnull=True).volume_generation

        for node in ("pve1", "pve2"):
            self.responses[f"nodes/{node}/storage/shared/content"] = [
                {"volid": "shared:100/vm-100-disk-0.qcow2", "vmid": 100, "content": "images", "size": 99},
            ]
        self._volumes()

        coverage = shared.volume_coverages.get(node__isnull=True)
        row = shared.volume_observations.get()
        self.assertEqual(coverage.volume_generation, generation)
        self.assertEqual(row.size_bytes, 99)
        self.assertEqual(row.observed_volume_generation, generation)

    def test_changed_volume_set_is_diffed_onto_a_new_generation(self):
        self._metadata()
        self._volumes()
        shared = ClusterStorage.objects.get(cluster=self.cluster, storage_id="shared")
        first_generation = shared.volume_coverages.get(node__isnull=True).volume_generation
        kept = shared.volume_observations.get(volid="shared:100/vm-100-disk-0.qcow2")

        added = {"volid": "shared:101/vm-101-disk-0.qcow2", "vmid": 101, "content": "images", "size": 20}
        for node in ("pve1", "pve2"):
            self.responses[f"nodes/{node}/storage/shared/content"] = [
                {"volid": "shared:100/vm-100-disk-0.qcow2", "vmid": 100, "content": "images", "size": 11},
                added,
            ]
        self._volumes()

        coverage = shared.volume_coverages.get(node__isnull=True)
        self.assertNotEqual(coverage.volume_generation, first_generation)
        # The surviving row is updated in place and re-bound to the new
        # generation rather than deleted and re-inserted.
        kept.refresh_from_db()
        self.assertEqual(kept.size_bytes, 11)
        self.assertEqual(kept.observed_volume_generation, coverage.volume_generation)
        self.assertEqual(
            set(shared.volume_observations.values_list("volid", flat=True)),
            {"shared:100/vm-100-disk-0.qcow2", "shared:101/vm-101-disk-0.qcow2"},
        )
        self.assertEqual(len(storage_volumes(storage_view(shared))), 2)

    def test_unchanged_metadata_refresh_preserves_volume_coverage(self):
        self._metadata()
        self._volumes()
        shared = ClusterStorage.objects.get(cluster=self.cluster, storage_id="shared")
        coverage = shared.volume_coverages.get(node__isnull=True)
        volume_generation = coverage.volume_generation

        refreshed = self._metadata()
        coverage.refresh_from_db()

        self.assertTrue(refreshed.volume_complete)
        self.assertEqual(coverage.volume_generation, volume_generation)
        # The coverage row carries the binding to the metadata generation the
        # published set is still valid under; that is what every reader checks,
        # and re-stamping each observation as well would rewrite the whole table
        # every cycle for a value nothing reads.
        self.assertEqual(coverage.based_on_metadata_generation, refreshed.metadata_generation)
        self.assertTrue(storage_view(shared).coverage_complete)

    def test_changed_metadata_invalidates_volume_coverage(self):
        self._metadata()
        self._volumes()
        self.responses["storage"][0]["content"] = "images,iso,backup"

        refreshed = self._metadata()

        self.assertFalse(refreshed.volume_complete)
        shared = ClusterStorage.objects.get(cluster=self.cluster, storage_id="shared")
        local = ClusterStorage.objects.get(cluster=self.cluster, storage_id="local")
        self.assertFalse(storage_view(shared).coverage_complete)
        self.assertTrue(storage_view(local, node="pve1").coverage_complete)

    def test_failed_refresh_keeps_last_complete_projection(self):
        first = self._metadata()
        generation = first.metadata_generation
        self.responses["nodes/pve2/storage"] = RuntimeError("boom")

        failed = self._metadata()

        self.assertFalse(failed.metadata_complete)
        self.assertEqual(failed.metadata_generation, generation)
        self.assertEqual(ClusterStorage.objects.filter(cluster=self.cluster, present=True).count(), 2)

    def test_failed_volume_refresh_keeps_last_complete_observations(self):
        self._metadata()
        self._volumes()
        shared = ClusterStorage.objects.get(cluster=self.cluster, storage_id="shared")
        generation = shared.volume_coverages.get(node__isnull=True).volume_generation
        observation_count = ClusterStorageVolumeObservation.objects.filter(
            cluster_storage__cluster=self.cluster
        ).count()
        self.responses["nodes/pve2/storage/shared/content"] = []

        failed = self._volumes()

        self.assertFalse(failed.volume_complete)
        shared_coverage = shared.volume_coverages.get(node__isnull=True)
        self.assertFalse(shared_coverage.complete)
        self.assertEqual(shared_coverage.volume_generation, generation)
        stale_view = storage_view(shared)
        self.assertFalse(stale_view.coverage_complete)
        self.assertTrue(stale_view.volumes_stale)
        self.assertEqual(len(storage_volumes(stale_view)), 1)
        self.assertEqual(
            ClusterStorageVolumeObservation.objects.filter(cluster_storage__cluster=self.cluster).count(),
            observation_count,
        )

    def test_shared_failure_does_not_poison_node_local_coverage(self):
        self._metadata()
        self._volumes()
        self.responses["nodes/pve2/storage/shared/content"] = []

        state = self._volumes()
        shared = ClusterStorage.objects.get(cluster=self.cluster, storage_id="shared")
        local = ClusterStorage.objects.get(cluster=self.cluster, storage_id="local")

        self.assertFalse(state.volume_complete)
        self.assertFalse(storage_view(shared).coverage_complete)
        self.assertTrue(storage_view(local, node="pve1").coverage_complete)
        self.assertEqual(
            [row.volid for row in storage_volumes(storage_view(local, node="pve1"))], ["local:vm-101-disk-0"]
        )

    def test_node_local_failure_is_isolated_to_that_node(self):
        self._metadata()
        self._volumes()
        self.responses["nodes/pve2/storage/local/content"] = RuntimeError("down")

        state = self._volumes()
        local = ClusterStorage.objects.get(cluster=self.cluster, storage_id="local")

        self.assertFalse(state.volume_complete)
        self.assertTrue(storage_view(local, node="pve1").coverage_complete)
        self.assertFalse(storage_view(local, node="pve2").coverage_complete)
        self.assertEqual(
            [row.volid for row in storage_volumes(storage_view(local, node="pve1"))], ["local:vm-101-disk-0"]
        )

    def test_lineage_uses_healthy_scopes_when_cluster_summary_is_partial(self):
        self.responses["nodes/pve1/storage/local/content"][0]["parent"] = "local:100/base-100-disk-0.qcow2"
        self._metadata()
        self._volumes()
        self.responses["nodes/pve2/storage/shared/content"] = []

        state = self._volumes()
        lineage = _fetch_live_guest_lineage_uncached(cluster=self.cluster)

        self.assertFalse(state.volume_complete)
        self.assertEqual(lineage, {101: 100})

    def test_complete_metadata_omission_retires_definition(self):
        self._metadata()
        self.responses["storage"] = [row for row in self.responses["storage"] if row["storage"] != "local"]
        for node in ("pve1", "pve2"):
            key = f"nodes/{node}/storage"
            self.responses[key] = [row for row in self.responses[key] if row["storage"] != "local"]

        state = self._metadata()
        local = ClusterStorage.objects.get(cluster=self.cluster, storage_id="local")

        self.assertTrue(state.metadata_complete)
        self.assertFalse(local.present)
        self.assertIsNotNone(local.retired_at)

    def test_unknown_plugin_does_not_poison_supported_storage_refresh(self):
        self.responses["storage"].append({"storage": "vendor-store", "type": "vendor-plugin", "shared": 1})
        for node in ("pve1", "pve2"):
            self.responses[f"nodes/{node}/storage"].append({"storage": "vendor-store", "active": 1, "enabled": 1})

        self._metadata()
        state = self._volumes()
        unknown = ClusterStorage.objects.get(cluster=self.cluster, storage_id="vendor-store")

        self.assertTrue(state.volume_complete)
        self.assertFalse(storage_view(unknown).capabilities.can_list_volumes)
        self.assertIn("Unsupported storage type", storage_view(unknown).coverage_reason)
        self.assertTrue(
            ClusterStorageVolumeObservation.objects.filter(
                cluster_storage__cluster=self.cluster,
                cluster_storage__storage_id="shared",
            ).exists()
        )

    def test_shared_disagreement_never_publishes_absence(self):
        self._metadata()
        self.responses["nodes/pve2/storage/shared/content"] = []

        state = self._volumes()

        self.assertFalse(state.volume_complete)
        self.assertIn("shared", state.volume_errors)
        shared = ClusterStorage.objects.get(cluster=self.cluster, storage_id="shared")
        self.assertFalse(storage_view(shared).coverage_complete)

    def test_same_storage_id_is_cluster_qualified(self):
        self._metadata()
        cluster_b = ProxmoxCluster.objects.create(key="cluster-b", display_name="Cluster B")
        client_b = FakeStorageClient(
            {
                "storage": [{"storage": "local", "type": "dir", "shared": 0}],
                "nodes": [{"node": "pve9", "status": "online"}],
                "nodes/pve9/storage": [{"storage": "local", "active": 1, "enabled": 1}],
            }
        )
        with patch("core.services.storage_catalog.cluster_clients", return_value=[client_b]):
            refresh_storage_metadata(cluster_b)

        self.assertEqual(ClusterStorage.objects.filter(storage_id="local").count(), 2)

    def test_binding_service_enforces_definition_scope(self):
        self._metadata()
        mount = StorageMount.objects.create(
            storage_id="hint", display_name="Shared mount", path="/storages/shared", relative_path="shared"
        )
        shared = ClusterStorage.objects.get(cluster=self.cluster, storage_id="shared")
        local = ClusterStorage.objects.get(cluster=self.cluster, storage_id="local")

        self.assertEqual(
            bind_storage_mount(cluster_storage=shared, mount=mount).scope, ClusterStorageMount.Scope.SHARED
        )
        with self.assertRaises(StorageMountError):
            bind_storage_mount(cluster_storage=shared, mount=mount, node="pve1")
        with self.assertRaises(StorageMountError):
            bind_storage_mount(cluster_storage=local, mount=mount)
        self.assertEqual(bind_storage_mount(cluster_storage=local, mount=mount, node="pve1").node, "pve1")

    def test_usage_preflight_is_tri_state_and_generation_bound(self):
        self._metadata()
        self._volumes()
        shared = ClusterStorage.objects.get(cluster=self.cluster, storage_id="shared")
        local = ClusterStorage.objects.get(cluster=self.cluster, storage_id="local")
        CurrentGuestInventory.objects.create(
            cluster=self.cluster,
            node="pve1",
            object_type="vm",
            vmid=100,
            disk_references=["shared:100/vm-100-disk-0.qcow2"],
            observed_at=timezone.now(),
        )

        referenced = usage_preflight(shared, fresh=False)
        node_local = usage_preflight(local, node="", fresh=False)

        self.assertEqual(referenced.state, UsageState.REFERENCED)
        self.assertTrue(referenced.token)
        # A node-local block storage has no host mount to register, ever; a node
        # belongs to one cluster, so its identity is settled, not unknown.
        self.assertEqual(node_local.state, UsageState.UNREFERENCED)

    def test_unmounted_file_tree_storage_stays_unknown(self):
        """The fail-closed rule that still applies: a browsable backend needs its mount."""
        self._metadata()
        self._volumes()
        definition = ClusterStorage.objects.create(
            cluster=self.cluster,
            storage_id="dir-store",
            storage_type="dir",
            shared=False,
            present=True,
            config={"path": "/var/lib/vz"},
        )
        ClusterStorageNodeState.objects.create(
            cluster_storage=definition,
            node="pve1",
            present=True,
            active=True,
            enabled=True,
            observed_metadata_generation=definition.observed_metadata_generation,
        )

        result = usage_preflight(definition, node="pve1", fresh=False)

        self.assertEqual(result.state, UsageState.UNKNOWN)

    def test_shared_block_storage_uses_its_own_definition_for_identity(self):
        """Module 5's storage deletes need a preflight that can say yes for rbd/iscsi."""
        self._metadata()
        self._volumes()
        config = {"monhost": "ceph1.hq.local,ceph2.hq.local", "pool": "vmpool"}
        rbd_a = self._published_block_storage(self.cluster, "rbd-a", config)

        cluster_b = ProxmoxCluster.objects.create(key="cluster-b", display_name="Cluster B")
        rbd_b = self._published_block_storage(
            cluster_b, "rbd-b", {**config, "monhost": "ceph2.hq.local,ceph1.hq.local"}
        )

        clean = usage_preflight(rbd_a, volid="rbd-a:vm-100-disk-0", fresh=False)
        self.assertEqual(clean.state, UsageState.UNREFERENCED)

        CurrentGuestInventory.objects.create(
            cluster=cluster_b,
            node="pve9",
            object_type="vm",
            vmid=100,
            disk_references=[f"{rbd_b.storage_id}:vm-100-disk-0"],
            observed_at=timezone.now(),
        )

        elsewhere = usage_preflight(rbd_a, volid="rbd-a:vm-100-disk-0", fresh=False)
        self.assertEqual(elsewhere.state, UsageState.REFERENCED_ELSEWHERE)
        self.assertEqual(elsewhere.references, ("cluster-b:vm:100",))

    def test_shared_block_storage_without_a_publishable_identity_stays_unknown(self):
        self._metadata()
        self._volumes()
        definition = self._published_block_storage(self.cluster, "iscsi-a", {"target": "iqn.2026-01.local:lun0"})

        result = usage_preflight(definition, fresh=False)

        self.assertEqual(result.state, UsageState.UNKNOWN)
        self.assertIn("does not publish", result.reason)

    def _published_block_storage(self, cluster, storage_id, config, storage_type=""):
        """A shared block storage with complete coverage and no volumes of its own."""
        state = StorageCatalogState.objects.filter(cluster=cluster).first()
        if state is None:
            state = StorageCatalogState.objects.create(
                cluster=cluster,
                metadata_generation=uuid.uuid4(),
                metadata_complete=True,
                metadata_refreshed_at=timezone.now(),
            )
        definition = ClusterStorage.objects.create(
            cluster=cluster,
            storage_id=storage_id,
            storage_type=storage_type or ("iscsi" if "target" in config and "monhost" not in config else "rbd"),
            shared=True,
            present=True,
            config=config,
            observed_metadata_generation=state.metadata_generation,
        )
        ClusterStorageNodeState.objects.create(
            cluster_storage=definition,
            node="pve1" if cluster == self.cluster else "pve9",
            present=True,
            active=True,
            enabled=True,
            observed_metadata_generation=state.metadata_generation,
        )
        ClusterStorageVolumeCoverage.objects.create(
            cluster_storage=definition,
            node=None,
            scope=ClusterStorageVolumeCoverage.Scope.SHARED,
            volume_generation=uuid.uuid4(),
            based_on_metadata_generation=state.metadata_generation,
            complete=True,
            refreshed_at=timezone.now(),
        )
        return definition

    def test_operation_scope_refreshes_once_and_pins_the_generation(self):
        self._metadata()
        self._volumes()
        shared = ClusterStorage.objects.get(cluster=self.cluster, storage_id="shared")
        scope = StorageOperationScope()

        with patch("core.services.storage_catalog.refresh_storage_catalog") as refresh:
            for index in range(12):
                scope.preflight(shared, volid=f"shared:100/vm-100-disk-{index}.qcow2")

        self.assertEqual(refresh.call_count, 1)

    def test_operation_scope_refuses_to_mix_republished_generations(self):
        self._metadata()
        self._volumes()
        shared = ClusterStorage.objects.get(cluster=self.cluster, storage_id="shared")
        scope = StorageOperationScope()

        with patch("core.services.storage_catalog.refresh_storage_catalog"):
            scope.preflight(shared)
            coverage = shared.volume_coverages.get(node__isnull=True)
            coverage.volume_generation = uuid.uuid4()
            coverage.save()
            with self.assertRaises(StorageCatalogChanged):
                scope.preflight(shared)

    def test_usage_preflight_detects_same_verified_mount_across_clusters(self):
        self._metadata()
        self._volumes()
        shared = ClusterStorage.objects.get(cluster=self.cluster, storage_id="shared")
        mount = StorageMount.objects.create(
            storage_id="shared-hint",
            display_name="Shared backend",
            path="/storages/shared",
            relative_path="shared",
            backend_identity="nfs:server:/export",
        )
        bind_storage_mount(cluster_storage=shared, mount=mount)

        cluster_b = ProxmoxCluster.objects.create(key="cluster-b", display_name="Cluster B")
        client_b = FakeStorageClient(
            {
                "storage": [{"storage": "shared-b", "type": "nfs", "shared": 1, "content": "images"}],
                "nodes": [{"node": "pve9", "status": "online"}],
                "nodes/pve9/storage": [{"storage": "shared-b", "active": 1, "enabled": 1}],
                "nodes/pve9/storage/shared-b/content": [
                    {
                        "volid": "shared-b:100/vm-100-disk-0.qcow2",
                        "vmid": 100,
                        "content": "images",
                    }
                ],
            }
        )
        with patch("core.services.storage_catalog.cluster_clients", return_value=[client_b]):
            refresh_storage_metadata(cluster_b)
            refresh_storage_volumes(cluster_b)
        shared_b = ClusterStorage.objects.get(cluster=cluster_b, storage_id="shared-b")
        mount_b = StorageMount.objects.create(
            storage_id="shared-b-hint",
            display_name="Same backend, second mount",
            path="/storages/shared-b",
            relative_path="shared-b",
            backend_identity="nfs:server:/export",
        )
        bind_storage_mount(cluster_storage=shared_b, mount=mount_b)
        CurrentGuestInventory.objects.create(
            cluster=cluster_b,
            node="pve9",
            object_type="vm",
            vmid=100,
            disk_references=["shared-b:100/vm-100-disk-0.qcow2"],
            observed_at=timezone.now(),
        )

        result = usage_preflight(shared, volid="shared:100/vm-100-disk-0.qcow2", fresh=False)

        self.assertEqual(result.state, UsageState.REFERENCED_ELSEWHERE)
        self.assertEqual(result.references, ("cluster-b:vm:100",))

        ClusterStorageVolumeCoverage.objects.filter(cluster_storage=shared_b).update(
            complete=False,
            error_code="test_incomplete",
            error_reason="Coverage is incomplete.",
        )
        incomplete = usage_preflight(shared, volid="shared:100/vm-100-disk-0.qcow2", fresh=False)
        self.assertEqual(incomplete.state, UsageState.UNKNOWN)

    def test_classifying_many_volumes_of_one_mount_costs_one_setup(self):
        """A scan classifies every disk it finds; the setup must not repeat.

        The per-file cost used to include a storage view, the catalog state and a
        full pass over the cluster's guests — identical work, once per disk, so a
        larger datastore paid quadratically. Everything except the volume id is
        resolved when the classifier is built, which makes classify() a pure
        lookup: zero queries, no matter how many disks follow.
        """
        self._metadata()
        self._volumes()
        shared = ClusterStorage.objects.get(cluster=self.cluster, storage_id="shared")
        mount = StorageMount.objects.create(
            storage_id="shared-hint",
            display_name="Shared backend",
            path="/storages/shared",
            relative_path="shared",
            backend_identity="nfs:server:/export",
        )
        bind_storage_mount(cluster_storage=shared, mount=mount)
        CurrentGuestInventory.objects.create(
            cluster=self.cluster,
            node="pve1",
            object_type="vm",
            vmid=100,
            disk_references=["shared:100/vm-100-disk-0.qcow2"],
            observed_at=timezone.now(),
        )

        classifier = MountedVolumeClassifier(mount)
        with self.assertNumQueries(0):
            referenced = classifier.classify("images/100/vm-100-disk-0.qcow2")
            for index in range(20):
                classifier.classify(f"images/900/vm-900-disk-{index}.qcow2")

        self.assertEqual(referenced.classification, FileInventory.Classification.REFERENCED)

    def test_metadata_semantics_costs_two_queries_and_agrees_with_the_database(self):
        """The snapshot that decides whether volume coverage survives a refresh.

        It runs twice per metadata refresh — before and after the update loop —
        so a per-definition query here is paid 2N times every cycle to prove that
        nothing changed. It must also agree exactly with the database: a tuple
        that reorders makes every storage look changed, which republishes the
        catalog and discards every absence proof with it.
        """
        self._metadata()

        with self.assertNumQueries(2):
            semantics = _metadata_semantics(self.cluster)

        definitions = list(ClusterStorage.objects.filter(cluster=self.cluster))
        self.assertEqual(set(semantics), {definition.storage_id for definition in definitions})
        for definition in definitions:
            expected = tuple(
                (state.node, state.present, state.active, state.enabled)
                for state in ClusterStorageNodeState.objects.filter(cluster_storage=definition).order_by("node")
            )
            self.assertEqual(semantics[definition.storage_id][-1], expected)

    def test_candidate_nodes_reuses_a_prefetched_definition(self):
        """The volume refresh asks this once per storage.

        That lane already pays a Proxmox round trip per storage; it must not also
        pay a database round trip for node states it has already loaded.
        """
        self._metadata()
        definitions = list(
            ClusterStorage.objects.filter(cluster=self.cluster, present=True).prefetch_related("node_states")
        )
        self.assertGreater(len(definitions), 1)

        with self.assertNumQueries(0):
            candidates = [_candidate_nodes(definition) for definition in definitions]

        for definition, nodes in zip(definitions, candidates, strict=True):
            expected = list(
                ClusterStorageNodeState.objects.filter(
                    cluster_storage=definition, present=True, active=True, enabled=True
                )
                .order_by("node")
                .values_list("node", flat=True)
            )
            self.assertEqual(nodes, expected)
            self.assertTrue(nodes)

    def test_a_prefetched_listing_resolves_every_storage_view_for_free(self):
        """Listing N datastores must not cost N times the per-storage reads.

        `storage_view` used to re-fetch every relation with `.filter()` or
        `.order_by()`, and `scope_conflict` with `values_list`. Each of those
        builds a new queryset and silently bypasses the prefetch cache, so the
        page paid for the prefetch *and* for four-plus queries per definition.
        """
        self._metadata()
        self._volumes()
        definitions = list(
            ClusterStorage.objects.filter(cluster=self.cluster, present=True)
            .select_related("cluster__storage_catalog_state")
            .prefetch_related("node_states", "mount_bindings__mount", "volume_coverages")
        )
        self.assertGreater(len(definitions), 1)

        with self.assertNumQueries(0):
            for definition in definitions:
                storage_view(definition, node="pve1")

    def test_a_storage_view_never_reads_the_observation_table(self):
        """The expensive half is opt-in.

        Volume observations scale with the size of a datastore, and the callers
        that only ask what a storage *can do* — the listing, every usage
        preflight — never read one. Only `storage_volumes` may touch the table.
        """
        self._metadata()
        self._volumes()
        definition = ClusterStorage.objects.get(cluster=self.cluster, storage_id="shared")
        table = ClusterStorageVolumeObservation._meta.db_table

        with CaptureQueriesContext(connection) as captured:
            view = storage_view(definition)
        self.assertNotIn(table, " ".join(entry["sql"] for entry in captured))

        with CaptureQueriesContext(connection) as captured:
            volumes = storage_volumes(view)
        self.assertTrue(volumes)
        self.assertIn(table, " ".join(entry["sql"] for entry in captured))

    def test_classifier_and_single_volume_helper_agree(self):
        # The one-shot helper must stay a thin wrapper: two implementations of a
        # gate that authorizes destructive file actions would drift.
        self._metadata()
        self._volumes()
        shared = ClusterStorage.objects.get(cluster=self.cluster, storage_id="shared")
        mount = StorageMount.objects.create(
            storage_id="shared-hint",
            display_name="Shared backend",
            path="/storages/shared",
            relative_path="shared",
            backend_identity="nfs:server:/export",
        )
        bind_storage_mount(cluster_storage=shared, mount=mount)

        classifier = MountedVolumeClassifier(mount)
        for relative_path in ("images/100/vm-100-disk-0.qcow2", "images/900/vm-900-disk-0.qcow2"):
            with self.subTest(relative_path=relative_path):
                one_shot = classify_mounted_volume(mount, relative_path)
                reused = classifier.classify(relative_path)
                self.assertEqual(one_shot.classification, reused.classification)
                self.assertEqual(one_shot.reason, reused.reason)
                self.assertEqual(one_shot.evidence, reused.evidence)

    def test_unbound_mount_has_nothing_to_classify(self):
        mount = StorageMount.objects.create(
            storage_id="orphan-hint",
            display_name="Unbound",
            path="/storages/orphan",
            relative_path="orphan",
        )

        classifier = MountedVolumeClassifier(mount)

        self.assertIsNone(classifier.classify("images/100/vm-100-disk-0.qcow2"))

    def test_usage_preflight_refuses_unverified_mount_identity(self):
        self._metadata()
        self._volumes()
        shared = ClusterStorage.objects.get(cluster=self.cluster, storage_id="shared")
        mount = StorageMount.objects.create(
            storage_id="legacy-hint",
            display_name="Unverified legacy mount",
            path="/storages/legacy",
            relative_path="legacy",
        )
        bind_storage_mount(cluster_storage=shared, mount=mount)

        result = usage_preflight(shared, fresh=False)

        self.assertEqual(result.state, UsageState.UNKNOWN)
        self.assertIn("no verified backend identity", result.reason)


class BackendIdentityAssistanceTests(TestCase):
    """The identity that decides cross-cluster deletion safety must not depend on typing."""

    def setUp(self):
        self.cluster = ProxmoxCluster.objects.create(key="cluster-a", display_name="Cluster A")

    def _definition(self, storage_type, config, storage_id="nas"):
        return ClusterStorage.objects.create(
            cluster=self.cluster,
            storage_id=storage_id,
            storage_type=storage_type,
            shared=True,
            present=True,
            config=config,
        )

    def test_identity_is_derived_from_the_proxmox_definition(self):
        nfs = self._definition("nfs", {"server": "NAS.hq.local", "export": "/mnt/tank/vm"})
        cifs = self._definition("cifs", {"server": "nas", "share": "vm"}, storage_id="smb")

        self.assertEqual(derived_backend_identity(nfs), "nas.hq.local:/mnt/tank/vm")
        self.assertEqual(derived_backend_identity(cifs), "//nas/vm")

    def test_identity_is_not_invented_for_backends_that_do_not_publish_one(self):
        local_dir = self._definition("dir", {"path": "/var/lib/vz"}, storage_id="local")
        incomplete = self._definition("nfs", {"server": "nas"}, storage_id="broken")

        self.assertEqual(derived_backend_identity(local_dir), "")
        self.assertEqual(derived_backend_identity(incomplete), "")

    def test_same_export_under_a_different_host_spelling_is_a_near_match(self):
        StorageMount.objects.create(
            storage_id="mount-a",
            display_name="NAS via short name",
            path="/storages/nas",
            relative_path="nas",
            backend_identity="nas:/mnt/tank/vm",
        )

        matches = near_match_mounts("nas.hq.local:/mnt/tank/vm")
        self.assertEqual([mount.display_name for mount in matches], ["NAS via short name"])
        self.assertEqual(near_match_mounts("nas:/mnt/tank/vm"), [])
        self.assertEqual(near_match_mounts("nas.hq.local:/mnt/tank/iso"), [])


class StorageMountIdentityTests(TestCase):
    def test_duplicate_legacy_ids_require_immutable_mount_reference(self):
        first = StorageMount.objects.create(
            storage_id="shared",
            display_name="First",
            path="/storages/first",
            relative_path="first",
            backend_identity="nfs:first:/export",
        )
        second = StorageMount.objects.create(
            storage_id="shared",
            display_name="Second",
            path="/storages/second",
            relative_path="second",
            backend_identity="nfs:second:/export",
        )

        self.assertEqual(resolve_storage_mount(first.mount_ref), first)
        self.assertEqual(resolve_storage_mount(str(second.mount_key)), second)
        with self.assertRaises(StorageMount.DoesNotExist):
            resolve_storage_mount("shared")


class StorageMountHealthTests(TestCase):
    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_network_backend_rejects_unmounted_backing_directory(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "shared").mkdir()
            with override_settings(PVE_HELPER_STORAGE_CONTAINER_ROOT=Path(root)):
                mount = StorageMount.objects.create(
                    storage_id="hint", display_name="NFS", path=f"{root}/shared", relative_path="shared"
                )
                with patch("core.services.storage_mounts.mountinfo_entries", return_value=()):
                    health = mount_health(mount, backend_profile("nfs"))

        self.assertFalse(health.available)
        self.assertIn("backing directory", health.reason)

    @override_settings(STORAGE_WRITE_ENABLED=False)
    def test_directory_backend_can_browse_while_writes_are_disabled(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "local").mkdir()
            with override_settings(PVE_HELPER_STORAGE_CONTAINER_ROOT=Path(root)):
                mount = StorageMount.objects.create(
                    storage_id="hint", display_name="Directory", path=f"{root}/local", relative_path="local"
                )
                health = mount_health(mount, backend_profile("dir"))

        self.assertTrue(health.available)
        self.assertFalse(health.writable)
        self.assertEqual(health.reason, "Storage writes are disabled.")


class StorageCatalogFailureLoggingTests(TestCase):
    """A swallowed exception must still reach the log with its traceback.

    Every failure path here converts the exception into a curated public string
    so the operator is never shown a raw provider message. That is deliberate —
    but for a long time it also meant a `TypeError` from a bug in the parsing
    code presented as a permanently incomplete catalog with nothing whatsoever
    in the container logs, which is how the `content` ordering defect stayed
    hidden. Keep the public message generic; keep the traceback.
    """

    logger_name = "core.services.storage_catalog"

    def setUp(self):
        self.cluster = ProxmoxCluster.objects.create(key="cluster-log", display_name="Cluster Log")
        self.responses = {
            "storage": [{"storage": "shared", "type": "nfs", "shared": 1, "content": "images"}],
            "nodes": [{"node": "pve1", "status": "online"}],
            "nodes/pve1/storage": [
                {"storage": "shared", "active": 1, "enabled": 1, "total": 100, "used": 10, "avail": 90}
            ],
            "nodes/pve1/storage/shared/content": [
                {"volid": "shared:100/vm-100-disk-0.qcow2", "vmid": 100, "content": "images", "size": 10},
            ],
        }
        self.client = FakeStorageClient(self.responses)

    def _metadata(self):
        with patch("core.services.storage_catalog.cluster_clients", return_value=[self.client]):
            return refresh_storage_metadata(self.cluster)

    def _volumes(self):
        with patch("core.services.storage_catalog.cluster_clients", return_value=[self.client]):
            return refresh_storage_volumes(self.cluster)

    def test_node_inventory_failure_is_logged_with_its_traceback(self):
        # A TypeError stands in for a bug in our own parsing code, which the
        # bare `except Exception` cannot distinguish from an unreachable node.
        self.responses["nodes/pve1/storage"] = TypeError("bug in the parser")

        with self.assertLogs(self.logger_name, level="WARNING") as captured:
            state = self._metadata()

        self.assertFalse(state.metadata_complete)
        node_records = [record for record in captured.records if "Node storage inventory failed" in record.getMessage()]
        self.assertEqual(len(node_records), 1)
        self.assertIn("cluster=cluster-log", node_records[0].getMessage())
        self.assertIn("node=pve1", node_records[0].getMessage())
        self.assertIsNotNone(node_records[0].exc_info, "the traceback must not be discarded")
        # The operator still sees only the curated message.
        self.assertNotIn("bug in the parser", str(state.metadata_errors))

    def test_volume_lane_failure_is_logged_with_its_traceback(self):
        self._metadata()
        self.responses["nodes/pve1/storage/shared/content"] = TypeError("bug in the volume parser")

        with self.assertLogs(self.logger_name, level="WARNING") as captured:
            state = self._volumes()

        self.assertFalse(state.volume_complete)
        volume_records = [
            record for record in captured.records if "Shared volume listing failed" in record.getMessage()
        ]
        self.assertEqual(len(volume_records), 1)
        self.assertIn("storage=shared", volume_records[0].getMessage())
        self.assertIsNotNone(volume_records[0].exc_info, "the traceback must not be discarded")
        self.assertNotIn("bug in the volume parser", str(state.volume_errors))


class ApiStorageContextTests(TestCase):
    """The catalog-derived half of the per-node storage page is optional.

    It used to be smuggled through `locals()` introspection, which hid the fact
    that both values are simply absent when the storage is not in the catalog.
    """

    def setUp(self):
        self.cluster = ProxmoxCluster.objects.create(key="cluster-ctx", display_name="Cluster Ctx")

    def test_absent_storage_yields_no_catalog_context(self):
        from core.views.storage import _api_storage_context, _resolve_datastore_scope

        definition, node, _moved = _resolve_datastore_scope(self.cluster, "missing", "pve1")
        context = _api_storage_context(self.cluster, definition, "missing", node, "summary")

        self.assertFalse(context["found"])
        self.assertIsNone(context["catalog_view"])
        self.assertFalse(context["storage_shared"])

    def test_catalogued_shared_storage_reports_itself_as_shared(self):
        from core.views.storage import _api_storage_context, _resolve_datastore_scope

        definition = ClusterStorage.objects.create(
            cluster=self.cluster,
            storage_id="shared",
            storage_type="nfs",
            shared=True,
            present=True,
            content=["images"],
            config={"storage": "shared", "type": "nfs"},
        )
        ClusterStorageNodeState.objects.create(
            cluster_storage=definition,
            node="pve1",
            active=True,
            enabled=True,
            total_bytes=100,
            used_bytes=10,
            available_bytes=90,
        )

        # A shared datastore resolves to the cluster-wide scope, so the node the
        # caller supplied is dropped rather than baked into the page.
        definition, node, moved = _resolve_datastore_scope(self.cluster, "shared", "pve1")
        self.assertTrue(moved)
        self.assertEqual(node, "")
        context = _api_storage_context(self.cluster, definition, "shared", node, "summary")

        self.assertTrue(context["found"])
        self.assertIsNotNone(context["catalog_view"])
        self.assertTrue(context["storage_shared"])


class DatastoreScopeUrlTests(TestCase):
    """A datastore page is keyed on the scope its volumes are published under.

    A shared datastore is one cluster-wide object however many nodes see it; a
    node-local one is a different disk on each node, and Proxmox gives them all
    the same name. Collapsing the latter into one page would show one node's
    capacity under another node's disk, which is precisely what the per-node
    volume coverage scope exists to keep apart.
    """

    def setUp(self):
        self.cluster = ProxmoxCluster.objects.create(key="scope", display_name="Scope", enabled=True)

    def _definition(self, storage_id, *, shared, nodes):
        definition = ClusterStorage.objects.create(
            cluster=self.cluster,
            storage_id=storage_id,
            storage_type="nfs" if shared else "dir",
            shared=shared,
            present=True,
            content=["images"],
            config={"storage": storage_id},
        )
        for node in nodes:
            ClusterStorageNodeState.objects.create(
                cluster_storage=definition, node=node, present=True, active=True, enabled=True
            )
        return definition

    def test_a_shared_datastore_answers_without_a_node(self):
        self._definition("TrueNAS-VM", shared=True, nodes=["pve1", "pve2"])

        response = self.client.get("/clusters/scope/datastores/TrueNAS-VM/summary/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Shared datastore in Scope")

    def test_a_shared_datastore_addressed_through_a_node_moves_to_its_cluster_url(self):
        """Otherwise the same datastore has one page per node, and each of them
        claims a completeness that is only ever proven cluster-wide."""
        self._definition("TrueNAS-VM", shared=True, nodes=["pve1", "pve2"])

        response = self.client.get("/clusters/scope/nodes/pve2/datastores/TrueNAS-VM/summary/")

        self.assertRedirects(
            response,
            "/clusters/scope/datastores/TrueNAS-VM/summary/",
            fetch_redirect_response=False,
        )

    def test_each_node_local_instance_keeps_its_own_page(self):
        self._definition("local", shared=False, nodes=["pve1", "pve2", "pve3"])

        for node in ("pve1", "pve2", "pve3"):
            response = self.client.get(f"/clusters/scope/nodes/{node}/datastores/local/summary/")
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, f"Node-local datastore on {node}")

    def test_a_node_local_datastore_refuses_an_ambiguous_cluster_wide_url(self):
        """Three disks are called `local` here. Picking one would show a node's
        capacity under another node's name rather than admit the ambiguity."""
        self._definition("local", shared=False, nodes=["pve1", "pve2", "pve3"])

        response = self.client.get("/clusters/scope/datastores/local/summary/")

        self.assertEqual(response.status_code, 404)

    def test_a_single_instance_node_local_datastore_moves_to_its_node(self):
        self._definition("local", shared=False, nodes=["pve1"])

        response = self.client.get("/clusters/scope/datastores/local/summary/")

        self.assertRedirects(
            response,
            "/clusters/scope/nodes/pve1/datastores/local/summary/",
            fetch_redirect_response=False,
        )

    def test_a_node_that_does_not_hold_the_datastore_is_not_a_page(self):
        self._definition("local", shared=False, nodes=["pve1"])

        response = self.client.get("/clusters/scope/nodes/pve2/datastores/local/summary/")

        self.assertEqual(response.status_code, 404)


class DatastoreTabsAreUniformTests(TestCase):
    """Every datastore shows every tab; a backend that cannot answer says so.

    The point is that the page's shape never depends on the backend. An iSCSI LUN
    has no file tree and an unregistered NFS export has no permissions to read,
    and both used to mean a different set of tabs — or a different page entirely.
    """

    TABS = ("summary", "monitor", "configure", "content", "permissions", "files", "volumes", "nodes", "vms")

    def setUp(self):
        self.cluster = ProxmoxCluster.objects.create(key="tabs", display_name="Tabs", enabled=True)

    def _definition(self, storage_id, storage_type, *, shared=True, nodes=("pve1",)):
        definition = ClusterStorage.objects.create(
            cluster=self.cluster,
            storage_id=storage_id,
            storage_type=storage_type,
            shared=shared,
            present=True,
            content=["images"],
            config={"storage": storage_id},
        )
        for node in nodes:
            ClusterStorageNodeState.objects.create(
                cluster_storage=definition, node=node, present=True, active=True, enabled=True
            )
        return definition

    def test_a_block_backend_without_a_file_tree_still_has_every_tab(self):
        self._definition("iscsi-lun", "iscsi")

        for tab in self.TABS:
            with self.subTest(tab=tab):
                response = self.client.get(f"/clusters/tabs/datastores/iscsi-lun/{tab}/")
                self.assertEqual(response.status_code, 200)

    def test_the_files_tab_states_why_a_block_backend_has_none(self):
        self._definition("iscsi-lun", "iscsi")

        response = self.client.get("/clusters/tabs/datastores/iscsi-lun/files/")

        self.assertContains(response, "Files are unavailable")
        self.assertContains(response, "not a browsable file-tree backend")

    def test_the_permissions_tab_states_that_no_mount_is_registered(self):
        self._definition("TrueNAS-VM", "nfs")

        response = self.client.get("/clusters/tabs/datastores/TrueNAS-VM/permissions/")

        self.assertContains(response, "Permissions are unavailable")
        self.assertContains(response, "No host mount is registered")

    def test_the_nodes_tab_lists_every_instance_of_a_shared_datastore(self):
        self._definition("TrueNAS-VM", "nfs", nodes=("pve1", "pve2", "pve3"))

        response = self.client.get("/clusters/tabs/datastores/TrueNAS-VM/nodes/")

        for node in ("pve1", "pve2", "pve3"):
            self.assertContains(response, f">{node}<")
        self.assertContains(response, "pve-helper&#x27;s shared-mount gate")

    def test_the_nodes_tab_names_the_same_named_disks_on_the_other_nodes(self):
        """`local` is a different disk on every node and Proxmox names them all
        alike, so this tab is the only place the UI can say they are not one."""
        self._definition("local", "dir", shared=False, nodes=("pve1", "pve2", "pve3"))

        response = self.client.get("/clusters/tabs/nodes/pve1/datastores/local/nodes/")

        self.assertContains(response, "3 separate disks that share a name")
        self.assertContains(response, "/clusters/tabs/nodes/pve2/datastores/local/nodes/")
        self.assertContains(response, "/clusters/tabs/nodes/pve3/datastores/local/nodes/")


class DatastoreNavHighlightTests(TestCase):
    """The datastore page is its own destination, not the storage dashboard.

    It used to claim the dashboard's navigation key, a leftover from when the only
    way in was the Overview table. Selecting a datastore therefore lit the Overview
    leaf as well as the datastore's own, so the sidebar showed two current pages.
    """

    def test_a_datastore_page_does_not_also_highlight_overview(self):
        cluster = ProxmoxCluster.objects.create(key="hl", display_name="Hl", enabled=True)
        definition = ClusterStorage.objects.create(
            cluster=cluster, storage_id="TrueNAS-VM", storage_type="nfs", shared=True, present=True
        )
        ClusterStorageNodeState.objects.create(
            cluster_storage=definition, node="pve1", present=True, active=True, enabled=True
        )

        response = self.client.get("/clusters/hl/datastores/TrueNAS-VM/summary/")

        self.assertEqual(response.context["active_nav"], "datastore")
        html = response.content.decode()
        overview = html.split('href="/"', 1)[0].rsplit("<a ", 1)[1]
        self.assertNotIn("active", overview)
        # The Storage module itself must still read as the current one.
        self.assertIn("active-module", html)


class DatastorePageKeepsEveryPanelTests(TestCase):
    """The merged page must not quietly show less than the page it replaced.

    Moving the tabs across is easy to do half-way: the shell renders, every tab
    answers 200, and several panels are simply gone. This asserts the panels by
    name, so dropping one fails here instead of being noticed in a screenshot.
    """

    SUMMARY_PANELS = ("Filesystem", "Access", "Latest Scan", "Classification", "Storage Gate")
    # Every class stays listed even at zero: a missing one would read as "not
    # evaluated" rather than "none found".
    CLASSIFICATION_CHIPS = (
        "Referenced", "Likely orphan", "Blocked", "Unknown",
        "Infrastructure", "Proxmox content", "Import source", "Trash",
    )
    CONFIGURE_PANELS = ("Proxmox Configuration", "App Configuration", "Filesystem")
    METADATA_LABELS = ("Type", "Server", "Export", "PVE Path", "Content", "Options", "Preallocation", "Shared")

    def setUp(self):
        self.cluster = ProxmoxCluster.objects.create(key="panels", display_name="Panels", enabled=True)
        self.definition = ClusterStorage.objects.create(
            cluster=self.cluster,
            storage_id="TrueNAS-FS",
            storage_type="nfs",
            shared=True,
            present=True,
            content=["images", "iso"],
            config={"storage": "TrueNAS-FS", "type": "nfs", "server": "10.10.20.10", "export": "/mnt/Pool/Proxmox"},
        )
        ClusterStorageNodeState.objects.create(
            cluster_storage=self.definition,
            node="pve1",
            present=True,
            active=True,
            enabled=True,
            total_bytes=1000,
            used_bytes=400,
            available_bytes=600,
        )

    def test_summary_carries_every_panel_the_mount_page_had(self):
        response = self.client.get("/clusters/panels/datastores/TrueNAS-FS/summary/")

        for panel in self.SUMMARY_PANELS:
            with self.subTest(panel=panel):
                self.assertContains(response, panel)

    def test_configuration_carries_every_panel_the_mount_page_had(self):
        response = self.client.get("/clusters/panels/datastores/TrueNAS-FS/configure/")

        for panel in self.CONFIGURE_PANELS:
            with self.subTest(panel=panel):
                self.assertContains(response, panel)

    def test_the_definition_fields_come_from_the_catalog_not_a_filesystem_scan(self):
        """The mount page read these from scan-derived `StorageMount.details` and
        rendered a row of dashes on exactly the datastores that do have a
        definition. The catalog holds the real values, so they are filled in."""
        response = self.client.get("/clusters/panels/datastores/TrueNAS-FS/summary/")

        for label in self.METADATA_LABELS:
            with self.subTest(label=label):
                self.assertContains(response, label)
        self.assertContains(response, "10.10.20.10")
        self.assertContains(response, "/mnt/Pool/Proxmox")
        self.assertContains(response, "images, iso")

    def test_summary_lists_every_classification_without_a_row_each(self):
        mount = StorageMount.objects.create(
            storage_id="TrueNAS-FS", display_name="TrueNAS-FS", path="/storages/truenas-fs",
            relative_path="truenas-fs", enabled=True,
        )
        ClusterStorageMount.objects.create(
            cluster_storage=self.definition, mount=mount, node=None,
            scope=ClusterStorageMount.Scope.SHARED,
        )
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED, finished_at=timezone.now())
        FileInventory.objects.create(
            scan_run=scan, storage=mount, path="images/101/vm-101-disk-0.qcow2",
            entry_type=FileInventory.EntryType.FILE,
            classification=FileInventory.Classification.REFERENCED,
        )

        response = self.client.get("/clusters/panels/datastores/TrueNAS-FS/summary/")

        for chip in self.CLASSIFICATION_CHIPS:
            with self.subTest(chip=chip):
                self.assertContains(response, chip)
        self.assertContains(response, "classification-chips")
        self.assertContains(response, "Total files")

    def test_the_mount_only_panels_say_why_they_are_empty(self):
        """Without a registered mount the panels stay, and each states its reason
        rather than vanishing and changing the shape of the page."""
        response = self.client.get("/clusters/panels/datastores/TrueNAS-FS/configure/")

        self.assertContains(response, "App Configuration")
        self.assertContains(response, "No host mount is registered")

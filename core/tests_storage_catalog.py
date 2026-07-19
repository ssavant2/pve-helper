from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from core.models import (
    ClusterStorage,
    ClusterStorageMount,
    ClusterStorageVolumeObservation,
    CurrentGuestInventory,
    ProxmoxCluster,
    StorageCatalogState,
    StorageMount,
)
from core.services.refs import (
    ClusterStorageRef,
    MountRef,
    StorageInstanceRef,
    VolumeRef,
)
from core.services.storage_backends import backend_profile
from core.services.storage_catalog import (
    UsageState,
    refresh_storage_metadata,
    refresh_storage_volumes,
    storage_view,
    usage_preflight,
)
from core.services.storage_mounts import (
    StorageMountError,
    bind_storage_mount,
    mount_health,
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
            self.assertNotIn("TRUENAS_FS_HOST_PATH", source)


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
        self.assertEqual(len(storage_view(shared).volumes), 1)
        self.assertEqual(len(storage_view(local, node="pve2").volumes), 1)

    def test_unchanged_metadata_refresh_preserves_volume_coverage(self):
        self._metadata()
        first = self._volumes()
        volume_generation = first.volume_generation

        refreshed = self._metadata()

        self.assertTrue(refreshed.volume_complete)
        self.assertEqual(refreshed.volume_generation, volume_generation)
        self.assertEqual(
            refreshed.volume_based_on_metadata_generation,
            refreshed.metadata_generation,
        )
        self.assertFalse(
            ClusterStorageVolumeObservation.objects.exclude(
                based_on_metadata_generation=refreshed.metadata_generation
            ).exists()
        )

    def test_changed_metadata_invalidates_volume_coverage(self):
        self._metadata()
        self._volumes()
        self.responses["storage"][0]["content"] = "images,iso,backup"

        refreshed = self._metadata()

        self.assertFalse(refreshed.volume_complete)

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
        first = self._volumes()
        generation = first.volume_generation
        observation_count = ClusterStorageVolumeObservation.objects.filter(
            cluster_storage__cluster=self.cluster
        ).count()
        self.responses["nodes/pve2/storage/shared/content"] = []

        failed = self._volumes()

        self.assertFalse(failed.volume_complete)
        self.assertEqual(failed.volume_generation, generation)
        self.assertEqual(
            ClusterStorageVolumeObservation.objects.filter(cluster_storage__cluster=self.cluster).count(),
            observation_count,
        )

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
        unknown = usage_preflight(local, node="", fresh=False)

        self.assertEqual(referenced.state, UsageState.REFERENCED)
        self.assertTrue(referenced.token)
        self.assertEqual(unknown.state, UsageState.UNKNOWN)

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

        StorageCatalogState.objects.filter(cluster=cluster_b).update(volume_complete=False)
        incomplete = usage_preflight(shared, volid="shared:100/vm-100-disk-0.qcow2", fresh=False)
        self.assertEqual(incomplete.state, UsageState.UNKNOWN)

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
        self.assertIn("not been explicitly verified", result.reason)


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

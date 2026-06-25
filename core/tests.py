from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.contrib.auth import get_user_model

from core.models import AuditEvent, FileInventory, ProxmoxEndpoint, ScanRun, StorageMount
from core.services.classification import classify_entry, extract_disk_references
from core.services.config import sync_runtime_configuration
from core.services.storage import StorageScanner


class ClassificationTests(SimpleTestCase):
    def test_extracts_disk_references_from_nested_snapshot_config(self):
        config = {
            "scsi0": "TrueNAS-VM:100/vm-100-disk-0.qcow2,size=32G",
            "ide2": "none,media=cdrom",
            "snapshots": {
                "before-upgrade": {
                    "scsi0": "TrueNAS-VM:100/vm-100-disk-0.qcow2,size=32G",
                    "unused0": "TrueNAS-VM:100/vm-100-disk-1.qcow2",
                }
            },
        }

        references = extract_disk_references(config)

        self.assertEqual(
            references,
            [
                "TrueNAS-VM:100/vm-100-disk-0.qcow2",
                "TrueNAS-VM:100/vm-100-disk-1.qcow2",
            ],
        )

    def test_unreferenced_vm_disk_is_blocked_when_gate_is_not_ok(self):
        result = classify_entry(
            relative_path="images/100/vm-100-disk-0.qcow2",
            entry_type=FileInventory.EntryType.FILE,
            content_category="vm_disk",
            derived_volid="TrueNAS-VM:100/vm-100-disk-0.qcow2",
            referenced_volids=set(),
            template_vmids=set(),
            gate_ok=False,
            missing_consumers=["pve3"],
        )

        self.assertEqual(result.classification, FileInventory.Classification.CLASSIFICATION_BLOCKED)

    def test_base_image_is_never_likely_orphan_in_v1(self):
        result = classify_entry(
            relative_path="images/900/base-900-disk-0.qcow2",
            entry_type=FileInventory.EntryType.FILE,
            content_category="base_image",
            derived_volid="TrueNAS-VM:900/base-900-disk-0.qcow2",
            referenced_volids=set(),
            template_vmids=set(),
            gate_ok=True,
            missing_consumers=[],
        )

        self.assertEqual(result.classification, FileInventory.Classification.UNKNOWN)

    def test_storage_scanner_records_permission_errors_without_raising(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            readable = root / "dump"
            blocked = root / "images" / "500"
            readable.mkdir()
            blocked.mkdir(parents=True)
            blocked.chmod(0)

            try:
                scanner = StorageScanner("TrueNAS-VM", root.as_posix())
                entries = list(scanner.iter_entries())
            finally:
                blocked.chmod(0o700)

        self.assertIn("images/500", {entry.relative_path for entry in entries})
        self.assertEqual(scanner.errors[0]["path"], "images/500")
        self.assertEqual(scanner.errors[0]["error"], "PermissionError")


class RuntimeConfigurationTests(TestCase):
    @override_settings(
        PVE_ENDPOINTS=["https://pve-node-1.example.com:8006"],
        PVE_EXPECTED_CONSUMERS=["pve3"],
        TRUENAS_FS_STORAGE_ID="TrueNAS-FS",
        TRUENAS_VM_STORAGE_ID="TrueNAS-VM",
        TRUENAS_FS_EXPORT="203.0.113.20:/mnt/Pool-FS/FS/Proxmox",
        TRUENAS_VM_EXPORT="203.0.113.20:/mnt/Pool-VMs/VM/Proxmox",
        TRUENAS_FS_CONTAINER_PATH="/storages/truenas-fs",
        TRUENAS_VM_CONTAINER_PATH="/storages/truenas-vm",
    )
    def test_sync_runtime_configuration_from_settings(self):
        endpoints, storages = sync_runtime_configuration()

        self.assertEqual([endpoint.name for endpoint in endpoints], ["pve3"])
        self.assertEqual(ProxmoxEndpoint.objects.get(name="pve3").url, "https://pve-node-1.example.com:8006")
        self.assertEqual({storage.storage_id for storage in storages}, {"TrueNAS-FS", "TrueNAS-VM"})
        self.assertEqual(StorageMount.objects.get(storage_id="TrueNAS-VM").expected_consumers, ["pve3"])


@override_settings(APP_REQUIRE_LOGIN=False)
class ViewSmokeTests(TestCase):
    def test_storage_views_render(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        storage = StorageMount.objects.create(
            storage_id="TrueNAS-VM",
            display_name="TrueNAS-VM",
            path="/storages/truenas-vm",
            expected_consumers=["pve3"],
        )
        scan = ScanRun.objects.create(
            status=ScanRun.Status.COMPLETED,
            progress_message="Smoke scan",
            storage_gate_status={
                "TrueNAS-VM": {
                    "ok": False,
                    "status": "blocked",
                    "expected_consumers": ["pve3"],
                    "missing_consumers": ["pve3"],
                }
            },
        )
        FileInventory.objects.create(
            scan_run=scan,
            storage=storage,
            path="images",
            entry_type=FileInventory.EntryType.DIRECTORY,
            content_category="unknown",
            classification=FileInventory.Classification.UNKNOWN,
        )
        FileInventory.objects.create(
            scan_run=scan,
            storage=storage,
            path="images/100",
            entry_type=FileInventory.EntryType.DIRECTORY,
            content_category="unknown",
            classification=FileInventory.Classification.UNKNOWN,
        )
        FileInventory.objects.create(
            scan_run=scan,
            storage=storage,
            path="images/100/vm-100-disk-0.qcow2",
            derived_volid="TrueNAS-VM:100/vm-100-disk-0.qcow2",
            content_category="vm_disk",
            classification=FileInventory.Classification.CLASSIFICATION_BLOCKED,
        )

        for name in ["core:dashboard", "core:datastores", "core:orphan_finder"]:
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200)

        response = self.client.get(reverse("core:storage_browser", args=["TrueNAS-VM"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "images")

        response = self.client.get(reverse("core:storage_browser", args=["TrueNAS-VM"]), {"path": "images/100"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "vm-100-disk-0.qcow2")

    def test_recent_tasks_endpoint_paginates_scans(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        scans = [
            ScanRun.objects.create(status=ScanRun.Status.COMPLETED, progress_message=f"Scan {index}")
            for index in range(6)
        ]
        AuditEvent.objects.create(
            user=user,
            username="viewer",
            action="scan.queued",
            object_type="scan_run",
            object_id=str(scans[-1].id),
        )

        response = self.client.get(reverse("core:recent_tasks"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["tasks"]), 5)
        self.assertEqual(payload["total"], 6)
        self.assertTrue(payload["has_next"])
        self.assertFalse(payload["has_previous"])
        self.assertEqual(payload["tasks"][0]["initiator"], "viewer")

        response = self.client.get(reverse("core:recent_tasks"), {"page": "1"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["tasks"]), 1)
        self.assertFalse(payload["has_next"])
        self.assertTrue(payload["has_previous"])

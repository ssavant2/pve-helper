from __future__ import annotations

from datetime import datetime
from typing import Any

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from core.models import FileInventory, ProxmoxInventory, ScanRun, StorageMount

from .classification import classify_entry
from .image_info import probe_qemu_image_info
from .storage import StorageScanner
from .storage_visibility import ignored_relative_paths_for_storage


def refresh_storage_directory(
    *,
    storage: StorageMount,
    scan: ScanRun,
    directory_path: str = "",
) -> None:
    scanner = StorageScanner(
        storage.storage_id,
        storage.path,
        ignored_paths=ignored_relative_paths_for_storage(storage),
    )
    entries = scanner.iter_directory(directory_path)
    referenced_volids, template_vmids = _scan_references(scan)
    gate = (scan.storage_gate_status or {}).get(storage.storage_id, {})
    gate_ok = bool(gate.get("ok"))
    missing_consumers = list(gate.get("missing_consumers") or [])

    rows = []
    for entry in entries:
        classification = classify_entry(
            relative_path=entry.relative_path,
            entry_type=entry.entry_type,
            content_category=entry.content_category,
            derived_volid=entry.derived_volid,
            referenced_volids=referenced_volids,
            template_vmids=template_vmids,
            gate_ok=gate_ok,
            missing_consumers=missing_consumers,
        )
        image_info = probe_qemu_image_info(
            path=entry.full_path,
            entry_type=entry.entry_type,
            content_category=entry.content_category,
        )
        rows.append(
            FileInventory(
                scan_run=scan,
                storage=storage,
                path=entry.path,
                derived_volid=entry.derived_volid,
                content_category=entry.content_category,
                entry_type=entry.entry_type,
                size_bytes=entry.size_bytes,
                modified_at=_from_timestamp(entry.modified_at),
                classification=classification.classification,
                classification_reason=classification.reason,
                matched_object=classification.matched_object,
                evidence={
                    **classification.evidence,
                    "full_path": entry.full_path,
                    "image_info": image_info,
                    "partial_refresh": True,
                    "partial_refresh_directory": directory_path or "/",
                },
            )
        )

    with transaction.atomic():
        _direct_children(scan=scan, storage=storage, directory_path=directory_path).delete()
        FileInventory.objects.bulk_create(rows, batch_size=500)
        scan.filesystem_scan_at = timezone.now()
        scan.summary_counts = _summary_counts(scan)
        scan.save(update_fields=["filesystem_scan_at", "summary_counts", "updated_at"])


def _direct_children(scan: ScanRun, storage: StorageMount, directory_path: str):
    queryset = FileInventory.objects.filter(scan_run=scan, storage=storage)
    prefix = f"{directory_path}/" if directory_path else ""
    if prefix:
        queryset = queryset.filter(path__startswith=prefix)

    child_ids = []
    for item in queryset.only("id", "path"):
        remainder = item.path[len(prefix) :] if prefix else item.path
        if remainder and "/" not in remainder:
            child_ids.append(item.id)
    return FileInventory.objects.filter(id__in=child_ids)


def _scan_references(scan: ScanRun) -> tuple[set[str], set[int]]:
    referenced_volids: set[str] = set()
    template_vmids: set[int] = set()
    for obj in ProxmoxInventory.objects.filter(scan_run=scan):
        referenced_volids.update(obj.disk_references or [])
        if obj.object_type == ProxmoxInventory.ObjectType.VM and _is_template(obj.config) and obj.vmid is not None:
            template_vmids.add(obj.vmid)
    return referenced_volids, template_vmids


def _summary_counts(scan: ScanRun) -> dict[str, Any]:
    classifications = {
        item["classification"]: item["count"]
        for item in scan.files.values("classification").order_by().annotate(count=Count("id"))
    }
    return {
        "files": scan.files.count(),
        "proxmox_objects": scan.proxmox_objects.count(),
        "classifications": classifications,
    }


def _is_template(config: dict[str, Any]) -> bool:
    value = config.get("template")
    return value is True or str(value) == "1"


def _from_timestamp(value: float | None):
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.get_current_timezone())

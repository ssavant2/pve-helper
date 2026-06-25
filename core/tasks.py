from __future__ import annotations

from datetime import datetime
from typing import Any

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from .models import (
    FileInventory,
    ProxmoxEndpoint,
    ProxmoxInventory,
    ScanRun,
    StorageMount,
)
from .services.classification import classify_entry
from .services.config import sync_runtime_configuration
from .services.proxmox import ProxmoxClient
from .services.storage import StorageScanner


def run_scan(scan_run_id: int) -> None:
    scan = ScanRun.objects.get(pk=scan_run_id)
    try:
        _run_scan(scan)
    except Exception as exc:
        scan.status = ScanRun.Status.FAILED
        scan.finished_at = timezone.now()
        scan.progress_message = "Scan failed."
        scan.error_details = {"error": exc.__class__.__name__, "message": str(exc)}
        scan.save(update_fields=["status", "finished_at", "progress_message", "error_details", "updated_at"])
        raise


def _run_scan(scan: ScanRun) -> None:
    now = timezone.now()
    scan.status = ScanRun.Status.RUNNING
    scan.started_at = now
    scan.progress_message = "Syncing runtime configuration."
    scan.save(update_fields=["status", "started_at", "progress_message", "updated_at"])

    sync_runtime_configuration()
    endpoints = list(ProxmoxEndpoint.objects.filter(enabled=True).order_by("name"))
    storages = list(StorageMount.objects.filter(enabled=True).order_by("display_name"))

    scan.progress_message = "Reading Proxmox inventory."
    scan.save(update_fields=["progress_message", "updated_at"])

    endpoint_attempts: list[str] = []
    endpoint_successes: list[str] = []
    endpoint_errors: dict[str, Any] = {}
    proxmox_objects: list[ProxmoxInventory] = []
    referenced_volids: set[str] = set()
    template_vmids: set[int] = set()

    for endpoint in endpoints:
        client = ProxmoxClient(endpoint.url)
        node_name = client.discover_node_name(endpoint.name)
        endpoint_attempts.append(node_name)
        result = client.inventory(node_name)

        if result.ok:
            endpoint_successes.append(node_name)
            endpoint.last_health_status = "ok"
            endpoint.last_successful_scan = timezone.now()
            endpoint.details = {"node": node_name}
        else:
            endpoint.last_health_status = "error"
            endpoint.details = {"node": node_name, "errors": result.errors}
            endpoint_errors[node_name] = result.errors
        endpoint.save(update_fields=["last_health_status", "last_successful_scan", "details", "updated_at"])

        for obj in result.objects:
            referenced_volids.update(obj.disk_references)
            if obj.object_type == "vm" and _is_template(obj.config) and obj.vmid is not None:
                template_vmids.add(obj.vmid)
            proxmox_objects.append(
                ProxmoxInventory(
                    scan_run=scan,
                    node=obj.node,
                    object_type=obj.object_type,
                    vmid=obj.vmid,
                    name=obj.name,
                    status=obj.status,
                    config=obj.config,
                    disk_references=obj.disk_references,
                )
            )

    ProxmoxInventory.objects.bulk_create(proxmox_objects, batch_size=500)

    inventory_at = timezone.now()
    gate_status = _storage_gate_status(storages, endpoint_successes, inventory_at)

    scan.endpoints_attempted = endpoint_attempts
    scan.endpoints_succeeded = endpoint_successes
    scan.proxmox_inventory_at = inventory_at
    scan.storage_gate_status = gate_status
    scan.error_details = {"proxmox": endpoint_errors} if endpoint_errors else {}
    scan.progress_message = "Scanning storage roots."
    scan.save(
        update_fields=[
            "endpoints_attempted",
            "endpoints_succeeded",
            "proxmox_inventory_at",
            "storage_gate_status",
            "error_details",
            "progress_message",
            "updated_at",
        ]
    )

    file_rows: list[FileInventory] = []
    storage_errors: dict[str, Any] = {}

    for storage in storages:
        status = gate_status.get(storage.storage_id, {})
        gate_ok = bool(status.get("ok"))
        missing_consumers = list(status.get("missing_consumers") or [])

        scanner = StorageScanner(storage.storage_id, storage.path)
        for entry in scanner.iter_entries():
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
            file_rows.append(
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
                    },
                )
            )
        if scanner.errors:
            storage_errors[storage.storage_id] = {"errors": scanner.errors}

    with transaction.atomic():
        FileInventory.objects.bulk_create(file_rows, batch_size=1000)

    filesystem_at = timezone.now()
    if storage_errors:
        scan.error_details = {**scan.error_details, "storage": storage_errors}

    summary = _summary_counts(scan, len(proxmox_objects), len(file_rows))
    warning_count = len(endpoint_errors) + len(storage_errors)
    scan.status = ScanRun.Status.COMPLETED
    scan.finished_at = timezone.now()
    scan.filesystem_scan_at = filesystem_at
    scan.summary_counts = summary
    scan.progress_message = (
        f"Scan completed with {warning_count} warning(s)."
        if warning_count
        else "Scan completed."
    )
    scan.save(
        update_fields=[
            "status",
            "finished_at",
            "filesystem_scan_at",
            "summary_counts",
            "progress_message",
            "error_details",
            "updated_at",
        ]
    )


def _storage_gate_status(
    storages: list[StorageMount],
    endpoint_successes: list[str],
    inventory_at: datetime,
) -> dict[str, dict[str, Any]]:
    succeeded = set(endpoint_successes)
    result: dict[str, dict[str, Any]] = {}

    for storage in storages:
        expected = list(storage.expected_consumers or [])
        missing = sorted(set(expected) - succeeded)
        status = "ok" if not missing else "blocked"
        result[storage.storage_id] = {
            "ok": not missing,
            "status": status,
            "expected_consumers": expected,
            "inventoried_consumers": sorted(set(expected) & succeeded),
            "missing_consumers": missing,
        }
        for consumer in storage.consumer_statuses.all():
            consumer.last_gate_status = status if consumer.expected_node_name in expected else "not_expected"
            if consumer.expected_node_name in succeeded:
                consumer.last_successful_inventory_scan = inventory_at
            consumer.save(update_fields=["last_gate_status", "last_successful_inventory_scan", "updated_at"])

    return result


def _summary_counts(scan: ScanRun, proxmox_count: int, file_count: int) -> dict[str, Any]:
    classifications = {
        item["classification"]: item["count"]
        for item in scan.files.values("classification").order_by().annotate(count=Count("id"))
    }
    return {
        "files": file_count,
        "proxmox_objects": proxmox_count,
        "classifications": classifications,
    }


def _is_template(config: dict[str, Any]) -> bool:
    value = config.get("template")
    return value is True or str(value) == "1"


def _from_timestamp(value: float | None):
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.get_current_timezone())

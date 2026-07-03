from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from django_q.tasks import async_task

from .models import (
    AuditEvent,
    FileInventory,
    ProxmoxEndpoint,
    ProxmoxInventory,
    ScanRun,
    StorageMount,
    StorageSpaceSnapshot,
    TrashItem,
)
from .services.classification import classify_entry
from .services.config import sync_runtime_configuration
from .services.filesystem import storage_space_info
from .services.image_info import probe_qemu_image_info
from .services.partial_scan import refresh_storage_directory
from .services.proxmox import ProxmoxAPIError, ProxmoxClient, ProxmoxTaskTimeout, clear_live_guest_caches
from .services.scan_schedule import scan_schedule_state
from .services.scan_retention import prune_scan_history
from .services.scheduled_actions import dispatch_due_scheduled_actions, execute_scheduled_action_run
from .services.storage import StorageScanner
from .services.storage_actions import (
    StorageActionError,
    inflate_storage_file,
    normalize_uploaded_proxmox_image_paths,
    purge_trash_item,
)
from .services.storage_visibility import ignored_relative_paths_for_storage


SPACE_SNAPSHOT_RETENTION_DAYS = 8

logger = logging.getLogger(__name__)


def dispatch_scheduled_actions() -> dict[str, int | bool]:
    result = dispatch_due_scheduled_actions()
    return {
        "queued": result.queued,
        "missed": result.missed,
        "skipped": result.skipped,
        "disabled": result.disabled,
    }


def run_scheduled_action(run_id: int) -> None:
    execute_scheduled_action_run(run_id)


def poll_guest_audit_task(
    audit_event_id: int,
    endpoint_url: str,
    node: str,
    upid: str,
    timeout_seconds: int,
) -> None:
    event = AuditEvent.objects.filter(pk=audit_event_id).first()
    if event is None:
        return

    details = event.details if isinstance(event.details, dict) else {}
    details = dict(details)
    try:
        result = ProxmoxClient(endpoint_url).wait_for_task(
            node=node,
            upid=upid,
            timeout_seconds=timeout_seconds,
        )
    except ProxmoxTaskTimeout as exc:
        event.outcome = "failed"
        details["error"] = str(exc)
    except ProxmoxAPIError as exc:
        event.outcome = "failed"
        details["error"] = str(exc)
    else:
        details["proxmox_task"] = result.raw
        if result.success:
            event.outcome = "success"
        else:
            event.outcome = "failed"
            details["error"] = f"Proxmox task exitstatus: {result.exitstatus or result.status or 'unknown'}"

    details["finished_at"] = timezone.now().isoformat()
    event.details = details
    event.save(update_fields=["outcome", "details"])
    clear_live_guest_caches()


def enqueue_scheduled_scan() -> int | None:
    schedule_state = scan_schedule_state()
    active_scan = ScanRun.objects.filter(
        status__in=[
            ScanRun.Status.QUEUED,
            ScanRun.Status.RUNNING,
        ]
    ).order_by("-created_at").first()
    if active_scan:
        AuditEvent.objects.create(
            username="system",
            action="scan.schedule.skipped",
            object_type="scan_run",
            object_id=str(active_scan.id),
            outcome="skipped",
            details={"reason": "A scan is already queued or running."},
        )
        return None

    scan = ScanRun.objects.create(progress_message="Queued from schedule")
    task_id = async_task("core.tasks.run_scan", scan.id)
    scan.queued_task_id = task_id
    scan.save(update_fields=["queued_task_id", "updated_at"])

    AuditEvent.objects.create(
        username="system",
        action="scan.queued",
        object_type="scan_run",
        object_id=str(scan.id),
        outcome="success",
        details={
            "task_id": task_id,
            "source": "schedule",
            "interval_minutes": schedule_state.interval_minutes,
            "target_label": "All storages",
        },
    )
    return scan.id


def purge_expired_trash(max_age_days: int = 30) -> None:
    cutoff = timezone.now() - timedelta(days=max_age_days)
    expired = TrashItem.objects.filter(
        restore_status=TrashItem.RestoreStatus.TRASHED,
        moved_at__lte=cutoff,
    )
    purged = 0
    errors = []
    for item in expired:
        try:
            purge_trash_item(item=item)
        except StorageActionError as exc:
            errors.append({"item_id": item.id, "error": str(exc)})
            continue
        purged += 1

    AuditEvent.objects.create(
        username="system",
        action="trash.purge",
        object_type="trash",
        outcome="success" if not errors else "partial",
        details={
            "max_age_days": max_age_days,
            "purged": purged,
            "errors": errors[:10],
        },
    )


def purge_expired_audit_events(retention_days: int = 90) -> None:
    cutoff = timezone.now() - timedelta(days=retention_days)
    deleted_count, _deleted_by_model = AuditEvent.objects.filter(timestamp__lt=cutoff).delete()

    AuditEvent.objects.create(
        username="system",
        action="audit.retention.purge",
        object_type="audit_retention",
        object_id="automatic-audit-retention",
        outcome="success",
        details={
            "retention_days": retention_days,
            "purged": deleted_count,
        },
    )


def record_storage_space_snapshots(retention_days: int = SPACE_SNAPSHOT_RETENTION_DAYS) -> int:
    recorded_at = timezone.now()
    storages = list(StorageMount.objects.filter(enabled=True).order_by("display_name"))
    created = _record_space_snapshots(None, storages, recorded_at)
    cutoff = recorded_at - timedelta(days=retention_days)
    StorageSpaceSnapshot.objects.filter(recorded_at__lt=cutoff).delete()
    return created


def normalize_uploaded_proxmox_image_paths_task(
    storage_id: int,
    paths: list[str],
    username: str = "",
) -> None:
    storage = StorageMount.objects.get(pk=storage_id)
    try:
        result = normalize_uploaded_proxmox_image_paths(storage=storage, paths=paths)
        normalized = result["normalized"]
        AuditEvent.objects.create(
            username=username,
            action="file.upload_normalized",
            object_type="file",
            object_id=f"{storage.storage_id}:{', '.join(normalized) if normalized else '-'}",
            outcome="success" if normalized else "skipped",
            details={
                "storage_id": storage.storage_id,
                "storage_name": storage.display_name,
                "paths": paths,
                "normalized": normalized,
                "skipped": result["skipped"],
            },
        )
    except Exception as exc:
        AuditEvent.objects.create(
            username=username,
            action="file.upload_normalize_failed",
            object_type="file",
            object_id=f"{storage.storage_id}:{', '.join(paths)}",
            outcome="failed",
            details={
                "storage_id": storage.storage_id,
                "storage_name": storage.display_name,
                "paths": paths,
                "error": exc.__class__.__name__,
                "message": str(exc),
            },
        )
        raise


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


def inflate_storage_file_task(
    storage_id: int,
    entry_id: int,
    username: str = "",
    target_preallocation: str = "full",
) -> None:
    storage = StorageMount.objects.get(pk=storage_id)
    entry = FileInventory.objects.select_related("scan_run", "storage").get(pk=entry_id)
    try:
        result = inflate_storage_file(
            storage=storage,
            entry=entry,
            target_preallocation=target_preallocation,
        )
        refresh_scan = _latest_storage_result_scan(storage)
        if refresh_scan is None and entry.scan_run.status == ScanRun.Status.COMPLETED:
            refresh_scan = entry.scan_run
        if refresh_scan:
            refresh_storage_directory(
                storage=storage,
                scan=refresh_scan,
                directory_path=str(result["directory_path"]),
            )
        AuditEvent.objects.create(
            username=username,
            action="file.inflated",
            object_type="file",
            object_id=f"{storage.storage_id}:{result['path']}",
            outcome="success",
            details={
                "storage_id": storage.storage_id,
                "storage_name": storage.display_name,
                "path": result["path"],
                "target_preallocation": result["target_preallocation"],
                "refreshed_scan_id": refresh_scan.id if refresh_scan else None,
                "before": result["before"],
                "after": result["after"],
            },
        )
    except Exception as exc:
        AuditEvent.objects.create(
            username=username,
            action="file.inflate_failed",
            object_type="file",
            object_id=f"{storage.storage_id}:{entry.path}",
            outcome="failed",
            details={
                "storage_id": storage.storage_id,
                "storage_name": storage.display_name,
                "path": entry.path,
                "target_preallocation": target_preallocation,
                "error": str(exc),
                "error_type": exc.__class__.__name__,
            },
        )
        if not isinstance(exc, StorageActionError):
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
    scan_target = scan.target_storage
    if scan_target is not None:
        scan_target = StorageMount.objects.filter(pk=scan_target.pk, enabled=True).first()
        if scan_target is None:
            raise ValueError("Target storage is no longer enabled or available.")
    storages_to_scan = [scan_target] if scan_target is not None else storages

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

        if result.success:
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
    scan.progress_message = (
        f"Scanning {scan_target.display_name}."
        if scan_target is not None
        else "Scanning storage roots."
    )
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

    for storage in storages_to_scan:
        status = gate_status.get(storage.storage_id, {})
        gate_ok = bool(status.get("ok"))
        missing_consumers = list(status.get("missing_consumers") or [])

        scanner = StorageScanner(
            storage.storage_id,
            storage.path,
            ignored_paths=ignored_relative_paths_for_storage(storage),
        )
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
            image_info = probe_qemu_image_info(
                path=entry.full_path,
                entry_type=entry.entry_type,
                content_category=entry.content_category,
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
                        "image_info": image_info,
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
    _prune_scan_history_after_success()


def _prune_scan_history_after_success() -> None:
    try:
        result = prune_scan_history()
    except Exception as exc:
        logger.exception("Failed to prune old scan history")
        AuditEvent.objects.create(
            username="system",
            action="scan.retention.purge_failed",
            object_type="scan_retention",
            object_id="automatic-scan-retention",
            outcome="failed",
            details={
                "error": exc.__class__.__name__,
                "message": str(exc),
            },
        )
        return

    if not result.deleted_anything:
        return

    AuditEvent.objects.create(
        username="system",
        action="scan.retention.purge",
        object_type="scan_retention",
        object_id="automatic-scan-retention",
        outcome="success",
        details={
            "kept_scan_ids": sorted(result.kept_scan_ids),
            "deleted_files": result.deleted_files,
            "deleted_proxmox_objects": result.deleted_proxmox_objects,
            "deleted_scan_runs": result.deleted_scan_runs,
        },
    )


def _record_space_snapshots(
    scan: ScanRun | None, storages: list[StorageMount], recorded_at: datetime
) -> int:
    created = 0
    for storage in storages:
        space = storage_space_info(storage.path)
        if space.ok:
            StorageSpaceSnapshot.objects.create(
                storage=storage,
                scan_run=scan,
                recorded_at=recorded_at,
                total_bytes=space.total_bytes,
                available_bytes=space.available_bytes,
                used_bytes=space.used_bytes,
            )
            created += 1
    return created


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


def _latest_storage_result_scan(storage: StorageMount) -> ScanRun | None:
    return (
        ScanRun.objects.filter(status=ScanRun.Status.COMPLETED)
        .filter(Q(target_storage=storage) | Q(target_storage__isnull=True))
        .order_by("-filesystem_scan_at", "-finished_at", "-created_at")
        .first()
    )

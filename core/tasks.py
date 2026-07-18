from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

from django.db import connection
from django.db import transaction
from django.db.models import Count, F, Q
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django_q.models import Task
from django_q.tasks import async_task

from .models import (
    AuditEvent,
    FileInventory,
    ProxmoxEndpoint,
    ProxmoxInventory,
    ScanClusterObservation,
    ScanRun,
    StorageMount,
    StorageSpaceSnapshot,
    TrashItem,
)
from .services.classification import classify_entry, extract_disk_references
from .services.cluster_state_identity import cluster_advisory_lock_id
from .services.audit_events import record_audit_event
from .services.console_session_cleanup import prune_console_sessions
from .services.cluster_resolver import client_for_endpoint, cluster_clients
from .services.runtime_bootstrap import ensure_bootstrap
from .services.current_guest_inventory import (
    ScanGuestObservation,
    reconcile_live_guest_inventory,
    reconcile_scan_guest_inventory,
    refresh_current_guest_from_client,
    upsert_current_guest,
)
from .services.durable_guest_operations import (
    DurableGuestOperationError,
    client_for_audit_event,
)
from .services.filesystem import storage_space_info
from .services.image_info import probe_qemu_image_info
from .services.partial_scan import refresh_storage_directory
from .services.proxmox import (
    ProxmoxAPIError,
    ProxmoxClient,
    ProxmoxTaskTimeout,
    clear_live_guest_caches,
    fetch_live_guest_status,
    fetch_verified_guest_inventory,
)
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
from .services.task_queues import BULK_QUEUE_NAME, queued_task_ids


SPACE_SNAPSHOT_RETENTION_DAYS = 8

logger = logging.getLogger(__name__)

CURRENT_GUEST_REFRESH_LOCK_ID = 0x50564547554501


def _durable_or_legacy_guest_operation(
    event: AuditEvent,
    *,
    endpoint_url: str = "",
    node: str = "",
    object_type: str = "",
    vmid: int | None = None,
):
    """Resolve a Phase-3 operation, with one bounded pre-Phase-3 reader.

    Old Django-Q rows carried the endpoint and target as positional arguments.
    They may finish during a rolling deployment even though their AuditEvent has
    no cluster identity.  Only that explicit old payload is accepted; new event-
    only writers must resolve through the durable relation and GuestRef.
    """
    try:
        return client_for_audit_event(event, preferred_endpoint_url=endpoint_url)
    except DurableGuestOperationError:
        details = event.details if isinstance(event.details, dict) else {}
        legacy_type = object_type or str(details.get("target_type") or "")
        legacy_node = node or str(details.get("node") or details.get("target_node") or "")
        try:
            legacy_vmid = int(vmid if vmid is not None else details.get("vmid"))
        except (TypeError, ValueError):
            legacy_vmid = 0
        if (
            details.get("operation_payload_version") is None
            and endpoint_url
            and legacy_node
            and legacy_type in {"vm", "ct"}
            and legacy_vmid > 0
        ):
            return (
                ProxmoxClient(endpoint_url),
                SimpleNamespace(
                    cluster_key="",
                    node=legacy_node,
                    object_type=legacy_type,
                    vmid=legacy_vmid,
                ),
                None,
            )
        raise



def _cluster_clients():
    """Provider clients for the sole enabled cluster, or none.

    Workers carry no cluster scope of their own yet; Phase 3 gives their durable
    payloads a GuestRef and this resolves from that instead.
    """
    from .services.cluster_resolver import (
        ClusterResolutionError,
        cluster_clients,
        require_sole_enabled_cluster_for_legacy_caller,
    )

    try:
        return cluster_clients(require_sole_enabled_cluster_for_legacy_caller())
    except ClusterResolutionError:
        return []


def _first_cluster_client():
    return next(iter(_cluster_clients()), None)


def refresh_current_guest_inventory(*, cluster=None) -> dict[str, object]:
    """Refresh the non-blocking guest read model outside HTTP requests."""
    if cluster is None:
        from .services.cluster_resolver import require_sole_enabled_cluster_for_legacy_caller

        cluster = require_sole_enabled_cluster_for_legacy_caller()
    lock_id = cluster_advisory_lock_id(CURRENT_GUEST_REFRESH_LOCK_ID, cluster)
    acquired = connection.vendor != "postgresql"
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock_id])
            acquired = bool(cursor.fetchone()[0])
    if not acquired:
        return {"skipped": True, "reason": "refresh already running"}
    try:
        inventory = fetch_verified_guest_inventory(cluster=cluster)
        state = reconcile_live_guest_inventory(inventory)
        return {
            "skipped": False,
            "complete": state.complete,
            "guests": len(inventory.guests),
            "cluster_key": cluster.key,
            "errors": list(inventory.errors),
        }
    finally:
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])


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
    endpoint_url: str = "",
    node: str = "",
    upid: str = "",
    timeout_seconds: int | None = None,
) -> None:
    event = AuditEvent.objects.filter(pk=audit_event_id).first()
    if event is None:
        return

    details = event.details if isinstance(event.details, dict) else {}
    details = dict(details)
    try:
        client, ref, cluster = _durable_or_legacy_guest_operation(
            event,
            endpoint_url=endpoint_url,
            node=node,
        )
    except DurableGuestOperationError as exc:
        event.outcome = "failed"
        details["error"] = str(exc)
        details["finished_at"] = timezone.now().isoformat()
        event.details = details
        event.save(update_fields=["outcome", "details"])
        return
    node = node or str(details.get("proxmox_task_node") or ref.node)
    upid = upid or str(details.get("proxmox_task_upid") or "")
    timeout_seconds = int(
        timeout_seconds
        or details.get("task_timeout_seconds")
        or settings.SCHEDULED_ACTION_TIMEOUT_SECONDS
    )
    if not node or not upid:
        event.outcome = "failed"
        details["error"] = "The queued operation is missing its Proxmox task identity."
        details["finished_at"] = timezone.now().isoformat()
        event.details = details
        event.save(update_fields=["outcome", "details"])
        return
    try:
        result = client.wait_for_task(
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

    if AuditEvent.objects.filter(pk=audit_event_id, outcome="cancelled").exists():
        return

    details["finished_at"] = timezone.now().isoformat()
    event.details = details
    event.save(update_fields=["outcome", "details"])

    if event.outcome == "success":
        try:
            target_type = ref.object_type
            target_vmid = ref.vmid
            target_node = ref.node or str(details.get("node") or node)
            allow_relocation = event.action in {"guest.migrate", "guest.destroy"}
            if event.action == "guest.clone.create" and details.get("new_vmid"):
                target_vmid = int(details["new_vmid"])
                allow_relocation = True
            refresh = refresh_current_guest_from_client(
                client,
                node=target_node,
                object_type=target_type,
                vmid=target_vmid,
                cluster=cluster,
                allow_relocation=allow_relocation,
                delete_if_authoritatively_absent=event.action == "guest.destroy",
            )
            if refresh.error:
                details["projection_refresh_error"] = refresh.error
            else:
                details["projection_refreshed_at"] = timezone.now().isoformat()
        except (TypeError, ValueError) as exc:
            details["projection_refresh_error"] = f"Invalid operation target: {exc}"
        except Exception as exc:
            logger.exception("Guest operation succeeded but targeted projection refresh failed")
            details["projection_refresh_error"] = f"{exc.__class__.__name__}: {exc}"
        event.details = details
        event.save(update_fields=["details"])

    clear_live_guest_caches(cluster=cluster)
    if event.outcome == "success":
        enqueue_storage_rescan(details.get("rescan_storage_ids") or [])


def enqueue_storage_rescan(storage_ids: list[str]) -> None:
    """Kick a storage scan scoped to each affected storage so freshly created or
    destroyed guest disks reclassify immediately instead of lingering as orphans
    until the next scheduled scan. Skips storages that already have a scan queued
    or running, and dedupes."""
    active = {ScanRun.Status.QUEUED, ScanRun.Status.RUNNING}
    seen: set[str] = set()
    for storage_id in storage_ids:
        if not storage_id or storage_id in seen:
            continue
        seen.add(storage_id)
        storage = StorageMount.objects.filter(storage_id=storage_id, enabled=True).first()
        if storage is None:
            continue
        if ScanRun.objects.filter(target_storage=storage, status__in=active).exists():
            continue
        scan = ScanRun.objects.create(
            progress_message="Auto-scan after clone/destroy",
            target_storage=storage,
            target_label=storage.display_name,
        )
        task_id = async_task("core.tasks.run_scan", scan.id, q_options={"cluster": BULK_QUEUE_NAME})
        scan.queued_task_id = task_id
        scan.save(update_fields=["queued_task_id", "updated_at"])


# A guest audit event stuck at outcome="running" longer than this, with no live
# Proxmox task backing it, is treated as dead (worker crash / deploy race).
STALE_GUEST_TASK_SECONDS = 15 * 60
STALE_BULK_TASK_GRACE_SECONDS = 15 * 60


def reap_stale_guest_tasks() -> dict[str, int]:
    """Safety net: finalize guest audit events stuck at ``running``.

    A worker that dies (or a task that fails at the framework level) before
    finalizing leaves the event at ``running`` forever. For each stale event: if
    it carries a Proxmox UPID that is still running, leave it; if that task has
    stopped, resolve the outcome from it; otherwise (no resolvable task) mark it
    failed so it stops showing as a phantom running row.
    """
    threshold = timezone.now() - timedelta(seconds=STALE_GUEST_TASK_SECONDS)
    stale = AuditEvent.objects.filter(
        action__startswith="guest.", outcome="running", timestamp__lt=threshold
    )
    resolved = 0
    reaped = 0
    changed = False
    for event in stale:
        details = dict(event.details) if isinstance(event.details, dict) else {}
        upid = details.get("proxmox_task_upid")
        node = details.get("proxmox_task_node")
        endpoint = details.get("proxmox_endpoint") or ""
        status = None
        if upid and node:
            client = ProxmoxClient(endpoint) if endpoint else _first_cluster_client()
            if client is not None:
                try:
                    status = client.get(f"nodes/{quote(str(node), safe='')}/tasks/{quote(str(upid), safe='')}/status")
                except ProxmoxAPIError:
                    status = None
        if isinstance(status, dict) and status.get("status") == "running":
            continue  # genuinely still running — leave it
        if isinstance(status, dict) and status.get("status") == "stopped":
            exitstatus = status.get("exitstatus")
            if exitstatus == "OK":
                event.outcome = "success"
            else:
                event.outcome = "failed"
                details["error"] = details.get("error") or f"Proxmox task exitstatus: {exitstatus or 'unknown'}"
            resolved += 1
        else:
            event.outcome = "failed"
            details["error"] = details.get("error") or "Task did not finish (worker unavailable); resolved by reaper."
            reaped += 1
        details["finished_at"] = timezone.now().isoformat()
        details["reaped"] = True
        event.details = details
        event.save(update_fields=["outcome", "details"])
        changed = True
    if changed:
        clear_live_guest_caches()
    resolved_force_stop_questions = _resolve_force_stop_questions(now=timezone.now())
    interrupted_tag_operations = _reap_stale_tag_operations(now=timezone.now())
    interrupted_tag_inventory_refreshes = _reap_stale_tag_inventory_refreshes(now=timezone.now())
    return {
        "resolved_from_proxmox": resolved,
        "reaped_dead": reaped,
        "resolved_force_stop_questions": resolved_force_stop_questions,
        "interrupted_tag_operations": interrupted_tag_operations,
        "interrupted_tag_inventory_refreshes": interrupted_tag_inventory_refreshes,
    }


def _reap_stale_tag_operations(*, now) -> int:
    """Resolve stale fan-outs only after checking their durable queue state."""
    threshold = now - timedelta(seconds=STALE_GUEST_TASK_SECONDS)
    candidates = list(
        AuditEvent.objects.filter(
            action="tag.bulk_operation",
            outcome__in=["queued", "running"],
        )
    )
    interrupted = 0
    for candidate in candidates:
        details = dict(candidate.details) if isinstance(candidate.details, dict) else {}
        activity_at = parse_datetime(
            str(details.get("heartbeat_at") or details.get("queued_at") or "")
        ) or candidate.timestamp
        if activity_at > threshold:
            continue
        with transaction.atomic():
            event = AuditEvent.objects.select_for_update().get(pk=candidate.pk)
            if event.outcome not in {"queued", "running"}:
                continue
            details = dict(event.details) if isinstance(event.details, dict) else {}
            activity_at = parse_datetime(
                str(details.get("heartbeat_at") or details.get("queued_at") or "")
            ) or event.timestamp
            if activity_at > threshold:
                continue
            task_id = str(details.get("worker_task_id") or "")
            task = Task.objects.filter(id=task_id).only("id", "success").first() if task_id else None
            if task_id and task is None and task_id in queued_task_ids({task_id}):
                continue
            details["stage"] = "interrupted"
            if task is not None and not task.success:
                details["error"] = "The background worker reported that the operation failed; retry is safe."
            elif task is not None:
                details["error"] = "The background task finished without finalizing the operation; retry is safe."
            elif task_id:
                details["error"] = "The queued background task is no longer present; retry is safe."
            else:
                details["error"] = "The operation was not attached to a background task; retry is safe."
            details["retryable"] = True
            details["interrupted_at"] = now.isoformat()
            details["finished_at"] = now.isoformat()
            event.outcome = "failed"
            event.details = details
            event.save(update_fields=["outcome", "details"])
            interrupted += 1
    return interrupted


def _reap_stale_tag_inventory_refreshes(*, now) -> int:
    threshold = now - timedelta(seconds=STALE_GUEST_TASK_SECONDS)
    candidates = list(
        AuditEvent.objects.filter(
            action="tag.inventory.refresh",
            outcome__in=["queued", "running"],
        )
    )
    interrupted = 0
    for candidate in candidates:
        details = dict(candidate.details) if isinstance(candidate.details, dict) else {}
        activity_at = parse_datetime(
            str(details.get("heartbeat_at") or details.get("queued_at") or "")
        ) or candidate.timestamp
        if activity_at > threshold:
            continue
        with transaction.atomic():
            event = AuditEvent.objects.select_for_update().get(pk=candidate.pk)
            if event.outcome not in {"queued", "running"}:
                continue
            details = dict(event.details) if isinstance(event.details, dict) else {}
            activity_at = parse_datetime(
                str(details.get("heartbeat_at") or details.get("queued_at") or "")
            ) or event.timestamp
            if activity_at > threshold:
                continue
            task_id = str(details.get("worker_task_id") or "")
            task = Task.objects.filter(id=task_id).only("id", "success").first() if task_id else None
            if task_id and task is None and task_id in queued_task_ids({task_id}):
                continue
            details["stage"] = "interrupted"
            details["error"] = "The tag inventory refresh worker stopped reporting progress; start a new refresh."
            details["interrupted_at"] = now.isoformat()
            details["finished_at"] = now.isoformat()
            event.outcome = "failed"
            event.details = details
            event.save(update_fields=["outcome", "details"])
            interrupted += 1
    return interrupted

def _resolve_force_stop_questions(*, now) -> int:
    """Resolve timed-out shutdown questions outside the request/HTML path."""
    candidates = list(
        AuditEvent.objects.filter(
            action="guest.power.shutdown",
            outcome="failed",
            timestamp__gte=now - timedelta(minutes=60),
        ).order_by("-timestamp")
    )
    candidates = [
        event
        for event in candidates
        if _is_open_force_stop_question(event)
    ]
    if not candidates:
        return 0

    try:
        statuses = fetch_live_guest_status()
    except Exception:  # best-effort control-plane reconciliation
        return 0

    resolved = 0
    for event in candidates:
        details = dict(event.details) if isinstance(event.details, dict) else {}
        target_type = str(details.get("target_type") or "")
        try:
            vmid = int(details.get("vmid"))
        except (TypeError, ValueError):
            continue
        stopped = any(
            object_type == target_type and guest_vmid == vmid and status == "stopped"
            for (_node, object_type, guest_vmid), status in statuses.items()
        )
        if not stopped:
            continue
        details["force_stop_resolved_at"] = now.isoformat()
        details["force_stop_resolution"] = "guest_stopped"
        event.details = details
        event.save(update_fields=["details"])
        resolved += 1
    return resolved


def _is_open_force_stop_question(event: AuditEvent) -> bool:
    details = event.details if isinstance(event.details, dict) else {}
    error_text = str(details.get("error") or "").lower()
    return (
        not details.get("force_stop_dismissed")
        and not details.get("force_stop_resolved_at")
        and bool(details.get("target_type"))
        and details.get("vmid") is not None
        and ("timeout" in error_text or "powerdown failed" in error_text)
    )


def reap_stale_bulk_tasks(*, now=None) -> dict[str, int]:
    """Resolve bulk work whose Django-Q worker died before finalization.

    A scan sets itself to running before doing I/O, while an inflate leaves a
    queued audit event until its terminal event is written. If the worker is
    killed by the queue or a deploy, neither path has its normal exception
    handler. Reconcile only after the workflow timeout plus a conservative
    grace period so valid work is never marked failed prematurely.
    """
    now = now or timezone.now()
    scan_cutoff = now - timedelta(seconds=settings.SCAN_TASK_TIMEOUT_SECONDS + STALE_BULK_TASK_GRACE_SECONDS)
    inflate_cutoff = now - timedelta(
        seconds=settings.STORAGE_INFLATE_TIMEOUT_SECONDS + STALE_BULK_TASK_GRACE_SECONDS
    )

    scans_reaped = 0
    scans = ScanRun.objects.filter(status=ScanRun.Status.RUNNING, started_at__lt=scan_cutoff)
    for scan in scans:
        scan.status = ScanRun.Status.FAILED
        scan.finished_at = now
        scan.progress_message = "Scan did not finish before the worker timeout."
        scan.error_details = {
            "error": "WorkerTimeout",
            "message": "Reconciled after exceeding the scan timeout and grace period.",
            "reaped": True,
        }
        scan.save(update_fields=["status", "finished_at", "progress_message", "error_details", "updated_at"])
        _audit_scan_terminal(scan, "scan.failed", "failed")
        scans_reaped += 1

    inflates_reaped = 0
    queued_inflates = AuditEvent.objects.filter(action="file.inflate_queued", timestamp__lt=inflate_cutoff)
    for queued in queued_inflates:
        details = dict(queued.details) if isinstance(queued.details, dict) else {}
        storage_id = queued.storage_id or str(details.get("storage_id") or "")
        path = queued.path or str(details.get("path") or "")
        if not storage_id or not path:
            continue
        has_terminal_event = AuditEvent.objects.filter(
            action__in=["file.inflated", "file.inflate_failed"],
            storage_id=storage_id,
            path=path,
            timestamp__gte=queued.timestamp,
        ).exists()
        if has_terminal_event:
            continue
        record_audit_event(
            username="system",
            action="file.inflate_failed",
            object_type="file",
            object_id=queued.object_id,
            outcome="failed",
            details={
                "storage_id": storage_id,
                "path": path,
                "target_preallocation": details.get("target_preallocation") or "",
                "task_id": details.get("task_id") or "",
                "error": "Inflate did not finish before the worker timeout.",
                "error_type": "WorkerTimeout",
                "reaped": True,
            },
        )
        inflates_reaped += 1

    return {"scans_reaped": scans_reaped, "inflates_reaped": inflates_reaped}


def migrate_guest_disks_task(
    audit_event_id: int,
    endpoint_url: str = "",
    node: str = "",
    object_type: str = "",
    vmid: int | None = None,
    moves: list | None = None,
    timeout_seconds: int | None = None,
) -> None:
    """Worker task: relocate every one of a guest's volumes to a target storage.

    Proxmox locks the guest for each ``move_disk``/``move_volume``, so the moves
    run **sequentially** — one UPID at a time — recording the in-flight UPID on the
    audit row (so it stays cancelable) and stopping on the first failure with a
    precise partial-success report.
    """
    event = AuditEvent.objects.filter(pk=audit_event_id).first()
    if event is None:
        return
    details = dict(event.details) if isinstance(event.details, dict) else {}
    try:
        client, ref, cluster = _durable_or_legacy_guest_operation(
            event,
            endpoint_url=endpoint_url,
            node=node,
            object_type=object_type,
            vmid=vmid,
        )
    except DurableGuestOperationError as exc:
        event.outcome = "failed"
        event.details = {**details, "error": str(exc), "finished_at": timezone.now().isoformat()}
        event.save(update_fields=["outcome", "details"])
        return
    node = node or ref.node
    object_type = object_type or ref.object_type
    vmid = vmid or ref.vmid
    moves = moves if moves is not None else details.get("moves", [])
    timeout_seconds = int(
        timeout_seconds
        or details.get("task_timeout_seconds")
        or settings.SCHEDULED_ACTION_TIMEOUT_SECONDS
    )
    if not node or not isinstance(moves, list):
        event.outcome = "failed"
        event.details = {
            **details,
            "error": "The queued disk migration has incomplete target data.",
            "finished_at": timezone.now().isoformat(),
        }
        event.save(update_fields=["outcome", "details"])
        return
    kind = "qemu" if object_type == ProxmoxInventory.ObjectType.VM else "lxc"
    subpath = "move_disk" if kind == "qemu" else "move_volume"
    disk_param = "disk" if kind == "qemu" else "volume"

    moved: list[str] = []
    error: str | None = None
    for move in moves:
        disk_key, storage = move[0], move[1]
        if AuditEvent.objects.filter(pk=audit_event_id, outcome="cancelled").exists():
            return
        try:
            upid = client.post(
                f"nodes/{quote(node, safe='')}/{kind}/{vmid}/{subpath}",
                data={disk_param: disk_key, "storage": storage, "delete": 1},
            )
        except ProxmoxAPIError as exc:
            error = f"{disk_key}: {exc}"
            break
        if not (isinstance(upid, str) and upid.startswith("UPID:")):
            error = f"{disk_key}: unexpected Proxmox response"
            break
        details["proxmox_task_upid"] = upid
        details["proxmox_task_node"] = node
        details["current_disk"] = disk_key
        event.details = details
        event.save(update_fields=["details"])
        try:
            result = client.wait_for_task(node=node, upid=upid, timeout_seconds=timeout_seconds)
        except (ProxmoxTaskTimeout, ProxmoxAPIError) as exc:
            error = f"{disk_key}: {exc}"
            break
        if not result.success:
            error = f"{disk_key}: exitstatus {result.exitstatus or result.status or 'unknown'}"
            break
        moved.append(disk_key)

    if AuditEvent.objects.filter(pk=audit_event_id, outcome="cancelled").exists():
        return
    details["moved_disks"] = moved
    details.pop("proxmox_task_upid", None)
    details.pop("current_disk", None)
    if error:
        event.outcome = "failed"
        details["error"] = f"Moved {', '.join(moved) or 'none'}; then failed on {error}"
    else:
        event.outcome = "success"
    details["finished_at"] = timezone.now().isoformat()
    event.details = details
    event.save(update_fields=["outcome", "details"])
    clear_live_guest_caches(cluster=cluster)


def restore_guest_backup_task(
    audit_event_id: int,
    endpoint_url: str = "",
    node: str = "",
    object_type: str = "",
    vmid: int | None = None,
    archive: str = "",
    storage: str = "",
    overwrite: bool | None = None,
    shutdown_first: bool | None = None,
    start_after: bool | None = None,
    timeout_seconds: int | None = None,
) -> None:
    """Restore a vzdump archive, optionally replacing an existing guest.

    A replace is deliberately a staged worker operation: request a normal guest
    shutdown, wait for it, restore, then optionally start the restored guest.
    The current UPID is kept on the audit row throughout so Recent Tasks can
    cancel the Proxmox operation that is actually in flight.  We never turn a
    failed graceful shutdown into a hard stop.
    """
    event = AuditEvent.objects.filter(pk=audit_event_id).first()
    if event is None:
        return
    details = dict(event.details) if isinstance(event.details, dict) else {}
    try:
        client, ref, cluster = _durable_or_legacy_guest_operation(
            event,
            endpoint_url=endpoint_url,
            node=node,
            object_type=object_type,
            vmid=vmid,
        )
    except DurableGuestOperationError as exc:
        event.outcome = "failed"
        event.details = {**details, "error": str(exc), "finished_at": timezone.now().isoformat()}
        event.save(update_fields=["outcome", "details"])
        return
    endpoint_url = endpoint_url or str(details.get("proxmox_endpoint") or getattr(client, "endpoint", ""))
    node = node or ref.node
    object_type = object_type or ref.object_type
    vmid = vmid or ref.vmid
    archive = archive or str(details.get("archive") or "")
    storage = storage or str(details.get("target_storage") or "")
    overwrite = bool(details.get("overwrite")) if overwrite is None else overwrite
    shutdown_first = bool(details.get("shutdown_first")) if shutdown_first is None else shutdown_first
    start_after = bool(details.get("start_after")) if start_after is None else start_after
    timeout_seconds = int(
        timeout_seconds
        or details.get("task_timeout_seconds")
        or settings.BACKUP_TASK_TIMEOUT_SECONDS
    )
    if not node or not archive or not storage:
        event.outcome = "failed"
        event.details = {
            **details,
            "error": "The queued restore has incomplete target data.",
            "finished_at": timezone.now().isoformat(),
        }
        event.save(update_fields=["outcome", "details"])
        return
    kind = "qemu" if object_type == ProxmoxInventory.ObjectType.VM else "lxc"

    def cancelled() -> bool:
        return AuditEvent.objects.filter(pk=audit_event_id, outcome="cancelled").exists()

    def run_step(stage: str, path: str, data: dict) -> str | None:
        if cancelled():
            return "cancelled"
        try:
            upid = client.post(path, data=data)
        except ProxmoxAPIError as exc:
            return str(exc)
        if not (isinstance(upid, str) and upid.startswith("UPID:")):
            return f"unexpected Proxmox response during {stage}"
        details.update(
            {
                "stage": stage,
                "proxmox_task_upid": upid,
                "proxmox_task_node": node,
                "proxmox_endpoint": endpoint_url,
            }
        )
        event.details = details
        event.save(update_fields=["details"])
        try:
            result = client.wait_for_task(node=node, upid=upid, timeout_seconds=timeout_seconds)
        except (ProxmoxTaskTimeout, ProxmoxAPIError) as exc:
            return str(exc)
        if not result.success:
            return f"Proxmox task exitstatus: {result.exitstatus or result.status or 'unknown'}"
        details.setdefault("completed_stages", []).append(stage)
        return None

    error: str | None = None
    if overwrite:
        try:
            current = client.guest_current(node=node, object_type=object_type, vmid=vmid)
            current_status = str((current or {}).get("status") or "").lower()
        except ProxmoxAPIError as exc:
            current_status = ""
            error = f"Could not confirm the existing guest's power state: {exc}. Restore was not started."
        if error is None and not current_status:
            error = "Could not confirm the existing guest's power state. Restore was not started."
        if error is None and current_status != "stopped":
            error = run_step("shutdown existing guest", f"nodes/{quote(node, safe='')}/{kind}/{vmid}/status/shutdown", {})
            if error == "cancelled":
                return
            if error:
                error = f"Could not shut down the existing guest cleanly: {error}. Restore was not started."
            else:
                try:
                    stopped_status = str(
                        (client.guest_current(node=node, object_type=object_type, vmid=vmid) or {}).get("status") or ""
                    ).lower()
                except ProxmoxAPIError as exc:
                    stopped_status = ""
                    error = f"Could not verify shutdown completion: {exc}. Restore was not started."
                if error is None and stopped_status != "stopped":
                    error = (
                        f"The existing guest still reports power state '{stopped_status or 'unknown'}' after shutdown. "
                        "Restore was not started."
                    )

    if error is None:
        restore_data: dict[str, object] = {"vmid": vmid, "storage": storage}
        restore_path = f"nodes/{quote(node, safe='')}/{kind}"
        if kind == "qemu":
            restore_data["archive"] = archive
        else:
            restore_data.update({"ostemplate": archive, "restore": 1})
        if overwrite:
            restore_data["force"] = 1
        error = run_step("restore archive", restore_path, restore_data)
        if error == "cancelled":
            return

    if error is None and start_after:
        error = run_step("start restored guest", f"nodes/{quote(node, safe='')}/{kind}/{vmid}/status/start", {})
        if error == "cancelled":
            return

    if cancelled():
        return
    details.pop("proxmox_task_upid", None)
    details["finished_at"] = timezone.now().isoformat()
    if error:
        event.outcome = "failed"
        details["error"] = error
    else:
        event.outcome = "success"
        details["stage"] = "completed"
    event.details = details
    event.save(update_fields=["outcome", "details"])
    clear_live_guest_caches(cluster=cluster)


def register_import_vm_task(
    audit_event_id: int,
    node: str = "",
    params: dict | None = None,
    source_storage_id: str = "",
    source_path: str = "",
    source_volid: str = "",
) -> None:
    """Worker task: import a disk image into a new VM and record the outcome.

    A browsable source is staged then imported; a ready volid (e.g. a local
    import-content image) is imported directly. The result updates the audit row
    the view created with ``outcome="running"`` so it flows through Recent Tasks.
    """
    from .services.vm_register import VmRegisterError, import_disk_as_vm, import_volid_as_vm

    event = AuditEvent.objects.filter(pk=audit_event_id).first()
    details = dict(event.details) if event and isinstance(event.details, dict) else {}
    if event is None:
        return
    try:
        _client, ref, cluster = client_for_audit_event(event)
    except DurableGuestOperationError as exc:
        event.outcome = "failed"
        event.details = {**details, "error": str(exc), "finished_at": timezone.now().isoformat()}
        event.save(update_fields=["outcome", "details"])
        return
    node = node or ref.node
    params = params if isinstance(params, dict) else details.get("params", {})
    source_storage_id = source_storage_id or str(details.get("source_storage_id") or "")
    source_path = source_path or str(details.get("source_path") or "")
    source_volid = source_volid or str(details.get("source_volid") or "")
    if not node or not isinstance(params, dict):
        event.outcome = "failed"
        event.details = {
            **details,
            "error": "The queued VM import has incomplete target data.",
            "finished_at": timezone.now().isoformat(),
        }
        event.save(update_fields=["outcome", "details"])
        return
    upid = ""
    error: str | None = None
    try:
        if source_volid:
            upid, error = import_volid_as_vm(
                node,
                params,
                source_volid=source_volid,
                cluster=cluster,
            )
        else:
            storage = StorageMount.objects.get(storage_id=source_storage_id, enabled=True)
            upid, error = import_disk_as_vm(
                node,
                params,
                source_storage=storage,
                source_path=source_path,
                cluster=cluster,
            )
    except StorageMount.DoesNotExist:
        error = "Source storage is no longer available."
    except VmRegisterError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - surface any failure into the audit row
        error = f"{type(exc).__name__}: {exc}"

    if event is not None:
        if upid:
            details["proxmox_task_upid"] = upid
            details["proxmox_task_node"] = node
        if error:
            event.outcome = "failed"
            details["error"] = error
        else:
            event.outcome = "success"
        details["finished_at"] = timezone.now().isoformat()
        event.details = details
        event.save(update_fields=["outcome", "details"])
    if not error:
        # Refresh the target storage's inventory so the freshly created
        # images/<vmid>/ folder and disk show up in the browser immediately.
        _refresh_import_target_inventory(
            str(params.get("target_storage", "")),
            str(params.get("vmid", "")),
            node=node,
            cluster=cluster,
        )
    clear_live_guest_caches(cluster=cluster)


def _refresh_import_target_inventory(
    target_storage_id: str,
    vmid: str,
    *,
    node: str = "",
    cluster=None,
) -> None:
    """Refresh an imported VM's disk rows using its current PVE config.

    A filesystem-only partial refresh against an older ScanRun otherwise sees the
    new disk before that run's stored Proxmox inventory knows about the VM. That
    briefly and incorrectly classifies the freshly imported disk as an orphan.
    """
    storage = StorageMount.objects.filter(storage_id=target_storage_id, enabled=True).first()
    if storage is None:
        return
    scan = (
        ScanRun.objects.filter(status=ScanRun.Status.COMPLETED)
        .exclude(queued_task_id="content-preflight")
        .filter(Q(target_storage=storage) | Q(target_storage__isnull=True))
        .order_by(
            F("filesystem_scan_at").desc(nulls_last=True),
            F("finished_at").desc(nulls_last=True),
            "-created_at",
        )
        .first()
    )
    if scan is None:
        return

    try:
        numeric_vmid = int(vmid)
    except (TypeError, ValueError):
        numeric_vmid = None

    if node and numeric_vmid is not None:
        # Bounded to one cluster: asking every endpoint whether it holds this
        # node/vmid is a cross-cluster search, and two clusters may each answer.
        clients = cluster_clients(cluster) if cluster is not None else _cluster_clients()
        for client in clients:
            try:
                config = client.guest_config(
                    node=node,
                    object_type=ProxmoxInventory.ObjectType.VM,
                    vmid=numeric_vmid,
                )
                current = client.guest_current(
                    node=node,
                    object_type=ProxmoxInventory.ObjectType.VM,
                    vmid=numeric_vmid,
                )
            except Exception:  # noqa: BLE001 - retain the existing best-effort refresh
                continue

            upsert_current_guest(
                node=node,
                object_type=ProxmoxInventory.ObjectType.VM,
                vmid=numeric_vmid,
                name=str(config.get("name") or ""),
                status=str(current.get("status") or ""),
                config=config,
                cluster=cluster,
            )
            break

    directories = ["images"] + ([f"images/{vmid}"] if vmid else [])
    for directory_path in directories:
        try:
            refresh_storage_directory(storage=storage, scan=scan, directory_path=directory_path)
        except Exception:  # noqa: BLE001 - best-effort inventory refresh
            pass


def enqueue_scheduled_scan() -> int | None:
    schedule_state = scan_schedule_state()
    active_scan = ScanRun.objects.filter(
        status__in=[
            ScanRun.Status.QUEUED,
            ScanRun.Status.RUNNING,
        ]
    ).order_by("-created_at").first()
    if active_scan:
        record_audit_event(
            username="system",
            action="scan.schedule.skipped",
            object_type="scan_run",
            object_id=str(active_scan.id),
            outcome="skipped",
            details={"reason": "A scan is already queued or running."},
        )
        return None

    scan = ScanRun.objects.create(progress_message="Queued from schedule")
    task_id = async_task("core.tasks.run_scan", scan.id, q_options={"cluster": BULK_QUEUE_NAME})
    scan.queued_task_id = task_id
    scan.save(update_fields=["queued_task_id", "updated_at"])

    record_audit_event(
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
            logger.warning(
                "Scheduled trash purge failed: item_id=%s error_type=%s",
                item.id,
                exc.__class__.__name__,
                exc_info=True,
            )
            error = (
                "Invalid storage path."
                if str(exc) == "Invalid storage path."
                else "Trash item could not be purged."
            )
            errors.append({"item_id": item.id, "error": error})
            continue
        purged += 1

    record_audit_event(
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

    record_audit_event(
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


def prune_expired_console_sessions() -> dict[str, int]:
    return prune_console_sessions()


def record_storage_space_snapshots(retention_days: int = SPACE_SNAPSHOT_RETENTION_DAYS) -> int:
    recorded_at = timezone.now()
    storages = list(StorageMount.objects.filter(enabled=True).order_by("display_name"))
    created = _record_space_snapshots(None, storages, recorded_at)
    created += _record_local_space_snapshots(recorded_at)
    cutoff = recorded_at - timedelta(days=retention_days)
    StorageSpaceSnapshot.objects.filter(recorded_at__lt=cutoff).delete()
    return created


def _snapshot_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _record_local_space_snapshots(recorded_at: datetime) -> int:
    """Record capacity for local (non-shared) storages via the Proxmox API, so
    the local-storage Monitor tab gets the same 2x/day time series as the
    mounted ones. Cheap: a couple of calls per node, deduped across endpoints."""
    created = 0
    seen: set[tuple[str, str]] = set()
    truthy = {"1", "true", "yes", "on"}
    # `nodes` is cluster-wide, so one answering member covers the cluster and the
    # per-node reads ride on the endpoint that proved reachable. The old loop asked
    # every endpoint and deduped the overlap by hand.
    client = _first_cluster_client()
    if client is not None:
        try:
            nodes = client.get("nodes")
        except ProxmoxAPIError:
            nodes = []
        if not isinstance(nodes, list):
            nodes = []
        for node_entry in nodes:
            node = str((node_entry or {}).get("node") or "")
            if not node:
                continue
            try:
                storages = client.get(f"nodes/{quote(node, safe='')}/storage")
            except ProxmoxAPIError:
                continue
            for entry in storages or []:
                if not isinstance(entry, dict):
                    continue
                sid = str(entry.get("storage") or "")
                if not sid or str(entry.get("shared") or "0").lower() in truthy:
                    continue
                key = (node, sid)
                if key in seen:
                    continue
                seen.add(key)
                total = _snapshot_int(entry.get("total"))
                if total is None:
                    continue
                avail = _snapshot_int(entry.get("avail"))
                used = _snapshot_int(entry.get("used"))
                if used is None and avail is not None:
                    used = total - avail
                StorageSpaceSnapshot.objects.create(
                    storage=None,
                    node=node,
                    api_storage_id=sid,
                    scan_run=None,
                    recorded_at=recorded_at,
                    total_bytes=total,
                    available_bytes=avail if avail is not None else 0,
                    used_bytes=used if used is not None else 0,
                )
                created += 1
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
        record_audit_event(
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
        logger.exception("Uploaded file metadata normalization failed")
        record_audit_event(
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
                "message": "Uploaded file metadata could not be normalized.",
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
        _audit_scan_terminal(scan, "scan.failed", "failed")
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
        record_audit_event(
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
        logger.exception("Storage file inflation failed")
        record_audit_event(
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
                "error": "Storage file inflation failed.",
                "error_type": exc.__class__.__name__,
            },
        )
        if not isinstance(exc, StorageActionError):
            raise


def _verify_scan_cluster_identities(endpoints: list[ProxmoxEndpoint]) -> set[int]:
    """Verify each scanned cluster's CA identity, returning the quarantined ones.

    Trust-on-first-use: a cluster with no pinned CA yet is bound to the one its
    endpoint reports. A mismatch quarantines the cluster and its guests are skipped
    below, so a re-pointed or restored endpoint cannot merge a different cluster's
    inventory under an existing key. A cluster whose CA is simply unreachable is not
    quarantined — that is coverage degradation, handled as an ordinary scan gap.
    """
    from .services.cluster_identity import (
        ClusterIdentityError,
        ClusterIdentityMismatch,
        observe_cluster_identity,
        verify_or_bind_identity,
    )

    quarantined: set[int] = set()
    seen_clusters: set[int] = set()
    for endpoint in endpoints:
        cluster_id = endpoint.cluster_id
        if cluster_id is None or cluster_id in seen_clusters:
            continue
        seen_clusters.add(cluster_id)
        cluster = endpoint.cluster
        try:
            # Fails over across the cluster's endpoints: a single down node must not
            # block identity verification while another member answers.
            observed = observe_cluster_identity(cluster)
        except ClusterIdentityError as exc:
            logger.warning("Cluster identity unreadable: cluster=%s error=%s", cluster.key, exc)
            continue
        try:
            verify_or_bind_identity(cluster, observed)
        except ClusterIdentityMismatch as exc:
            logger.error("Cluster identity mismatch: cluster=%s error=%s", cluster.key, exc)
            quarantined.add(cluster_id)
    return quarantined


def _run_scan(scan: ScanRun) -> None:
    now = timezone.now()
    scan.status = ScanRun.Status.RUNNING
    scan.started_at = now
    scan.progress_message = "Resolving runtime configuration."
    scan.save(update_fields=["status", "started_at", "progress_message", "updated_at"])

    # Ensure the installation was bootstrapped, then read DB-owned configuration.
    # The environment is not reapplied here: after the durable marker exists the
    # database is the sole runtime authority for endpoints, storage and consumers.
    ensure_bootstrap()
    # A disabled cluster blocks refresh acquisition, so its endpoints are not
    # scanned even though the endpoint rows are enabled. exclude() keeps legacy
    # null-cluster endpoints and enabled-cluster endpoints; it drops only those whose
    # cluster is explicitly disabled.
    endpoints = list(
        ProxmoxEndpoint.objects.filter(enabled=True)
        .exclude(cluster__enabled=False)
        .order_by("name")
    )
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
    guest_observations: list[ScanGuestObservation] = []
    successful_endpoint_objects: list[ProxmoxEndpoint] = []
    referenced_volids: set[str] = set()
    template_vmids: set[int] = set()

    # Coverage is recorded per cluster: a node name alone is not evidence, because
    # two clusters may both have a `pve1`. An endpoint with no cluster contributes
    # to no cluster's coverage rather than to a global pool.
    cluster_attempts: dict[int, set[str]] = defaultdict(set)
    cluster_coverage: dict[int, set[str]] = defaultdict(set)
    cluster_errors: dict[int, dict[str, Any]] = defaultdict(dict)
    # Clusters whose reported CA no longer matches the pinned one. Their guests must
    # not be ingested: a re-pointed endpoint would merge another cluster's inventory.
    quarantined_cluster_ids = _verify_scan_cluster_identities(endpoints)

    for endpoint in endpoints:
        # Build through the resolver so the client carries this cluster's own
        # credential and TLS trust, not the global fallback.
        client = client_for_endpoint(endpoint)
        node_name = client.discover_node_name(endpoint.name)
        endpoint_attempts.append(node_name)
        if endpoint.cluster_id in quarantined_cluster_ids:
            # Record the attempt as a coverage gap without ingesting anything.
            cluster_attempts[endpoint.cluster_id].add(node_name)
            cluster_errors[endpoint.cluster_id][node_name] = ["cluster identity quarantined"]
            endpoint.last_health_status = "quarantined"
            endpoint.details = {"node": node_name, "quarantined": True}
            endpoint.save(update_fields=["last_health_status", "details", "updated_at"])
            continue
        result = client.inventory(node_name)

        if endpoint.cluster_id is not None:
            cluster_attempts[endpoint.cluster_id].add(node_name)

        if result.ok:
            endpoint_successes.append(node_name)
            successful_endpoint_objects.append(endpoint)
            if endpoint.cluster_id is not None:
                cluster_coverage[endpoint.cluster_id].add(node_name)
            endpoint.last_health_status = "ok"
            endpoint.last_successful_scan = timezone.now()
            endpoint.details = {"node": node_name}
        else:
            endpoint.last_health_status = "error"
            endpoint.details = {"node": node_name, "errors": result.errors}
            endpoint_errors[node_name] = result.errors
            if endpoint.cluster_id is not None:
                cluster_errors[endpoint.cluster_id][node_name] = result.errors
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
            if obj.object_type in {ProxmoxInventory.ObjectType.VM, ProxmoxInventory.ObjectType.CT}:
                guest_observations.append(ScanGuestObservation(endpoint=endpoint, guest=obj))

    ProxmoxInventory.objects.bulk_create(proxmox_objects, batch_size=500)

    inventory_at = timezone.now()
    reconcile_scan_guest_inventory(
        scan=scan,
        observations=guest_observations,
        attempted_endpoints=endpoints,
        successful_endpoints=successful_endpoint_objects,
        errors=endpoint_errors,
        observed_at=inventory_at,
    )
    _record_cluster_observations(scan, endpoints, cluster_attempts, cluster_coverage, cluster_errors)
    gate_status = _storage_gate_status(storages, cluster_coverage, inventory_at)

    # Retained as legacy global evidence for existing history readers; the
    # authoritative coverage is now the per-cluster observations above.
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
    _audit_scan_terminal(
        scan,
        "scan.completed",
        "success" if not warning_count else "warning",
        {"warnings": warning_count},
    )
    _prune_scan_history_after_success()


def _audit_scan_terminal(
    scan: ScanRun,
    action: str,
    outcome: str,
    details: dict[str, Any] | None = None,
) -> None:
    payload = {
        "target_label": scan.target_label
        or (scan.target_storage.display_name if scan.target_storage else "All storages"),
        "progress": scan.progress_message,
    }
    if scan.error_details:
        payload["error_details"] = scan.error_details
    if scan.summary_counts:
        payload["summary_counts"] = scan.summary_counts
    if details:
        payload.update(details)
    record_audit_event(
        username="system",
        action=action,
        object_type="scan_run",
        object_id=str(scan.id),
        outcome=outcome,
        details=payload,
    )


def _prune_scan_history_after_success() -> None:
    try:
        result = prune_scan_history()
    except Exception as exc:
        logger.exception("Failed to prune old scan history")
        record_audit_event(
            username="system",
            action="scan.retention.purge_failed",
            object_type="scan_retention",
            object_id="automatic-scan-retention",
            outcome="failed",
            details={
                "error": exc.__class__.__name__,
                "message": "Old scan history could not be pruned.",
            },
        )
        return

    if not result.deleted_anything:
        return

    record_audit_event(
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


def _record_cluster_observations(
    scan: ScanRun,
    endpoints: list[ProxmoxEndpoint],
    cluster_attempts: dict[int, set[str]],
    cluster_coverage: dict[int, set[str]],
    cluster_errors: dict[int, dict[str, Any]],
) -> None:
    """Store one coverage observation per cluster this scan attempted."""
    cluster_ids = {endpoint.cluster_id for endpoint in endpoints if endpoint.cluster_id is not None}
    for cluster_id in cluster_ids:
        ScanClusterObservation.objects.update_or_create(
            scan_run=scan,
            cluster_id=cluster_id,
            defaults={
                "nodes_attempted": sorted(cluster_attempts.get(cluster_id, set())),
                "nodes_succeeded": sorted(cluster_coverage.get(cluster_id, set())),
                "errors": cluster_errors.get(cluster_id, {}),
            },
        )


def _storage_gate_status(
    storages: list[StorageMount],
    cluster_coverage: dict[int, set[str]],
    inventory_at: datetime,
) -> dict[str, dict[str, Any]]:
    """Decide, per storage, whether inventory coverage justifies file operations.

    Coverage is matched on (cluster, node), never on a bare node name: this gate
    governs destructive file operations, so cluster A's `pve1` scanning successfully
    must not clear a gate for storage in cluster B. An unattributed consumer — one
    with no cluster — is treated as uncovered rather than matched by name, because
    the evidence cannot be shown to come from the right cluster.
    """
    result: dict[str, dict[str, Any]] = {}

    for storage in storages:
        expected_names = set(storage.expected_consumers or [])
        consumers = list(storage.consumer_statuses.select_related("cluster").all())

        expected_refs: list[str] = []
        covered_names: list[str] = []
        missing_names: list[str] = []
        missing_refs: list[str] = []

        for consumer in consumers:
            if consumer.expected_node_name not in expected_names:
                consumer.last_gate_status = "not_expected"
                consumer.save(update_fields=["last_gate_status", "updated_at"])
                continue

            ref = consumer.node_ref()
            covered = (
                ref is not None
                and consumer.expected_node_name in cluster_coverage.get(consumer.cluster_id, set())
            )
            label = ref.serialize() if ref is not None else consumer.expected_node_name
            expected_refs.append(label)

            if covered:
                covered_names.append(consumer.expected_node_name)
                consumer.last_successful_inventory_scan = inventory_at
                consumer.last_gate_status = "ok"
            else:
                missing_names.append(consumer.expected_node_name)
                missing_refs.append(label)
                consumer.last_gate_status = "blocked"
            consumer.save(
                update_fields=["last_gate_status", "last_successful_inventory_scan", "updated_at"]
            )

        # An expectation with no consumer row cannot have been covered by anything.
        unqualified = sorted(expected_names - {c.expected_node_name for c in consumers})
        missing_names.extend(unqualified)
        missing_refs.extend(unqualified)

        status = "ok" if not missing_names else "blocked"
        result[storage.storage_id] = {
            "ok": not missing_names,
            "status": status,
            # Display lists stay bare node names so the single-cluster UI is
            # unchanged; the *_refs lists carry the unambiguous evidence. Phase 4
            # owns cluster qualification of what is rendered.
            "expected_consumers": sorted(expected_names),
            "inventoried_consumers": sorted(covered_names),
            "missing_consumers": sorted(missing_names),
            "expected_node_refs": sorted(expected_refs),
            "missing_node_refs": sorted(missing_refs),
        }

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

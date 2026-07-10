from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import PurePosixPath

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.db.models import Q

from core.models import AuditEvent, ScanRun, ScheduledActionRun
from core.services.guests import guest_identity, guest_identity_from_scheduled_action


GUEST_TASK_NAMES = {
    "guest.power.start": "Power on",
    "guest.power.shutdown": "Shut down guest",
    "guest.power.reboot": "Restart guest",
    "guest.power.stop": "Power off",
    "guest.power.reset": "Reset guest",
    "guest.power.suspend": "Suspend",
    "guest.power.resume": "Resume",
    "guest.power.hibernate": "Hibernate",
    "guest.snapshot.create": "Create snapshot",
    "guest.snapshot.delete": "Delete snapshot",
    "guest.snapshot.delete_all": "Delete all snapshots",
    "guest.snapshot.rollback": "Rollback snapshot",
    "guest.template.convert": "Convert to template",
    "guest.template.revert": "Convert template to VM",
    "guest.clone.create": "Clone guest",
    "guest.tags.updated": "Update tags",
    "guest.agent.enable": "Enable guest agent",
    "guest.agent.disable": "Disable guest agent",
    "guest.destroy": "Destroy guest",
    "guest.config.updated": "Reconfigure",
    "guest.hardware.updated": "Reconfigure hardware",
    "guest.cloudinit.update": "Update Cloud-Init",
    "guest.create": "Create guest",
    "guest.register.adopt": "Register VM from disk",
    "guest.register.import": "Import VM from disk",
    "guest.firewall.options": "Firewall options",
    "guest.firewall.rule_add": "Add firewall rule",
    "guest.firewall.rule_delete": "Delete firewall rule",
    "guest.firewall.rule_toggle": "Toggle firewall rule",
    "guest.backup.run": "Backup",
    "guest.backup.delete": "Delete backup",
    "guest.replication.create": "Create replication",
    "guest.replication.delete": "Delete replication",
    "guest.console.opened": "Open console",
    "guest.console.closed": "Close console",
    "guest.console.failed": "Console failed",
}


DEFAULT_TASK_LIMIT = 5
RECENT_TASK_RETENTION_MINUTES = 60
FILE_TASK_ACTIONS = [
    "file.downloaded",
    "file.folder_created",
    "file.uploaded",
    "file.folder_uploaded",
    "file.upload_normalized",
    "file.upload_normalize_failed",
    "file.moved",
    "file.copied",
    "file.renamed",
    "file.trashed",
    "file.restored",
    "file.inflate_queued",
    "file.inflated",
    "file.inflate_failed",
]
INFLATE_QUEUED_ACTION = "file.inflate_queued"
INFLATE_TERMINAL_ACTIONS = {"file.inflated", "file.inflate_failed"}


@dataclass(frozen=True)
class RecentTaskPage:
    tasks: list[dict[str, object]]
    page: int
    limit: int
    total: int

    @property
    def has_previous(self) -> bool:
        return self.page > 0

    @property
    def has_next(self) -> bool:
        return (self.page + 1) * self.limit < self.total

    @property
    def start_index(self) -> int:
        if self.total == 0:
            return 0
        return self.page * self.limit + 1

    @property
    def end_index(self) -> int:
        return min((self.page + 1) * self.limit, self.total)


def recent_task_page(page: int = 0, limit: int = DEFAULT_TASK_LIMIT) -> RecentTaskPage:
    page = max(0, page)
    limit = max(1, limit)
    offset = page * limit
    scans = list(_visible_scan_tasks().order_by("-created_at"))
    scan_ids = [str(scan.id) for scan in scans]
    audit_events = AuditEvent.objects.filter(
        action="scan.queued",
        object_type="scan_run",
        object_id__in=scan_ids,
    ).select_related("user").order_by("-timestamp")
    initiators = {}
    for event in audit_events:
        initiators.setdefault(event.object_id, event.username or (event.user.get_username() if event.user else ""))

    tasks = [_scan_task(scan, initiators.get(str(scan.id), "system")) for scan in scans]
    tasks.extend(_file_task(event) for event in _visible_file_tasks())
    tasks.extend(_scheduled_action_task(run) for run in _visible_scheduled_action_tasks())
    tasks.extend(_guest_task(event) for event in _visible_guest_tasks())
    tasks.sort(key=_task_timeline_sort_at, reverse=True)
    total = len(tasks)
    return RecentTaskPage(tasks=tasks[offset : offset + limit], page=page, limit=limit, total=total)


def _task_timeline_sort_at(task: dict[str, object]):
    return task.get("started_at") or task.get("sort_at")


def _visible_scan_tasks():
    cutoff = timezone.now() - timedelta(minutes=RECENT_TASK_RETENTION_MINUTES)
    terminal_statuses = [
        ScanRun.Status.COMPLETED,
        ScanRun.Status.FAILED,
        ScanRun.Status.CANCELLED,
    ]
    return ScanRun.objects.exclude(Q(status__in=terminal_statuses) & Q(finished_at__lte=cutoff))


def _visible_file_tasks():
    cutoff = timezone.now() - timedelta(minutes=RECENT_TASK_RETENTION_MINUTES)
    events = list(
        AuditEvent.objects.filter(action__in=FILE_TASK_ACTIONS, timestamp__gte=cutoff)
        .select_related("user")
        .order_by("-timestamp")
    )
    terminal_events = [event for event in events if event.action in INFLATE_TERMINAL_ACTIONS]
    return [
        event
        for event in events
        if event.action != INFLATE_QUEUED_ACTION or not _has_later_inflate_terminal(event, terminal_events)
    ]


def _visible_scheduled_action_tasks():
    cutoff = timezone.now() - timedelta(minutes=RECENT_TASK_RETENTION_MINUTES)
    terminal_statuses = [
        ScheduledActionRun.Status.COMPLETED,
        ScheduledActionRun.Status.FAILED,
        ScheduledActionRun.Status.SKIPPED,
        ScheduledActionRun.Status.MISSED,
        ScheduledActionRun.Status.TIMEOUT,
        ScheduledActionRun.Status.STALE,
        ScheduledActionRun.Status.CANCELLED,
    ]
    return (
        ScheduledActionRun.objects.select_related("scheduled_action")
        .exclude(Q(status__in=terminal_statuses) & Q(finished_at__lte=cutoff))
        .order_by("-created_at")
    )


def _has_later_inflate_terminal(queued_event: AuditEvent, terminal_events: list[AuditEvent]) -> bool:
    queued_key = _inflate_event_key(queued_event)
    return any(
        _inflate_event_key(event) == queued_key and event.timestamp >= queued_event.timestamp
        for event in terminal_events
    )


def _inflate_event_key(event: AuditEvent) -> tuple[object, object, object]:
    details = event.details if isinstance(event.details, dict) else {}
    return (
        event.storage_id or details.get("storage_id"),
        event.path or details.get("path") or event.object_id,
        event.target_preallocation or details.get("target_preallocation"),
    )


def serialize_task_page(task_page: RecentTaskPage) -> dict[str, object]:
    return {
        "tasks": [serialize_task(task) for task in task_page.tasks],
        "page": task_page.page,
        "limit": task_page.limit,
        "total": task_page.total,
        "has_previous": task_page.has_previous,
        "has_next": task_page.has_next,
        "start_index": task_page.start_index,
        "end_index": task_page.end_index,
    }


def serialize_task(task: dict[str, object]) -> dict[str, object]:
    return {
        "id": str(task.get("id", "")),
        "kind": str(task.get("kind", "")),
        "action": str(task.get("action", "")),
        "name": str(task["name"]),
        "target": str(task["target"]),
        "target_guest": task.get("target_guest") or None,
        "status": str(task["status"]),
        "status_class": str(task["status_class"]),
        "details": str(task["details"]),
        "initiator": str(task["initiator"]),
        "queued_for": str(task["queued_for"]),
        "started_at": _datetime_label(task.get("started_at")),
        "started_at_ms": _datetime_ms(task.get("started_at")),
        "finished_at": _datetime_label(task.get("finished_at")),
        "finished_at_ms": _datetime_ms(task.get("finished_at")),
        "server": str(task["server"] or "-"),
        "storage_id": str(task.get("storage_id", "")),
        "path": str(task.get("path", "")),
        "path_parent": str(task.get("path_parent", "")),
        "cancelable": bool(task.get("cancelable")),
    }


def _scan_task(scan: ScanRun, initiator: str) -> dict[str, object]:
    status_label = scan.get_status_display()
    status_class = scan.status
    if scan.status == ScanRun.Status.COMPLETED and scan.error_details:
        status_label = "Completed with warnings"
        status_class = "warning"

    return {
        "id": f"scan:{scan.id}",
        "kind": "scan",
        "action": "scan",
        "name": "Storage scan",
        "target": scan.target_label or (scan.target_storage.display_name if scan.target_storage else "All storages"),
        "status": status_label,
        "status_class": status_class,
        "details": _scan_details(scan),
        "initiator": initiator,
        "queued_for": _duration_label(scan.created_at, scan.started_at),
        "started_at": scan.started_at,
        "finished_at": scan.finished_at,
        "server": ", ".join(scan.endpoints_succeeded or scan.endpoints_attempted or []),
        "sort_at": scan.created_at,
        "cancelable": False,
    }


def _visible_guest_tasks():
    cutoff = timezone.now() - timedelta(minutes=RECENT_TASK_RETENTION_MINUTES)
    return list(
        AuditEvent.objects.filter(action__startswith="guest.", timestamp__gte=cutoff)
        .select_related("user")
        .order_by("-timestamp")
    )


def _guest_task(event: AuditEvent) -> dict[str, object]:
    details = event.details if isinstance(event.details, dict) else {}
    identity = guest_identity(details.get("target_type"), details.get("vmid"), details.get("name") or "")
    status, status_class = _guest_task_status(event)
    finished_at = _guest_task_finished_at(event, details, status_class)
    extra = ""
    if event.action == "guest.clone.create" and details.get("new_vmid"):
        new_name = str(details.get("new_name") or "").strip()
        extra = f"new VMID {details['new_vmid']}" + (f" ({new_name})" if new_name else "")
    elif event.action == "guest.tags.updated":
        mode = str(details.get("mode") or "update").strip()
        tags = details.get("tags") if isinstance(details.get("tags"), list) else []
        extra = f"{mode}: {', '.join(tags)}" if tags else mode
    elif event.action == "guest.destroy":
        flags = []
        if details.get("purge"):
            flags.append("purge")
        if details.get("destroy_unreferenced_disks"):
            flags.append("destroy unreferenced disks")
        extra = ", ".join(flags)
    for key in ("snapshot", "storage", "volid", "job_id", "target"):
        if not extra and details.get(key):
            extra = str(details[key])
            break
    if not extra and details.get("fields"):
        extra = ", ".join(details["fields"]) if isinstance(details["fields"], list) else str(details["fields"])
    if not extra and details.get("error"):
        extra = str(details["error"])
    return {
        "id": f"guest:{event.id}",
        "kind": "guest",
        "action": event.action,
        "name": GUEST_TASK_NAMES.get(event.action, event.action),
        "target": identity.full_label_with_type,
        "target_guest": identity.as_dict(),
        "status": status,
        "status_class": status_class,
        "details": extra or "-",
        "initiator": event.username or (event.user.get_username() if event.user else "system"),
        "queued_for": "-",
        "started_at": event.timestamp,
        "finished_at": finished_at,
        "server": str(details.get("proxmox_task_node") or details.get("node") or "-"),
        "sort_at": finished_at or event.timestamp,
        "cancelable": status_class in {"queued", "running"}
        and bool(details.get("proxmox_task_upid") and details.get("proxmox_task_node")),
    }


def _guest_task_status(event: AuditEvent) -> tuple[str, str]:
    if event.outcome == "running":
        return "Running", "running"
    if event.outcome == "queued":
        return "Queued", "queued"
    if event.outcome == "failed":
        return "Failed", "failed"
    if event.outcome == "cancelled":
        return "Cancelled", "cancelled"
    return "Completed", "completed"


def _guest_task_finished_at(event: AuditEvent, details: dict, status_class: str):
    if status_class in {"running", "queued"}:
        return None
    finished_at = details.get("finished_at")
    if isinstance(finished_at, str) and finished_at:
        parsed = parse_datetime(finished_at)
        if parsed is not None:
            return parsed
    return event.timestamp


def _file_task(event: AuditEvent) -> dict[str, object]:
    details = event.details if isinstance(event.details, dict) else {}
    name = _file_task_name(event.action)
    target_preallocation = event.target_preallocation or details.get("target_preallocation")
    if event.action in {INFLATE_QUEUED_ACTION, *INFLATE_TERMINAL_ACTIONS} and target_preallocation:
        name = f"{name} ({target_preallocation})"
    storage_id = event.storage_id or str(details.get("storage_id") or "")
    path = event.path or str(details.get("path") or "")
    if event.action == INFLATE_QUEUED_ACTION:
        status = "Queued"
        status_class = "queued"
        finished_at = None
    elif event.outcome == "failed":
        status = "Failed"
        status_class = "failed"
        finished_at = event.timestamp
    else:
        status = "Completed"
        status_class = "completed"
        finished_at = event.timestamp
    return {
        "id": f"file:{event.id}",
        "kind": "file",
        "action": event.action,
        "name": name,
        "target": details.get("storage_name") or storage_id or "-",
        "status": status,
        "status_class": status_class,
        "details": path or event.object_id or "-",
        "initiator": event.username or (event.user.get_username() if event.user else "system"),
        "queued_for": "-",
        "started_at": event.timestamp,
        "finished_at": finished_at,
        "server": storage_id or "-",
        "sort_at": event.timestamp,
        "storage_id": storage_id,
        "path": path,
        "path_parent": _parent_path(path),
        "cancelable": False,
    }


def _file_task_name(action: str) -> str:
    return {
        "file.downloaded": "Download file",
        "file.folder_created": "Create folder",
        "file.uploaded": "Upload file",
        "file.folder_uploaded": "Upload folder",
        "file.upload_normalized": "Normalize upload",
        "file.upload_normalize_failed": "Normalize upload",
        "file.moved": "Move file",
        "file.copied": "Copy file",
        "file.renamed": "Rename file",
        "file.trashed": "Move file to trash",
        "file.restored": "Restore file",
        "file.inflate_queued": "Inflate disk",
        "file.inflated": "Inflate disk",
        "file.inflate_failed": "Inflate disk",
    }.get(action, action)


def _scheduled_action_task(run: ScheduledActionRun) -> dict[str, object]:
    action = run.scheduled_action
    status, status_class = _scheduled_action_status(run)
    identity = guest_identity_from_scheduled_action(action)

    return {
        "id": f"scheduled_action:{run.id}",
        "kind": "scheduled_action",
        "action": action.action_type,
        "name": f"Scheduled {action.get_action_type_display().lower()}",
        "target": identity.full_label_with_type,
        "target_guest": identity.as_dict(),
        "status": status,
        "status_class": status_class,
        "details": run.error or _scheduled_action_details(run),
        "initiator": _scheduled_action_initiator(run),
        "queued_for": _duration_label(run.created_at, run.started_at),
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "server": _scheduled_action_node(run),
        "sort_at": run.finished_at or run.started_at or run.created_at,
        "cancelable": status_class in {"queued", "running"} and bool(run.proxmox_task_upid and run.proxmox_task_node),
    }


def _scheduled_action_node(run: ScheduledActionRun) -> str:
    preflight = run.preflight_snapshot if isinstance(run.preflight_snapshot, dict) else {}
    return run.proxmox_task_node or str(preflight.get("node") or "") or run.scheduled_action.target_node or "-"


def _scheduled_action_status(run: ScheduledActionRun) -> tuple[str, str]:
    if run.status == ScheduledActionRun.Status.QUEUED:
        return "Queued", "queued"
    if run.status in {
        ScheduledActionRun.Status.PREFLIGHT,
        ScheduledActionRun.Status.SUBMITTED,
        ScheduledActionRun.Status.POLLING,
    }:
        return "Running", "running"
    if run.status == ScheduledActionRun.Status.COMPLETED:
        if run.outcome == ScheduledActionRun.Outcome.SUCCESS_NOOP:
            return "Completed - no action needed", "completed"
        return "Completed", "completed"
    if run.status == ScheduledActionRun.Status.SKIPPED:
        return "Skipped", "skipped"
    if run.status == ScheduledActionRun.Status.MISSED:
        return "Missed", "warning"
    if run.status == ScheduledActionRun.Status.TIMEOUT:
        return "Timed out", "failed"
    if run.status == ScheduledActionRun.Status.STALE:
        return "Stale", "failed"
    if run.status == ScheduledActionRun.Status.CANCELLED:
        return "Cancelled", "cancelled"
    return "Failed", "failed"


def _scheduled_action_details(run: ScheduledActionRun) -> str:
    action = run.scheduled_action
    planned_for = _datetime_label(run.planned_for)
    upid = run.proxmox_task_upid
    if upid:
        return f"Planned for {planned_for}, UPID {upid}"
    return f"Planned for {planned_for}"


def _scheduled_action_initiator(run: ScheduledActionRun) -> str:
    if run.triggered_by:
        return run.triggered_by.get_username()
    if run.scheduled_action.created_by:
        return run.scheduled_action.created_by.get_username()
    return "system"


def _scan_details(scan: ScanRun) -> str:
    summary = scan.summary_counts or {}
    classifications = summary.get("classifications") or {}
    parts = []
    if "files" in summary:
        parts.append(f"{summary['files']} files")
    if "referenced" in classifications:
        parts.append(f"{classifications['referenced']} referenced")
    if "likely_orphan" in classifications:
        parts.append(f"{classifications['likely_orphan']} orphans")
    if scan.progress_message:
        parts.append(scan.progress_message)
    return ", ".join(str(part) for part in parts if part) or "-"


def _duration_label(start, end) -> str:
    if not start or not end:
        return "-"

    milliseconds = max(0, int((end - start).total_seconds() * 1000))
    if milliseconds < 1000:
        return f"{milliseconds} ms"

    seconds = milliseconds / 1000
    if seconds < 60:
        return f"{seconds:.1f} s"

    minutes = int(seconds // 60)
    remaining = int(seconds % 60)
    return f"{minutes}m {remaining}s"


def _datetime_label(value: object) -> str:
    if value is None:
        return "-"
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M:%S")


def _datetime_ms(value: object) -> int | None:
    if value is None:
        return None
    return int(timezone.localtime(value).timestamp() * 1000)


def _parent_path(path: str) -> str:
    if not path:
        return ""
    parent = PurePosixPath(path).parent.as_posix()
    return "" if parent == "." else parent

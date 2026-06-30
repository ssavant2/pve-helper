from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import PurePosixPath

from django.utils import timezone
from django.db.models import Q

from core.models import AuditEvent, ScanRun


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
    tasks.sort(key=lambda task: task["sort_at"], reverse=True)
    total = len(tasks)
    return RecentTaskPage(tasks=tasks[offset : offset + limit], page=page, limit=limit, total=total)


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
        "status": str(task["status"]),
        "status_class": str(task["status_class"]),
        "details": str(task["details"]),
        "initiator": str(task["initiator"]),
        "queued_for": str(task["queued_for"]),
        "started_at": _datetime_label(task.get("started_at")),
        "finished_at": _datetime_label(task.get("finished_at")),
        "finished_at_ms": _datetime_ms(task.get("finished_at")),
        "server": str(task["server"] or "-"),
        "storage_id": str(task.get("storage_id", "")),
        "path": str(task.get("path", "")),
        "path_parent": str(task.get("path_parent", "")),
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
    }


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
        "file.renamed": "Rename file",
        "file.trashed": "Move file to trash",
        "file.restored": "Restore file",
        "file.inflate_queued": "Inflate disk",
        "file.inflated": "Inflate disk",
        "file.inflate_failed": "Inflate disk",
    }.get(action, action)


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

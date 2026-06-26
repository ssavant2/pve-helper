from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone
from django.db.models import Q

from core.models import AuditEvent, ScanRun


DEFAULT_TASK_LIMIT = 5
RECENT_TASK_RETENTION_MINUTES = 60


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
    scans_queryset = _visible_scan_tasks()
    total = scans_queryset.count()
    scans = list(scans_queryset.order_by("-created_at")[offset : offset + limit])
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
    return RecentTaskPage(tasks=tasks, page=page, limit=limit, total=total)


def _visible_scan_tasks():
    cutoff = timezone.now() - timedelta(minutes=RECENT_TASK_RETENTION_MINUTES)
    terminal_statuses = [
        ScanRun.Status.COMPLETED,
        ScanRun.Status.FAILED,
        ScanRun.Status.CANCELLED,
    ]
    return ScanRun.objects.exclude(Q(status__in=terminal_statuses) & Q(finished_at__lte=cutoff))


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


def serialize_task(task: dict[str, object]) -> dict[str, str]:
    return {
        "name": str(task["name"]),
        "target": str(task["target"]),
        "status": str(task["status"]),
        "status_class": str(task["status_class"]),
        "details": str(task["details"]),
        "initiator": str(task["initiator"]),
        "queued_for": str(task["queued_for"]),
        "started_at": _datetime_label(task.get("started_at")),
        "finished_at": _datetime_label(task.get("finished_at")),
        "server": str(task["server"] or "-"),
    }


def _scan_task(scan: ScanRun, initiator: str) -> dict[str, object]:
    status_label = scan.get_status_display()
    status_class = scan.status
    if scan.status == ScanRun.Status.COMPLETED and scan.error_details:
        status_label = "Completed with warnings"
        status_class = "warning"

    return {
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
    }


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

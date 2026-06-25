from django.conf import settings

from .models import AuditEvent, ScanRun, StorageMount


def app_settings(_request):
    return {
        "app_base_url": settings.APP_BASE_URL,
        "app_require_login": settings.APP_REQUIRE_LOGIN,
        "app_nav_storages": StorageMount.objects.filter(enabled=True).order_by("display_name"),
        "app_recent_tasks": _recent_tasks(),
    }


def _recent_tasks() -> list[dict[str, str]]:
    scans = list(ScanRun.objects.order_by("-created_at")[:5])
    scan_ids = [str(scan.id) for scan in scans]
    audit_events = AuditEvent.objects.filter(
        action="scan.queued",
        object_type="scan_run",
        object_id__in=scan_ids,
    ).select_related("user").order_by("-timestamp")
    initiators = {}
    for event in audit_events:
        initiators.setdefault(event.object_id, event.username or (event.user.get_username() if event.user else ""))

    tasks = []
    for scan in scans:
        status_label = scan.get_status_display()
        status_class = scan.status
        if scan.status == ScanRun.Status.COMPLETED and scan.error_details:
            status_label = "Completed with warnings"
            status_class = "warning"

        tasks.append(
            {
                "name": "Storage scan",
                "target": "All storages",
                "status": status_label,
                "status_class": status_class,
                "details": _scan_details(scan),
                "initiator": initiators.get(str(scan.id), "system"),
                "queued_for": _duration_label(scan.created_at, scan.started_at),
                "started_at": scan.started_at,
                "finished_at": scan.finished_at,
                "server": ", ".join(scan.endpoints_succeeded or scan.endpoints_attempted or []),
            }
        )
    return tasks


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

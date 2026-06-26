from __future__ import annotations

from pathlib import PurePosixPath

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django_q.tasks import async_task

from .models import AuditEvent, FileInventory, ScanRun, StorageMount
from .services.recent_tasks import recent_task_page, serialize_task_page
from .services.scan_schedule import scan_schedule_state, update_scan_schedule


def app_login_required(view_func):
    if not settings.APP_REQUIRE_LOGIN:
        return view_func
    return login_required(view_func)


def navigation_context(active: str, **extra: str) -> dict[str, str]:
    return {"active_nav": active, **extra}


@app_login_required
def dashboard(request):
    latest_scan = ScanRun.objects.order_by("-created_at").first()
    result_scan = _latest_result_scan()
    storages = list(StorageMount.objects.filter(enabled=True).order_by("display_name"))
    classification_counts = _current_classification_counts(storages)
    context = {
        **navigation_context("dashboard"),
        "latest_scan": latest_scan,
        "result_scan": result_scan,
        "storage_count": StorageMount.objects.count(),
        "scan_count": ScanRun.objects.count(),
        "audit_count": AuditEvent.objects.count(),
        "classification_counts": classification_counts,
        "scan_schedule": scan_schedule_state(),
        "active_scan": _active_scan(),
    }
    return render(request, "core/dashboard.html", context)


@app_login_required
def datastores(request):
    result_scan = _latest_result_scan()
    storages = list(StorageMount.objects.order_by("display_name"))
    for storage in storages:
        storage_result_scan = _latest_storage_result_scan(storage)
        storage.latest_counts = _classification_counts(
            FileInventory.objects.filter(scan_run=storage_result_scan, storage=storage)
            if storage_result_scan
            else FileInventory.objects.none()
        )
        storage.latest_file_count = sum(storage.latest_counts.values())
        storage.latest_gate_status = (result_scan.storage_gate_status or {}).get(storage.storage_id, {}) if result_scan else {}

    context = {
        **navigation_context("datastores"),
        "latest_scan": result_scan,
        "storages": storages,
    }
    return render(request, "core/datastores.html", context)


@app_login_required
def storage_browser(request, storage_id: str):
    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    latest_scan = _latest_storage_result_scan(storage)
    current_path = _normalize_browser_path(request.GET.get("path", ""))
    parent_path = _parent_path(current_path)
    entries = []
    current_entry = None

    if latest_scan:
        if current_path:
            current_entry = FileInventory.objects.filter(
                scan_run=latest_scan,
                storage=storage,
                path=current_path,
                entry_type=FileInventory.EntryType.DIRECTORY,
            ).first()
            if current_entry is None:
                raise Http404("Directory not found in latest scan.")

        candidates = FileInventory.objects.filter(scan_run=latest_scan, storage=storage)
        if current_path:
            candidates = candidates.filter(path__startswith=f"{current_path}/")

        prefix = f"{current_path}/" if current_path else ""
        for entry in candidates:
            remainder = entry.path[len(prefix) :] if prefix else entry.path
            if not remainder or "/" in remainder:
                continue
            entry.name = remainder
            _decorate_browser_entry(entry)
            entries.append(entry)

    entries.sort(key=lambda item: (item.entry_type != FileInventory.EntryType.DIRECTORY, item.name.lower()))

    context = {
        **navigation_context("storage_browser", active_storage_id=storage.storage_id),
        "storage": storage,
        "latest_scan": latest_scan,
        "current_path": current_path,
        "parent_path": parent_path,
        "breadcrumbs": _browser_breadcrumbs(current_path),
        "entries": entries,
        "current_entry": current_entry,
        "active_scan": _active_scan(),
    }
    return render(request, "core/storage_browser.html", context)


@app_login_required
def orphan_finder(request):
    latest_scan = _latest_result_scan()
    files = _current_orphan_files()
    context = {
        **navigation_context("orphans"),
        "latest_scan": latest_scan,
        "files": files,
    }
    return render(request, "core/orphan_finder.html", context)


@app_login_required
def audit_log(request):
    context = {
        **navigation_context("audit"),
        "events": AuditEvent.objects.order_by("-timestamp")[:200],
    }
    return render(request, "core/audit_log.html", context)


@app_login_required
def recent_tasks(request):
    try:
        page = int(request.GET.get("page", "0"))
    except ValueError:
        page = 0

    return JsonResponse(serialize_task_page(recent_task_page(page=page)))


@app_login_required
def scan_status(request):
    active_scan = _active_scan()
    latest_scan = active_scan or ScanRun.objects.order_by("-created_at").first()
    return JsonResponse(
        {
            "active": active_scan is not None,
            "status": latest_scan.status if latest_scan else "",
            "status_label": latest_scan.get_status_display() if latest_scan else "",
            "button_label": _scan_button_label(active_scan),
            "progress": latest_scan.progress_message if latest_scan else "",
        }
    )


@require_POST
@app_login_required
def update_scan_schedule_view(request):
    enabled = request.POST.get("enabled") == "on"
    try:
        interval_minutes = int(request.POST.get("interval_minutes", "60"))
        state = update_scan_schedule(enabled=enabled, interval_minutes=interval_minutes)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("core:dashboard")

    AuditEvent.objects.create(
        user=request.user if request.user.is_authenticated else None,
        username=request.user.get_username() if request.user.is_authenticated else "",
        action="scan.schedule.updated",
        object_type="scan_schedule",
        object_id="automatic-storage-scan",
        outcome="success",
        details={
            "enabled": state.enabled,
            "interval_minutes": state.interval_minutes,
            "next_run": state.next_run.isoformat() if state.next_run else "",
        },
    )

    return redirect("core:dashboard")


@require_POST
@app_login_required
def start_scan(request):
    redirect_to = _safe_next_url(request)
    active_scan = _active_scan()
    if active_scan:
        AuditEvent.objects.create(
            user=request.user if request.user.is_authenticated else None,
            username=request.user.get_username() if request.user.is_authenticated else "",
            action="scan.manual.skipped",
            object_type="scan_run",
            object_id=str(active_scan.id),
            outcome="skipped",
            details={"reason": "A scan is already queued or running."},
        )
        return redirect(redirect_to)

    target_storage = _requested_scan_storage(request)
    scan = ScanRun.objects.create(
        progress_message="Queued from UI",
        target_storage=target_storage,
        target_label=target_storage.display_name if target_storage else "",
    )
    task_id = async_task("core.tasks.run_scan", scan.id)
    scan.queued_task_id = task_id
    scan.save(update_fields=["queued_task_id", "updated_at"])

    AuditEvent.objects.create(
        user=request.user if request.user.is_authenticated else None,
        username=request.user.get_username() if request.user.is_authenticated else "",
        action="scan.queued",
        object_type="scan_run",
        object_id=str(scan.id),
        outcome="success",
        details={
            "task_id": task_id,
            "target_storage": target_storage.storage_id if target_storage else "",
            "target_label": target_storage.display_name if target_storage else "All storages",
        },
    )
    return redirect(redirect_to)


def health_live(_request):
    return JsonResponse({"status": "ok", "service": "pve-helper"})


def health_ready(_request):
    checks = {"database": "unknown"}
    status = 200
    try:
        connection.ensure_connection()
        checks["database"] = "ok"
    except Exception as exc:  # pragma: no cover - defensive health endpoint
        checks["database"] = "error"
        checks["database_error"] = exc.__class__.__name__
        status = 503

    return JsonResponse({"status": "ok" if status == 200 else "error", "checks": checks}, status=status)


def _classification_counts(queryset) -> dict[str, int]:
    return {
        item["classification"]: item["count"]
        for item in queryset.values("classification").order_by().annotate(count=Count("id"))
    }


def _current_classification_counts(storages: list[StorageMount]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for storage in storages:
        scan = _latest_storage_result_scan(storage)
        if not scan:
            continue
        for classification, count in _classification_counts(
            FileInventory.objects.filter(scan_run=scan, storage=storage)
        ).items():
            totals[classification] = totals.get(classification, 0) + count
    return totals


def _current_orphan_files() -> list[FileInventory]:
    files = []
    for storage in StorageMount.objects.filter(enabled=True).order_by("display_name"):
        scan = _latest_storage_result_scan(storage)
        if not scan:
            continue
        files.extend(
            FileInventory.objects.select_related("storage", "scan_run")
            .filter(
                scan_run=scan,
                storage=storage,
                classification=FileInventory.Classification.LIKELY_ORPHAN,
            )
            .order_by("storage__display_name", "path")[:200]
        )
    return sorted(files, key=lambda item: (item.storage.display_name, item.path))[:200]


def _latest_result_scan() -> ScanRun | None:
    return (
        ScanRun.objects.filter(status=ScanRun.Status.COMPLETED)
        .exclude(storage_gate_status={})
        .order_by("-finished_at", "-created_at")
        .first()
    )


def _latest_storage_result_scan(storage: StorageMount) -> ScanRun | None:
    return (
        ScanRun.objects.filter(status=ScanRun.Status.COMPLETED)
        .filter(Q(target_storage=storage) | Q(target_storage__isnull=True))
        .order_by("-filesystem_scan_at", "-finished_at", "-created_at")
        .first()
    )


def _active_scan() -> ScanRun | None:
    return (
        ScanRun.objects.filter(status__in=[ScanRun.Status.QUEUED, ScanRun.Status.RUNNING])
        .order_by("-created_at")
        .first()
    )


def _scan_button_label(active_scan: ScanRun | None) -> str:
    if not active_scan:
        return "Start scan"
    if active_scan.status == ScanRun.Status.QUEUED:
        return "Scan queued"
    return "Scanning"


def _requested_scan_storage(request) -> StorageMount | None:
    storage_id = request.POST.get("storage_id", "").strip()
    if not storage_id:
        return None
    return get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)


def _safe_next_url(request) -> str:
    next_url = request.POST.get("next", "").strip()
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return next_url
    return reverse("core:dashboard")


def _normalize_browser_path(raw_path: str) -> str:
    path = (raw_path or "").strip().strip("/")
    if not path:
        return ""

    parts = PurePosixPath(path).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise Http404("Invalid storage path.")
    return PurePosixPath(*parts).as_posix()


def _parent_path(path: str) -> str:
    if not path or "/" not in path:
        return ""
    return path.rsplit("/", 1)[0]


def _browser_breadcrumbs(path: str) -> list[dict[str, str]]:
    breadcrumbs = [{"label": "Root", "path": ""}]
    if not path:
        return breadcrumbs

    current = []
    for part in path.split("/"):
        current.append(part)
        breadcrumbs.append({"label": part, "path": "/".join(current)})
    return breadcrumbs


def _decorate_browser_entry(entry: FileInventory) -> None:
    entry.classification_label = _classification_label(entry)
    entry.classification_class = _classification_class(entry)
    entry.category_label = _content_category_label(entry.content_category, entry.path)


def _classification_label(entry: FileInventory) -> str:
    if entry.entry_type != FileInventory.EntryType.FILE and entry.classification == FileInventory.Classification.UNKNOWN:
        return "Not classified"
    return entry.get_classification_display()


def _classification_class(entry: FileInventory) -> str:
    if entry.entry_type != FileInventory.EntryType.FILE and entry.classification == FileInventory.Classification.UNKNOWN:
        return "not_classified"
    return entry.classification


def _content_category_label(category: str, path: str) -> str:
    if category == "unknown":
        if path == "images":
            return "VM images"
        if path.startswith("images/"):
            return "VM image directory"
        if path == "template":
            return "Templates"

    labels = {
        "backup": "Backups",
        "base_image": "Base image",
        "ct_private": "CT private data",
        "ct_template": "CT templates",
        "iso": "ISO images",
        "snippet": "Snippets",
        "template_directory": "Templates",
        "trash": "Trash",
        "vm_disk": "VM disk",
        "vm_image_directory": "VM image directory",
        "vm_images": "VM images",
    }
    return labels.get(category, "Other / unknown")

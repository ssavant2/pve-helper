from __future__ import annotations

from pathlib import PurePosixPath

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.models import Count
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.shortcuts import render
from django.views.decorators.http import require_POST
from django_q.tasks import async_task

from .models import AuditEvent, FileInventory, ScanRun, StorageMount


def app_login_required(view_func):
    if not settings.APP_REQUIRE_LOGIN:
        return view_func
    return login_required(view_func)


def navigation_context(active: str, **extra: str) -> dict[str, str]:
    return {"active_nav": active, **extra}


@app_login_required
def dashboard(request):
    latest_scan = ScanRun.objects.order_by("-created_at").first()
    latest_files = FileInventory.objects.filter(scan_run=latest_scan) if latest_scan else FileInventory.objects.none()
    classification_counts = _classification_counts(latest_files)
    context = {
        **navigation_context("dashboard"),
        "latest_scan": latest_scan,
        "storage_count": StorageMount.objects.count(),
        "scan_count": ScanRun.objects.count(),
        "audit_count": AuditEvent.objects.count(),
        "classification_counts": classification_counts,
    }
    return render(request, "core/dashboard.html", context)


@app_login_required
def datastores(request):
    latest_scan = ScanRun.objects.order_by("-created_at").first()
    storages = list(StorageMount.objects.order_by("display_name"))
    if latest_scan:
        counts = (
            FileInventory.objects.filter(scan_run=latest_scan)
            .values("storage_id", "classification")
            .order_by()
            .annotate(count=Count("id"))
        )
        by_storage: dict[int, dict[str, int]] = {}
        for row in counts:
            by_storage.setdefault(row["storage_id"], {})[row["classification"]] = row["count"]
        for storage in storages:
            storage.latest_counts = by_storage.get(storage.id, {})
            storage.latest_file_count = sum(storage.latest_counts.values())
            storage.latest_gate_status = (latest_scan.storage_gate_status or {}).get(storage.storage_id, {})
    else:
        for storage in storages:
            storage.latest_counts = {}
            storage.latest_file_count = 0
            storage.latest_gate_status = {}

    context = {
        **navigation_context("datastores"),
        "latest_scan": latest_scan,
        "storages": storages,
    }
    return render(request, "core/datastores.html", context)


@app_login_required
def storage_browser(request, storage_id: str):
    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    latest_scan = ScanRun.objects.order_by("-created_at").first()
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
    }
    return render(request, "core/storage_browser.html", context)


@app_login_required
def orphan_finder(request):
    latest_scan = ScanRun.objects.order_by("-created_at").first()
    files = (
        FileInventory.objects.select_related("storage", "scan_run")
        .filter(scan_run=latest_scan, classification=FileInventory.Classification.LIKELY_ORPHAN)
        .order_by("storage__display_name", "path")[:200]
        if latest_scan
        else FileInventory.objects.none()
    )
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


@require_POST
@app_login_required
def start_scan(request):
    scan = ScanRun.objects.create(progress_message="Queued from UI")
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
        details={"task_id": task_id},
    )
    messages.success(request, f"Scan {scan.id} queued.")
    return redirect("core:dashboard")


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

from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.models import Count
from django.shortcuts import redirect
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST
from django_q.tasks import async_task

from .models import AuditEvent, FileInventory, ScanRun, StorageMount


def app_login_required(view_func):
    if not settings.APP_REQUIRE_LOGIN:
        return view_func
    return login_required(view_func)


def navigation_context(active: str) -> dict[str, str]:
    return {"active_nav": active}


@app_login_required
def dashboard(request):
    latest_scan = ScanRun.objects.order_by("-created_at").first()
    classification_counts = {
        item["classification"]: item["count"]
        for item in FileInventory.objects.values("classification").order_by().annotate(count=Count("id"))
    }
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
    context = {
        **navigation_context("datastores"),
        "storages": StorageMount.objects.order_by("display_name"),
    }
    return render(request, "core/datastores.html", context)


@app_login_required
def orphan_finder(request):
    files = (
        FileInventory.objects.select_related("storage", "scan_run")
        .filter(classification=FileInventory.Classification.LIKELY_ORPHAN)
        .order_by("storage__display_name", "path")[:200]
    )
    context = {
        **navigation_context("orphans"),
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

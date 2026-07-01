from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponse, HttpResponseForbidden, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone as tz
from django.utils.http import content_disposition_header, url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django_q.tasks import async_task

from .models import (
    AuditEvent,
    FileInventory,
    ProxmoxInventory,
    ScanRun,
    ScheduledAction,
    ScheduledActionRun,
    StorageMount,
    StorageSpaceSnapshot,
    TrashItem,
)
from .services.file_actions import FileActionRisk, file_action_risk
from .services.audit_retention_schedule import audit_retention_schedule_state, update_audit_retention_schedule
from .services.filesystem import storage_space_info
from .services.partial_scan import refresh_storage_directory
from .services.permissions import storage_permissions as get_permissions
from .services.proxmox import LIVE_GUEST_STATUS_CACHE_SECONDS, fetch_live_guest_status
from .services.recent_tasks import recent_task_page, serialize_task_page
from .services.scan_schedule import scan_schedule_state, update_scan_schedule
from .services.storage_actions import (
    INFLATE_PREALLOCATION_FULL,
    INFLATE_PREALLOCATION_METADATA,
    INFLATE_PREALLOCATION_MODES,
    MIN_INFLATE_ALLOCATED_PERCENT,
    StorageActionError,
    adopt_discovered_trash_items,
    cleanup_empty_app_trash_directories,
    create_storage_directory,
    full_inflate_already_recorded,
    is_nfs_silly_rename_path,
    validate_inflate_storage_file,
    move_storage_file,
    move_file_to_trash,
    purge_trash_item as purge_trash_item_action,
    rename_storage_file,
    restore_trash_item,
    upload_to_storage,
    upload_folder_to_storage,
)
from .services.storage_details import storage_details
from .services.storage_visibility import ignored_relative_paths_for_storage, is_ignored_storage_path
from .services.trash_schedule import trash_purge_schedule_state, update_trash_purge_schedule


SPACE_CHART_DAYS = 7
SPACE_CHART_BUCKET_HOURS = 12
SPACE_CHART_MAX_POINTS = 14
FILE_BROWSER_BATCH_SIZE = 200
AUDIT_PAGE_SIZE = 200


def app_login_required(view_func):
    if not settings.APP_REQUIRE_LOGIN:
        return view_func
    return login_required(view_func)


def navigation_context(active: str, **extra: str) -> dict[str, str]:
    return {"active_nav": active, **extra}


def _storage_tab_context(storage: StorageMount, latest_scan, active_tab: str) -> dict:
    return {
        **navigation_context("storage_browser", active_storage_id=storage.storage_id),
        "storage": storage,
        "latest_scan": latest_scan,
        "active_scan": _active_scan(),
        "active_storage_tab": active_tab,
    }


@app_login_required
def dashboard(request):
    latest_scan = ScanRun.objects.order_by("-created_at").first()
    result_scan = _latest_result_scan()
    storages = list(StorageMount.objects.filter(enabled=True).order_by("display_name"))
    _decorate_storages_with_scan_state(storages, result_scan)
    classification_counts = _current_classification_counts(storages)
    context = {
        **navigation_context("dashboard"),
        "latest_scan": latest_scan,
        "result_scan": result_scan,
        "storage_count": StorageMount.objects.count(),
        "scan_count": ScanRun.objects.count(),
        "audit_count": AuditEvent.objects.count(),
        "classification_counts": classification_counts,
        "storage_gate_rows": _storage_gate_rows(storages, result_scan),
        "scan_schedule": scan_schedule_state(),
        "trash_purge_schedule": _trash_purge_schedule_state(),
        "active_scan": _active_scan(),
    }
    return render(request, "core/dashboard.html", context)


@app_login_required
def datastores(request):
    result_scan = _latest_result_scan()
    storages = list(StorageMount.objects.order_by("display_name"))
    _decorate_storages_with_scan_state(storages, result_scan)

    context = {
        **navigation_context("datastores"),
        "latest_scan": result_scan,
        "storages": storages,
    }
    return render(request, "core/datastores.html", context)


@app_login_required
def scheduled_tasks(request):
    actions = list(
        ScheduledAction.objects.select_related("created_by")
        .order_by("-enabled", "next_run_at", "name")
    )
    latest_runs = list(
        ScheduledActionRun.objects.select_related("scheduled_action")
        .order_by("-created_at")[:50]
    )

    for action in actions:
        action.display_target = _scheduled_action_target_label(action)
        action.display_schedule = _scheduled_action_schedule_label(action)
        action.display_status_class = _scheduled_action_status_class(action.last_status)
        action.display_creator = action.created_by.get_username() if action.created_by else "system"

    for run in latest_runs:
        run.display_target = _scheduled_action_target_label(run.scheduled_action)
        run.display_status_class = _scheduled_run_status_class(run.status)
        run.display_outcome = run.get_outcome_display() if run.outcome else "-"

    context = {
        **navigation_context("scheduled_tasks"),
        "scheduled_actions": actions,
        "latest_runs": latest_runs,
        "scheduled_actions_enabled": settings.SCHEDULED_ACTIONS_ENABLED,
        "schedule_timezone": settings.TIME_ZONE,
        "run_retention_days": settings.SCHEDULED_ACTION_RUN_RETENTION_DAYS,
    }
    return render(request, "core/scheduled_tasks.html", context)


def _decorate_storages_with_scan_state(storages: list[StorageMount], result_scan: ScanRun | None) -> None:
    for storage in storages:
        storage_result_scan = _latest_storage_result_scan(storage)
        storage.latest_counts = _classification_counts(
            FileInventory.objects.filter(scan_run=storage_result_scan, storage=storage)
            if storage_result_scan
            else FileInventory.objects.none()
        )
        storage.latest_file_count = sum(storage.latest_counts.values())
        storage.latest_gate_status = (result_scan.storage_gate_status or {}).get(storage.storage_id, {}) if result_scan else {}
        storage.latest_scan = storage_result_scan
        storage.latest_scan_at = _scan_timestamp(storage_result_scan)
        storage.space_info = storage_space_info(storage.path)
        storage.storage_actions_enabled = settings.STORAGE_WRITE_ENABLED and storage.space_info.can_write
        storage.details = storage_details(storage, storage_result_scan, storage.space_info)


@app_login_required
def storage_browser(request, storage_id: str):
    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    _decorate_storage_with_space_info(storage)
    latest_scan = _latest_storage_result_scan(storage)
    current_path = _normalize_browser_path(request.GET.get("path", ""))
    parent_path = _parent_path(current_path)
    file_query = request.GET.get("q", "").strip()[:200]
    file_offset = max(0, _int_request_param(request, "file_offset", 0))
    file_partial = request.GET.get("file_partial") == "1"
    entries = []
    current_entry = None
    folder_tree = []

    if latest_scan:
        ignored_paths = ignored_relative_paths_for_storage(storage)
        if current_path:
            if is_ignored_storage_path(current_path, ignored_paths):
                raise Http404("Directory not found in latest scan.")
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
            if is_ignored_storage_path(entry.path, ignored_paths):
                continue
            remainder = entry.path[len(prefix) :] if prefix else entry.path
            if not remainder or "/" in remainder:
                continue
            entry.name = remainder
            _decorate_browser_entry(entry)
            entries.append(entry)
        folder_tree = _browser_folder_tree(latest_scan, storage, current_path, ignored_paths=ignored_paths)

    entries.sort(key=lambda item: (item.entry_type != FileInventory.EntryType.DIRECTORY, item.name.lower()))
    if file_query:
        query = file_query.lower()
        entries = [
            entry
            for entry in entries
            if query in " ".join(
                [
                    entry.name.lower(),
                    entry.path.lower(),
                    (entry.content_category or "").lower(),
                    (entry.classification or "").lower(),
                    getattr(entry, "classification_label", "").lower(),
                    getattr(entry, "category_label", "").lower(),
                ]
            )
        ]

    file_total = len(entries)
    entries = entries[file_offset:file_offset + FILE_BROWSER_BATCH_SIZE]
    file_next_offset = file_offset + FILE_BROWSER_BATCH_SIZE
    file_has_next = file_next_offset < file_total
    file_next_url = (
        _storage_browser_url(
            storage,
            current_path,
            q=file_query,
            file_offset=file_next_offset,
        )
        if file_has_next
        else ""
    )

    context = {
        **_storage_tab_context(storage, latest_scan, "files"),
        "current_path": current_path,
        "parent_path": parent_path,
        "breadcrumbs": _browser_breadcrumbs(current_path),
        "folder_tree": folder_tree,
        "entries": entries,
        "current_entry": current_entry,
        "file_query": file_query,
        "file_offset": file_offset,
        "file_batch_size": FILE_BROWSER_BATCH_SIZE,
        "file_total": file_total,
        "file_start": min(file_offset + 1, file_total),
        "file_end": min(file_offset + len(entries), file_total),
        "file_has_next": file_has_next,
        "file_next_url": file_next_url,
        "include_parent_row": current_path and file_offset == 0,
    }
    if file_partial:
        return JsonResponse(
            {
                "rows_html": render_to_string(
                    "core/partials/storage_file_rows.html",
                    {**context, "include_parent_row": False},
                    request=request,
                ),
                "has_next": file_has_next,
                "next_url": file_next_url,
                "total": file_total,
                "end": context["file_end"],
            }
        )
    return render(request, "core/storage_browser.html", context)


@app_login_required
def download_storage_file(request, storage_id: str):
    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    latest_scan = _latest_storage_result_scan(storage)
    if latest_scan is None:
        raise Http404("No storage inventory has been scanned yet.")

    requested_path = _normalize_browser_path(request.GET.get("path", ""))
    if not requested_path:
        raise Http404("No file path requested.")

    entry = get_object_or_404(
        FileInventory,
        scan_run=latest_scan,
        storage=storage,
        path=requested_path,
        entry_type=FileInventory.EntryType.FILE,
    )
    absolute_path = _resolve_storage_file(storage, entry.path)

    AuditEvent.objects.create(
        user=request.user if request.user.is_authenticated else None,
        username=request.user.get_username() if request.user.is_authenticated else "",
        source_ip=_client_ip(request),
        action="file.downloaded",
        object_type="file",
        object_id=f"{storage.storage_id}:{entry.path}",
        outcome="success",
        details={
            "storage_id": storage.storage_id,
            "storage_name": storage.display_name,
            "path": entry.path,
            "size_bytes": entry.size_bytes,
            "scan_run": latest_scan.id,
        },
    )

    return _download_response(request, storage, entry.path, absolute_path)


@require_POST
@app_login_required
def create_storage_folder(request, storage_id: str):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    current_path = _normalize_browser_path(request.POST.get("path", ""))
    redirect_to = _safe_next_url(request) or _storage_browser_url(storage, current_path)
    latest_scan = _latest_storage_result_scan(storage)
    if current_path:
        _storage_directory_or_404(storage, latest_scan, current_path)

    try:
        result = create_storage_directory(
            storage=storage,
            directory_path=current_path,
            folder_name=request.POST.get("folder_name", ""),
        )
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_to)

    _audit_file_action(
        request,
        action="file.folder_created",
        storage=storage,
        path=str(result["path"]),
        details={"directory_path": result["directory_path"]},
    )
    _refresh_latest_storage_directory(storage, str(result["directory_path"]))
    return redirect(redirect_to)


@require_POST
@app_login_required
def upload_storage_file(request, storage_id: str):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    current_path = _normalize_browser_path(request.POST.get("path", ""))
    redirect_to = _safe_next_url(request) or _storage_browser_url(storage, current_path)
    uploaded_file = request.FILES.get("file")
    if uploaded_file is None:
        return _upload_error_response(request, redirect_to, "No upload file selected.")

    latest_scan = _latest_storage_result_scan(storage)
    if current_path:
        _storage_directory_or_404(storage, latest_scan, current_path)

    try:
        result = upload_to_storage(
            storage=storage,
            directory_path=current_path,
            uploaded_file=uploaded_file,
        )
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        return _upload_error_response(request, redirect_to, str(exc))

    _audit_file_action(
        request,
        action="file.uploaded",
        storage=storage,
        path=str(result["path"]),
        details={"size_bytes": result["size_bytes"]},
    )
    _queue_upload_normalization(storage, [str(result["path"])], request.user)
    _refresh_latest_storage_directory(storage, current_path)
    return _upload_success_response(request, redirect_to)


@require_POST
@app_login_required
def upload_storage_folder(request, storage_id: str):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    current_path = _normalize_browser_path(request.POST.get("path", ""))
    redirect_to = _safe_next_url(request) or _storage_browser_url(storage, current_path)
    uploaded_files = request.FILES.getlist("files")
    relative_paths = request.POST.getlist("relative_path")
    if not uploaded_files:
        return _upload_error_response(request, redirect_to, "No upload files selected.")
    if not relative_paths:
        relative_paths = [uploaded_file.name for uploaded_file in uploaded_files]

    latest_scan = _latest_storage_result_scan(storage)
    if current_path:
        _storage_directory_or_404(storage, latest_scan, current_path)

    try:
        result = upload_folder_to_storage(
            storage=storage,
            directory_path=current_path,
            uploaded_files=uploaded_files,
            relative_paths=relative_paths,
        )
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        return _upload_error_response(request, redirect_to, str(exc))

    _audit_file_action(
        request,
        action="file.folder_uploaded",
        storage=storage,
        path=current_path or "/",
        details={
            "file_count": result["file_count"],
            "size_bytes": result["size_bytes"],
            "directory_path": result["directory_path"],
        },
    )
    _queue_upload_normalization(storage, [str(path) for path in result["paths"]], request.user)
    for directory_path in result["directory_paths"]:
        _refresh_latest_storage_directory(storage, str(directory_path))
    return _upload_success_response(request, redirect_to)


@app_login_required
def storage_trash(request, storage_id: str):
    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    _decorate_storage_with_space_info(storage)
    latest_scan = _latest_storage_result_scan(storage)
    if settings.STORAGE_WRITE_ENABLED and storage.storage_actions_enabled:
        try:
            cleanup_empty_app_trash_directories(storage=storage)
        except StorageActionError:
            pass
    if latest_scan:
        try:
            adopt_discovered_trash_items(storage=storage, scan=latest_scan)
        except StorageActionError:
            pass
    items = list(
        TrashItem.objects.filter(
            storage_id=storage.storage_id,
            restore_status=TrashItem.RestoreStatus.TRASHED,
        )
        .select_related("moved_by")
        .order_by("-moved_at", "-created_at")[:200]
    )
    items = [
        item
        for item in items
        if not is_nfs_silly_rename_path(item.original_path) and not is_nfs_silly_rename_path(item.trash_path)
    ]
    context = {
        **navigation_context("storage_browser", active_storage_id=storage.storage_id),
        "storage": storage,
        "items": items,
    }
    return render(request, "core/storage_trash.html", context)


@app_login_required
def storage_summary(request, storage_id: str):
    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    _decorate_storage_with_space_info(storage)
    latest_scan = _latest_storage_result_scan(storage)

    classification_counts = {}
    total_file_count = 0
    if latest_scan:
        classification_counts = _classification_counts(
            FileInventory.objects.filter(scan_run=latest_scan, storage=storage)
        )
        total_file_count = sum(classification_counts.values())

    gate_status = {}
    if latest_scan and latest_scan.storage_gate_status:
        gate_status = latest_scan.storage_gate_status.get(storage.storage_id, {})

    consumers = list(storage.consumer_statuses.order_by("expected_node_name"))

    context = {
        **_storage_tab_context(storage, latest_scan, "summary"),
        "classification_counts": classification_counts,
        "total_file_count": total_file_count,
        "gate_status": gate_status,
        "consumers": consumers,
    }
    return render(request, "core/storage_summary.html", context)


@app_login_required
def storage_monitor(request, storage_id: str):
    MONITOR_PAGE_SIZE = 10
    ACTIVITY_RETENTION_DAYS = 7

    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    _decorate_storage_with_space_info(storage)
    latest_scan = _latest_storage_result_scan(storage)

    scan_page = max(0, _int_request_param(request, "scan_page", 0))
    event_page = max(0, _int_request_param(request, "event_page", 0))

    activity_cutoff = tz.now() - timedelta(days=ACTIVITY_RETENTION_DAYS)
    all_scans = ScanRun.objects.filter(
        Q(target_storage=storage) | Q(target_storage__isnull=True),
        created_at__gte=activity_cutoff,
    ).order_by("-created_at")
    scan_total = all_scans.count()
    scan_start = scan_page * MONITOR_PAGE_SIZE
    scan_end = scan_start + MONITOR_PAGE_SIZE
    recent_scans = list(all_scans[scan_start:scan_end])

    all_events = AuditEvent.objects.filter(
        storage_id=storage.storage_id,
        timestamp__gte=activity_cutoff,
    ).order_by("-timestamp")
    event_total = all_events.count()
    event_start = event_page * MONITOR_PAGE_SIZE
    event_end = event_start + MONITOR_PAGE_SIZE
    recent_events = list(all_events[event_start:event_end])
    _decorate_audit_events(recent_events)

    space_chart_data = _storage_space_chart_data(storage, tz.now())

    context = {
        **_storage_tab_context(storage, latest_scan, "monitor"),
        "recent_scans": recent_scans,
        "scan_page": scan_page,
        "scan_total": scan_total,
        "scan_start": min(scan_start + 1, scan_total),
        "scan_end": min(scan_end, scan_total),
        "scan_has_prev": scan_page > 0,
        "scan_has_next": scan_end < scan_total,
        "recent_events": recent_events,
        "event_page": event_page,
        "event_total": event_total,
        "event_start": min(event_start + 1, event_total),
        "event_end": min(event_end, event_total),
        "event_has_prev": event_page > 0,
        "event_has_next": event_end < event_total,
        "space_chart_data_json": json.dumps(space_chart_data),
    }
    return render(request, "core/storage_monitor.html", context)


def _storage_space_chart_data(storage: StorageMount, now) -> list[dict[str, object]]:
    cutoff = now - timedelta(days=SPACE_CHART_DAYS)
    scheduled_history = list(
        StorageSpaceSnapshot.objects.filter(
            storage=storage,
            scan_run__isnull=True,
            recorded_at__gte=cutoff,
        ).order_by("recorded_at")
    )
    history = scheduled_history or list(
        StorageSpaceSnapshot.objects.filter(
            storage=storage,
            recorded_at__gte=cutoff,
        ).order_by("recorded_at")
    )

    bucket_seconds = SPACE_CHART_BUCKET_HOURS * 60 * 60
    buckets: dict[int, StorageSpaceSnapshot] = {}
    for snapshot in history:
        seconds_since_cutoff = max(0, int((snapshot.recorded_at - cutoff).total_seconds()))
        bucket = seconds_since_cutoff // bucket_seconds
        buckets[bucket] = snapshot

    snapshots = [buckets[bucket] for bucket in sorted(buckets)][-SPACE_CHART_MAX_POINTS:]
    return [
        {
            "timestamp": snapshot.recorded_at.isoformat(),
            "used_bytes": snapshot.used_bytes,
            "total_bytes": snapshot.total_bytes,
            "available_bytes": snapshot.available_bytes,
        }
        for snapshot in snapshots
    ]


@app_login_required
def storage_configure(request, storage_id: str):
    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    _decorate_storage_with_space_info(storage)
    latest_scan = _latest_storage_result_scan(storage)
    context = {
        **_storage_tab_context(storage, latest_scan, "configure"),
    }
    return render(request, "core/storage_configure.html", context)


@app_login_required
def storage_permissions_view(request, storage_id: str):
    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    _decorate_storage_with_space_info(storage)
    latest_scan = _latest_storage_result_scan(storage)

    perms = get_permissions(storage.path)

    context = {
        **_storage_tab_context(storage, latest_scan, "permissions"),
        "permissions": perms,
    }
    return render(request, "core/storage_permissions.html", context)


@app_login_required
def storage_hosts(request, storage_id: str):
    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    _decorate_storage_with_space_info(storage)
    latest_scan = _latest_storage_result_scan(storage)

    consumers = list(storage.consumer_statuses.order_by("expected_node_name"))

    proxmox_storage_entries = []
    if latest_scan:
        proxmox_storage_entries = list(
            ProxmoxInventory.objects.filter(
                scan_run=latest_scan,
                object_type=ProxmoxInventory.ObjectType.STORAGE,
                name=storage.storage_id,
            ).order_by("node")
        )

    context = {
        **_storage_tab_context(storage, latest_scan, "hosts"),
        "consumers": consumers,
        "proxmox_storage_entries": proxmox_storage_entries,
    }
    return render(request, "core/storage_hosts.html", context)


@app_login_required
def storage_vms(request, storage_id: str):
    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    _decorate_storage_with_space_info(storage)
    latest_scan = _latest_storage_result_scan(storage)

    guests = []
    if latest_scan:
        prefix = f"{storage.storage_id}:"
        for obj in ProxmoxInventory.objects.filter(
            scan_run=latest_scan,
            object_type__in=[
                ProxmoxInventory.ObjectType.VM,
                ProxmoxInventory.ObjectType.CT,
            ],
        ).order_by("object_type", "vmid"):
            matching_refs = [ref for ref in (obj.disk_references or []) if ref.startswith(prefix)]
            if matching_refs:
                obj.matching_disk_references = matching_refs
                guests.append(obj)

    if guests:
        live_status = fetch_live_guest_status()
        for guest in guests:
            key = (guest.object_type, guest.vmid)
            if key in live_status:
                guest.status = live_status[key]

    context = {
        **_storage_tab_context(storage, latest_scan, "vms"),
        "guests": guests,
        "inventory_scan_at": _scan_timestamp(latest_scan),
        "live_status_cache_seconds": LIVE_GUEST_STATUS_CACHE_SECONDS,
    }
    return render(request, "core/storage_vms.html", context)


@require_POST
@app_login_required
def trash_storage_file(request, storage_id: str):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    redirect_to = _safe_next_url(request)
    latest_scan = _latest_storage_result_scan(storage)
    entries = _selected_storage_file_entries(
        request,
        storage=storage,
        latest_scan=latest_scan,
        entry_types=[FileInventory.EntryType.FILE, FileInventory.EntryType.DIRECTORY],
    )

    try:
        _require_file_action_confirmations_for_entries(request, entries)
        results = [
            (entry, move_file_to_trash(storage=storage, entry=entry, user=request.user))
            for entry in entries
        ]
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_to)

    refresh_directories = set()
    pruned_paths = set()
    for entry, trash_item in results:
        _audit_file_action(
            request,
            action="file.trashed",
            storage=storage,
            path=entry.path,
            details={"trash_item": trash_item.id, "trash_path": trash_item.trash_path},
        )
        if entry.entry_type == FileInventory.EntryType.DIRECTORY:
            pruned_paths.add(entry.path)
        refresh_directories.add(_parent_path(entry.path))
    for path in pruned_paths:
        _prune_latest_storage_path(storage, path)
    for directory_path in refresh_directories:
        _refresh_latest_storage_directory(storage, directory_path)
    return redirect(redirect_to)


@require_POST
@app_login_required
def move_storage_file_view(request, storage_id: str):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    redirect_to = _safe_next_url(request)
    latest_scan = _latest_storage_result_scan(storage)
    entries = _selected_storage_file_entries(request, storage=storage, latest_scan=latest_scan)

    try:
        _require_file_action_confirmations_for_entries(request, entries)
        results = [
            move_storage_file(
                storage=storage,
                entry=entry,
                new_path=request.POST.get("new_path", ""),
            )
            for entry in entries
        ]
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_to)

    refresh_directories = set()
    for result in results:
        _audit_file_action(
            request,
            action="file.moved",
            storage=storage,
            path=str(result["new_path"]),
            details={"old_path": result["old_path"]},
        )
        refresh_directories.add(str(result["source_directory_path"]))
        refresh_directories.add(str(result["target_directory_path"]))
    for directory_path in refresh_directories:
        _refresh_latest_storage_directory(storage, directory_path)
    return redirect(redirect_to)


@require_POST
@app_login_required
def rename_storage_file_view(request, storage_id: str):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    redirect_to = _safe_next_url(request)
    latest_scan = _latest_storage_result_scan(storage)
    requested_path = _normalize_browser_path(request.POST.get("path", ""))
    entry = get_object_or_404(
        FileInventory,
        scan_run=latest_scan,
        storage=storage,
        path=requested_path,
        entry_type=FileInventory.EntryType.FILE,
    )
    risk = file_action_risk(entry)

    try:
        _require_file_action_confirmations(request, risk)
        result = rename_storage_file(
            storage=storage,
            entry=entry,
            new_name=request.POST.get("new_name", ""),
        )
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_to)

    _audit_file_action(
        request,
        action="file.renamed",
        storage=storage,
        path=str(result["new_path"]),
        details={"old_path": result["old_path"]},
    )
    _refresh_latest_storage_directory(storage, str(result["directory_path"]))
    return redirect(redirect_to)


@require_POST
@app_login_required
def inflate_storage_file_view(request, storage_id: str):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    redirect_to = _safe_next_url(request)
    target_preallocation = request.POST.get("target_preallocation") or INFLATE_PREALLOCATION_FULL
    if target_preallocation not in INFLATE_PREALLOCATION_MODES:
        messages.error(request, "Unknown inflate target.")
        return redirect(redirect_to)

    latest_scan = _latest_storage_result_scan(storage)
    requested_path = _normalize_browser_path(request.POST.get("path", ""))
    entry = get_object_or_404(
        FileInventory,
        scan_run=latest_scan,
        storage=storage,
        path=requested_path,
        entry_type=FileInventory.EntryType.FILE,
    )
    risk = file_action_risk(entry, block_running_guests=False)

    try:
        _require_file_action_confirmations(request, risk)
        validate_inflate_storage_file(
            storage=storage,
            entry=entry,
            target_preallocation=target_preallocation,
            validate_owner_locally=not settings.STORAGE_INFLATE_WORKER_PRESERVES_OWNER,
        )
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_to)

    task_id = async_task(
        "core.tasks.inflate_storage_file_task",
        storage.id,
        entry.id,
        request.user.get_username() if request.user.is_authenticated else "",
        target_preallocation,
    )
    _audit_file_action(
        request,
        action="file.inflate_queued",
        storage=storage,
        path=entry.path,
        details={"task_id": task_id, "target_preallocation": target_preallocation},
    )
    return redirect(redirect_to)


@require_POST
@app_login_required
def restore_storage_file(request, trash_item_id: int):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    item = get_object_or_404(TrashItem, pk=trash_item_id)
    redirect_to = _safe_next_url(request)
    try:
        result = restore_trash_item(item=item)
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_to)

    _audit_file_action(
        request,
        action="file.restored",
        storage=result["storage"],
        path=str(result["path"]),
        details={"trash_item": item.id},
    )
    _refresh_latest_storage_directory(result["storage"], _parent_path(str(result["path"])))
    if result.get("entry_type") == FileInventory.EntryType.DIRECTORY:
        _refresh_latest_storage_directory(result["storage"], str(result["path"]))
    _refresh_latest_storage_directory(result["storage"], _parent_path(str(result["trash_path"])))
    return redirect(redirect_to)


@require_POST
@app_login_required
def purge_trash_item(request, trash_item_id: int):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    item = get_object_or_404(TrashItem, pk=trash_item_id, restore_status=TrashItem.RestoreStatus.TRASHED)
    redirect_to = _safe_next_url(request)
    try:
        result = purge_trash_item_action(item=item)
    except StorageActionError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_to)

    _audit_file_action(
        request,
        action="file.purged",
        storage=result["storage"],
        path=str(result["path"]),
        details={"trash_item": item.id, "trash_path": result["trash_path"]},
    )
    _refresh_latest_storage_directory(result["storage"], _parent_path(str(result["trash_path"])))
    return redirect(redirect_to)


@app_login_required
def orphan_finder(request):
    latest_scan = _latest_result_scan()
    files = _current_orphan_files()
    _decorate_orphan_files_with_action_state(files)
    context = {
        **navigation_context("orphans"),
        "latest_scan": latest_scan,
        "files": files,
    }
    return render(request, "core/orphan_finder.html", context)


@app_login_required
def audit_log(request):
    try:
        audit_page = int(request.GET.get("page", "0"))
    except ValueError:
        audit_page = 0
    audit_page = max(0, audit_page)
    event_total = AuditEvent.objects.count()
    max_page = (event_total - 1) // AUDIT_PAGE_SIZE if event_total else 0
    audit_page = min(audit_page, max_page)
    event_offset = audit_page * AUDIT_PAGE_SIZE
    events = list(AuditEvent.objects.order_by("-timestamp")[event_offset:event_offset + AUDIT_PAGE_SIZE])
    _decorate_audit_events(events)
    context = {
        **navigation_context("audit"),
        "events": events,
        "audit_page": audit_page,
        "audit_has_prev": audit_page > 0,
        "audit_has_next": event_offset + len(events) < event_total,
        "audit_start": event_offset + 1 if event_total else 0,
        "audit_end": event_offset + len(events),
        "audit_total": event_total,
        "audit_retention_schedule": audit_retention_schedule_state(),
        "audit_filters": [
            {"key": "all", "label": "All"},
            {"key": "auth", "label": "Auth"},
            {"key": "clusters", "label": "Clusters"},
            {"key": "vms", "label": "VMs"},
            {"key": "storage", "label": "Storage"},
            {"key": "network", "label": "Network"},
            {"key": "system", "label": "System"},
        ],
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
def update_trash_purge_schedule_view(request):
    enabled = request.POST.get("enabled") == "on"
    try:
        max_age_days = int(request.POST.get("max_age_days", "30"))
        state = update_trash_purge_schedule(enabled=enabled, max_age_days=max_age_days)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("core:dashboard")

    AuditEvent.objects.create(
        user=request.user if request.user.is_authenticated else None,
        username=request.user.get_username() if request.user.is_authenticated else "",
        action="trash.purge.schedule.updated",
        object_type="trash_purge_schedule",
        object_id="automatic-trash-purge",
        outcome="success",
        details={
            "enabled": state.enabled,
            "max_age_days": state.max_age_days,
            "next_run": state.next_run.isoformat() if state.next_run else "",
        },
    )

    return redirect("core:dashboard")


@require_POST
@app_login_required
def update_audit_retention_schedule_view(request):
    redirect_to = _safe_next_url(request)
    enabled = request.POST.get("enabled") == "on"
    try:
        retention_days = int(request.POST.get("retention_days", "90"))
        state = update_audit_retention_schedule(enabled=enabled, retention_days=retention_days)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_to)

    AuditEvent.objects.create(
        user=request.user if request.user.is_authenticated else None,
        username=request.user.get_username() if request.user.is_authenticated else "",
        action="audit.retention.schedule.updated",
        object_type="audit_retention_schedule",
        object_id="automatic-audit-retention",
        outcome="success",
        details={
            "enabled": state.enabled,
            "retention_days": state.retention_days,
            "next_run": state.next_run.isoformat() if state.next_run else "",
        },
    )

    return redirect(redirect_to)


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


def _storage_gate_rows(storages: list[StorageMount], result_scan: ScanRun | None) -> list[dict[str, object]]:
    if not result_scan:
        return []

    rows = []
    gate_status = result_scan.storage_gate_status or {}
    for storage in storages:
        rows.append(
            {
                "storage": storage,
                "gate": gate_status.get(storage.storage_id, {}),
                "latest_scan_at": storage.latest_scan_at,
            }
        )
    return rows


def _decorate_audit_events(events: list[AuditEvent]) -> None:
    for event in events:
        event.display_module_key = _audit_module_key(event)
        event.display_module = _audit_module_label(event.display_module_key)
        event.display_action = _audit_action_label(event)
        event.display_object = _audit_object_label(event)
        event.search_text = " ".join(
            [
                event.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                event.username or "",
                event.display_module,
                event.display_action,
                event.display_object,
                event.outcome or "",
            ]
        )


def _audit_module_key(event: AuditEvent) -> str:
    details = event.details if isinstance(event.details, dict) else {}
    action = event.action or ""
    object_type = event.object_type or ""

    if action.startswith("auth."):
        return "auth"
    if action.startswith("network.") or object_type.startswith("network"):
        return "network"
    if action.startswith("vm.") or object_type in {"vm", "ct", "guest"}:
        return "vms"
    if action.startswith("cluster.") or object_type.startswith("cluster"):
        return "clusters"
    if (
        action.startswith("scan.")
        or action.startswith("file.")
        or action.startswith("trash.")
        or object_type in {"scan_run", "scan_schedule", "storage", "file"}
        or details.get("target_storage")
    ):
        return "storage"
    return "system"


def _audit_module_label(module_key: str) -> str:
    return {
        "auth": "Auth",
        "clusters": "Clusters",
        "network": "Network",
        "storage": "Storage",
        "system": "System",
        "vms": "VMs",
    }.get(module_key, "System")


def _audit_action_label(event: AuditEvent) -> str:
    details = event.details if isinstance(event.details, dict) else {}
    if event.action == "auth.login":
        return "Login"
    if event.action == "auth.logout":
        return "Logout"
    if event.action == "auth.login_failed":
        return "Login failed"
    if event.action == "scan.queued" and details.get("source") == "schedule":
        interval = details.get("interval_minutes")
        if interval:
            return f"Scheduled full scan ({interval} min)"
        return "Scheduled full scan"
    if event.action == "scan.queued":
        target = details.get("target_label")
        if target and target != "All storages":
            return f"Manual storage scan ({target})"
        return "Manual full scan"
    if event.action == "scan.schedule.skipped":
        return "Scheduled scan skipped"
    if event.action == "scan.schedule.updated":
        return "Scan schedule updated"
    if event.action == "scan.manual.skipped":
        return "Manual scan skipped"
    if event.action == "scan.retention.purge":
        return "Scan retention purge"
    if event.action == "scan.retention.purge_failed":
        return "Scan retention purge failed"
    if event.action == "file.downloaded":
        return "Download file"
    if event.action == "file.folder_created":
        return "Create folder"
    if event.action == "file.uploaded":
        return "Upload file"
    if event.action == "file.folder_uploaded":
        return "Upload folder"
    if event.action == "file.upload_normalized":
        return "Normalize uploaded disk metadata"
    if event.action == "file.upload_normalize_failed":
        return "Normalize uploaded disk metadata failed"
    if event.action == "file.moved":
        return "Move file"
    if event.action == "file.renamed":
        return "Rename file"
    if event.action == "file.trashed":
        return "Move file to trash"
    if event.action == "file.restored":
        return "Restore file"
    if event.action == "file.inflate_queued":
        return _inflate_action_label("Disk inflate queued", details)
    if event.action == "file.inflated":
        return _inflate_action_label("Inflate disk", details)
    if event.action == "file.inflate_failed":
        return _inflate_action_label("Inflate disk failed", details)
    if event.action == "trash.purge":
        return "Recycle Bin purge"
    if event.action == "trash.purge.schedule.updated":
        return "Recycle Bin purge schedule updated"
    if event.action == "audit.retention.purge":
        return "Audit retention purge"
    if event.action == "audit.retention.schedule.updated":
        return "Audit retention schedule updated"
    return event.action


def _inflate_action_label(base_label: str, details: dict) -> str:
    target_preallocation = details.get("target_preallocation")
    if target_preallocation:
        return f"{base_label} ({target_preallocation})"
    return base_label


def _audit_object_label(event: AuditEvent) -> str:
    if event.object_type == "scan_run" and event.object_id:
        return "Storage inventory scan"
    if event.object_type == "scan_retention":
        return "Scan retention"
    if event.object_type == "scan_schedule":
        return "Automatic scan schedule"
    if event.object_type == "trash_purge_schedule":
        return "Recycle Bin purge schedule"
    if event.object_type == "trash":
        return "Recycle Bin"
    if event.object_type == "audit_retention_schedule":
        return "Audit retention schedule"
    if event.object_type == "audit_retention":
        return "Audit retention"
    return f"{event.object_type} {event.object_id}".strip() or "-"


def _scan_timestamp(scan: ScanRun | None):
    if not scan:
        return None
    return scan.filesystem_scan_at or scan.finished_at or scan.created_at


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


def _decorate_storage_with_space_info(storage: StorageMount) -> None:
    storage.space_info = storage_space_info(storage.path)
    storage.storage_actions_enabled = settings.STORAGE_WRITE_ENABLED and storage.space_info.can_write
    storage.details = storage_details(storage, _latest_storage_result_scan(storage), storage.space_info)


def _refresh_latest_storage_directory(storage: StorageMount, directory_path: str = "") -> None:
    latest_scan = _latest_storage_result_scan(storage)
    if latest_scan is None:
        return
    refresh_storage_directory(storage=storage, scan=latest_scan, directory_path=directory_path)


def _prune_latest_storage_path(storage: StorageMount, path: str) -> None:
    latest_scan = _latest_storage_result_scan(storage)
    if latest_scan is None:
        return
    prefix = f"{path}/"
    FileInventory.objects.filter(scan_run=latest_scan, storage=storage).filter(Q(path=path) | Q(path__startswith=prefix)).delete()


def _decorate_orphan_files_with_action_state(files: list[FileInventory]) -> None:
    storages: dict[int, StorageMount] = {}
    for file in files:
        if file.storage_id not in storages:
            _decorate_storage_with_space_info(file.storage)
            storages[file.storage_id] = file.storage
        file.storage = storages[file.storage_id]
        _decorate_browser_entry(file)


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


def _storage_browser_url(storage: StorageMount, path: str = "", **params: object) -> str:
    url = reverse("core:storage_browser", args=[storage.storage_id])
    query = {}
    if path:
        query["path"] = path
    for key, value in params.items():
        if value in ("", None):
            continue
        query[key] = value
    if query:
        return f"{url}?{urlencode(query)}"
    return url


def _int_request_param(request, name: str, default: int) -> int:
    try:
        return int(request.GET.get(name, default))
    except (TypeError, ValueError):
        return default


def _storage_directory_or_404(storage: StorageMount, latest_scan: ScanRun | None, path: str) -> None:
    if latest_scan is None:
        raise Http404("No storage inventory has been scanned yet.")
    exists = FileInventory.objects.filter(
        scan_run=latest_scan,
        storage=storage,
        path=path,
        entry_type=FileInventory.EntryType.DIRECTORY,
    ).exists()
    if not exists:
        raise Http404("Directory not found in latest scan.")


def _audit_file_action(request, *, action: str, storage: StorageMount, path: str, details: dict[str, object]) -> None:
    AuditEvent.objects.create(
        user=request.user if request.user.is_authenticated else None,
        username=request.user.get_username() if request.user.is_authenticated else "",
        source_ip=_client_ip(request),
        action=action,
        object_type="file",
        object_id=f"{storage.storage_id}:{path}",
        outcome="success",
        details={
            "storage_id": storage.storage_id,
            "storage_name": storage.display_name,
            "path": path,
            **details,
        },
    )


def _queue_upload_normalization(storage: StorageMount, paths: list[str], user) -> None:
    image_paths = [path for path in paths if _is_proxmox_image_upload_path(path)]
    if not image_paths:
        return
    async_task(
        "core.tasks.normalize_uploaded_proxmox_image_paths_task",
        storage.id,
        image_paths,
        user.get_username() if getattr(user, "is_authenticated", False) else "",
    )


def _is_proxmox_image_upload_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return len(parts) >= 3 and parts[0] == "images" and parts[1].isdigit()


def _require_file_action_confirmations(request, risk: FileActionRisk) -> None:
    if risk.blocked:
        raise StorageActionError(risk.warning_message)
    if request.POST.get("confirm_basic") != "yes":
        raise StorageActionError("File action was not confirmed.")
    if risk.requires_extra_confirmation and request.POST.get("confirm_risk") != "yes":
        raise StorageActionError("Risk confirmation was not confirmed.")


def _require_file_action_confirmations_for_entries(request, entries: list[FileInventory]) -> None:
    risks = [file_action_risk(entry) for entry in entries]
    blocked_risk = next((risk for risk in risks if risk.blocked), None)
    if blocked_risk:
        raise StorageActionError(blocked_risk.warning_message)
    if request.POST.get("confirm_basic") != "yes":
        raise StorageActionError("File action was not confirmed.")
    if any(risk.requires_extra_confirmation for risk in risks) and request.POST.get("confirm_risk") != "yes":
        raise StorageActionError("Risk confirmation was not confirmed.")


def _selected_storage_file_entries(
    request,
    *,
    storage: StorageMount,
    latest_scan: ScanRun | None,
    entry_types: list[str] | None = None,
) -> list[FileInventory]:
    paths: list[str] = []
    seen: set[str] = set()
    for raw_path in request.POST.getlist("path"):
        path = _normalize_browser_path(raw_path)
        if not path or path in seen:
            continue
        paths.append(path)
        seen.add(path)
    if not paths:
        raise Http404("File not found.")

    entry_types = entry_types or [FileInventory.EntryType.FILE]
    entries_by_path = {
        entry.path: entry
        for entry in FileInventory.objects.filter(
            scan_run=latest_scan,
            storage=storage,
            path__in=paths,
            entry_type__in=entry_types,
        )
    }
    if len(entries_by_path) != len(paths):
        raise Http404("File not found.")
    return [entries_by_path[path] for path in paths]


def _storage_write_disabled_response() -> HttpResponseForbidden:
    return HttpResponseForbidden("Storage write actions are disabled.")


def _is_async_upload_request(request) -> bool:
    return request.headers.get("X-PVE-Helper-Async-Upload") == "1"


def _upload_success_response(request, redirect_to: str):
    if _is_async_upload_request(request):
        return JsonResponse({"ok": True, "redirect": redirect_to})
    return redirect(redirect_to)


def _upload_error_response(request, redirect_to: str, message: str):
    if _is_async_upload_request(request):
        return JsonResponse({"ok": False, "error": message, "redirect": redirect_to}, status=400)
    messages.error(request, message)
    return redirect(redirect_to)


def _resolve_storage_file(storage: StorageMount, relative_path: str) -> Path:
    root = Path(storage.path).resolve(strict=True)
    candidate = root.joinpath(*PurePosixPath(relative_path).parts).resolve(strict=True)

    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise Http404("File not found.")
    return candidate


def _download_response(request, storage: StorageMount, relative_path: str, absolute_path: Path):
    if settings.STORAGE_DOWNLOAD_ACCEL_ENABLED:
        response = HttpResponse(content_type="application/octet-stream")
        response["X-Accel-Redirect"] = _download_accel_uri(storage, relative_path)
        _decorate_download_response(response, absolute_path)
        return response

    file_size = absolute_path.stat().st_size
    range_header = request.headers.get("Range", "")
    if range_header:
        try:
            byte_range = _parse_http_byte_range(range_header, file_size)
        except ValueError:
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{file_size}"
            response["Accept-Ranges"] = "bytes"
            return response

        if byte_range is not None:
            start, end = byte_range
            length = end - start + 1
            response = StreamingHttpResponse(
                _file_range_iterator(absolute_path, start=start, length=length),
                status=206,
                content_type="application/octet-stream",
            )
            response["Content-Range"] = f"bytes {start}-{end}/{file_size}"
            response["Content-Length"] = str(length)
            _decorate_download_response(response, absolute_path)
            return response

    response = FileResponse(
        absolute_path.open("rb"),
        as_attachment=True,
        filename=absolute_path.name,
    )
    response.block_size = 1024 * 1024
    response["Accept-Ranges"] = "bytes"
    response["X-Accel-Buffering"] = "no"
    return response


def _decorate_download_response(response, absolute_path: Path) -> None:
    response["Accept-Ranges"] = "bytes"
    response["X-Accel-Buffering"] = "no"
    response["Content-Disposition"] = content_disposition_header(True, absolute_path.name)


def _download_accel_uri(storage: StorageMount, relative_path: str) -> str:
    prefix = settings.STORAGE_DOWNLOAD_ACCEL_PREFIX.rstrip("/")
    storage_id = quote(storage.storage_id, safe="")
    path = quote(PurePosixPath(relative_path).as_posix(), safe="/")
    return f"{prefix}/{storage_id}/{path}"


def _parse_http_byte_range(range_header: str, file_size: int) -> tuple[int, int] | None:
    units, separator, value = range_header.partition("=")
    if units.strip().lower() != "bytes" or separator != "=" or "," in value:
        return None

    start_text, separator, end_text = value.strip().partition("-")
    if separator != "-":
        return None
    if not start_text and not end_text:
        raise ValueError("empty range")

    if start_text:
        start = int(start_text)
        end = int(end_text) if end_text else file_size - 1
    else:
        suffix_length = int(end_text)
        if suffix_length <= 0:
            raise ValueError("invalid suffix range")
        start = max(file_size - suffix_length, 0)
        end = file_size - 1

    if start < 0 or end < start or start >= file_size:
        raise ValueError("unsatisfiable range")
    return start, min(end, file_size - 1)


def _file_range_iterator(absolute_path: Path, *, start: int, length: int):
    remaining = length
    with absolute_path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def _normalize_browser_path(raw_path: str) -> str:
    path = (raw_path or "").strip().strip("/")
    if not path:
        return ""

    parts = PurePosixPath(path).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise Http404("Invalid storage path.")
    return PurePosixPath(*parts).as_posix()


def _client_ip(request) -> str | None:
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


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


def _browser_folder_tree(
    scan: ScanRun,
    storage: StorageMount,
    current_path: str,
    *,
    ignored_paths: set[str] | None = None,
) -> list[dict[str, object]]:
    ignored_paths = ignored_paths or set()
    directory_paths = sorted(
        set(
            path
            for path in (
                FileInventory.objects.filter(
                    scan_run=scan,
                    storage=storage,
                    entry_type=FileInventory.EntryType.DIRECTORY,
                )
                .order_by("path")
                .values_list("path", flat=True)
            )
            if not is_ignored_storage_path(path, ignored_paths)
        ),
        key=lambda item: [part.lower() for part in item.split("/")],
    )
    directory_path_set = set(directory_paths)
    expanded_paths = {""}
    if current_path:
        current_parts = current_path.split("/")
        expanded_paths.update(
            "/".join(current_parts[:index]) for index in range(1, len(current_parts) + 1)
        )

    def has_children(path: str) -> bool:
        if not path:
            return bool(directory_paths)
        return any(candidate.startswith(f"{path}/") for candidate in directory_path_set)

    def is_initially_visible(path: str) -> bool:
        if not path:
            return True
        parts = path.split("/")
        return all(
            "/".join(parts[:index]) in expanded_paths for index in range(0, len(parts))
        )

    nodes = [
        {
            "name": storage.display_name,
            "path": "",
            "depth": 0,
            "is_current": current_path == "",
            "is_ancestor": bool(current_path),
            "is_expanded": "" in expanded_paths,
            "is_initially_visible": True,
            "has_children": has_children(""),
        }
    ]
    for path in directory_paths:
        parts = path.split("/")
        nodes.append(
            {
                "name": parts[-1],
                "path": path,
                "depth": len(parts),
                "is_current": path == current_path,
                "is_ancestor": bool(current_path) and current_path.startswith(f"{path}/"),
                "is_expanded": path in expanded_paths,
                "is_initially_visible": is_initially_visible(path),
                "has_children": has_children(path),
            }
        )
    return nodes


def _decorate_browser_entry(entry: FileInventory) -> None:
    entry.classification_label = _classification_label(entry)
    entry.classification_class = _classification_class(entry)
    entry.category_label = _content_category_label(entry.content_category, entry.path)
    image_info = (entry.evidence or {}).get("image_info") or {}
    entry.image_format = image_info.get("format", "")
    entry.virtual_size_bytes = image_info.get("virtual_size_bytes") or entry.size_bytes
    entry.disk_size_bytes = image_info.get("disk_size_bytes")
    entry.image_info_error = image_info.get("error", "")
    entry.qcow2_allocation_percent = image_info.get("qcow2_allocation_percent")
    if not isinstance(entry.qcow2_allocation_percent, (int, float)):
        entry.qcow2_allocation_percent = None
    entry.qcow2_allocation_error = image_info.get("qcow2_allocation_error", "")
    entry.qcow2_allocation_title = ""
    if entry.qcow2_allocation_percent is not None:
        allocated_clusters = image_info.get("qcow2_allocated_clusters")
        total_clusters = image_info.get("qcow2_total_clusters")
        if isinstance(allocated_clusters, int) and isinstance(total_clusters, int):
            entry.qcow2_allocation_title = f"{allocated_clusters} of {total_clusters} qcow2 clusters mapped"
    entry.has_qcow2_full_allocation = (
        entry.qcow2_allocation_percent is not None
        and entry.qcow2_allocation_percent >= MIN_INFLATE_ALLOCATED_PERCENT
    )
    entry.full_inflate_already_recorded = (
        entry.entry_type == FileInventory.EntryType.FILE
        and full_inflate_already_recorded(
            entry,
            current_virtual_size_bytes=entry.virtual_size_bytes
            if isinstance(entry.virtual_size_bytes, int)
            else None,
        )
    )
    entry.has_thin_usage = (
        entry.disk_size_bytes is not None
        and entry.virtual_size_bytes is not None
        and entry.disk_size_bytes != entry.virtual_size_bytes
    )
    entry.action_risk = file_action_risk(entry)
    entry.inflate_action_risk = file_action_risk(entry, block_running_guests=False)
    entry.can_trash = entry.entry_type in {FileInventory.EntryType.FILE, FileInventory.EntryType.DIRECTORY} and not entry.action_risk.blocked
    entry.can_rename = entry.entry_type == FileInventory.EntryType.FILE and entry.can_trash
    entry.can_inflate_action = (
        entry.entry_type == FileInventory.EntryType.FILE
        and not entry.inflate_action_risk.blocked
    )
    entry.can_inflate_metadata = (
        entry.can_inflate_action
        and entry.content_category == "vm_disk"
        and entry.image_format == "qcow2"
        and entry.qcow2_allocation_percent is not None
        and entry.qcow2_allocation_percent < MIN_INFLATE_ALLOCATED_PERCENT
    )
    entry.can_inflate_full = (
        entry.can_inflate_action
        and entry.content_category == "vm_disk"
        and entry.image_format == "qcow2"
        and entry.virtual_size_bytes is not None
        and entry.disk_size_bytes is not None
        and entry.qcow2_allocation_percent is not None
        and not entry.full_inflate_already_recorded
    )
    entry.can_inflate = entry.can_inflate_metadata or entry.can_inflate_full
    entry.action_blocked = entry.entry_type in {FileInventory.EntryType.FILE, FileInventory.EntryType.DIRECTORY} and entry.action_risk.blocked
    entry.action_warning_message = entry.action_risk.warning_message
    entry.action_requires_extra_confirmation = entry.action_risk.requires_extra_confirmation
    entry.inflate_warning_message = entry.inflate_action_risk.warning_message
    entry.inflate_requires_extra_confirmation = entry.inflate_action_risk.requires_extra_confirmation


def _classification_label(entry: FileInventory) -> str:
    return entry.get_classification_display()


def _classification_class(entry: FileInventory) -> str:
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


def _scheduled_action_target_label(action: ScheduledAction) -> str:
    label = f"{action.get_target_type_display()} {action.target_vmid}"
    if action.target_name_snapshot:
        label = f"{label} ({action.target_name_snapshot})"
    if action.target_node:
        label = f"{label} on {action.target_node}"
    return label


def _scheduled_action_schedule_label(action: ScheduledAction) -> str:
    if action.schedule_type == ScheduledAction.ScheduleType.ONCE:
        return f"Once at {tz.localtime(action.run_at or action.next_run_at).strftime('%Y-%m-%d %H:%M')}" if (action.run_at or action.next_run_at) else "Once"

    recurrence = action.recurrence if isinstance(action.recurrence, dict) else {}
    time_label = _recurrence_time_label(recurrence)
    if action.recurrence_kind == ScheduledAction.RecurrenceKind.DAILY:
        return f"Daily at {time_label}"
    if action.recurrence_kind == ScheduledAction.RecurrenceKind.WEEKLY:
        return f"Weekly at {time_label}"
    if action.recurrence_kind == ScheduledAction.RecurrenceKind.MONTHLY_ORDINAL:
        ordinal = recurrence.get("ordinal", recurrence.get("week", "first"))
        weekday = recurrence.get("weekday", "weekday")
        return f"Monthly on the {ordinal} {weekday} at {time_label}"
    if action.recurrence_kind == ScheduledAction.RecurrenceKind.MONTHLY_DAY:
        day = recurrence.get("day", recurrence.get("day_of_month", "?"))
        return f"Monthly on day {day} at {time_label}"
    return "Advanced recurrence"


def _recurrence_time_label(recurrence: dict) -> str:
    raw_time = recurrence.get("time")
    if raw_time:
        return str(raw_time)
    try:
        hour = int(recurrence.get("hour", 0))
        minute = int(recurrence.get("minute", 0))
    except (TypeError, ValueError):
        hour = 0
        minute = 0
    return f"{hour:02d}:{minute:02d}"


def _scheduled_action_status_class(status: str) -> str:
    return {
        ScheduledAction.LastStatus.COMPLETED: "completed",
        ScheduledAction.LastStatus.QUEUED: "queued",
        ScheduledAction.LastStatus.FAILED: "failed",
        ScheduledAction.LastStatus.TIMEOUT: "failed",
        ScheduledAction.LastStatus.SKIPPED: "warning",
        ScheduledAction.LastStatus.MISSED: "warning",
    }.get(status, "")


def _scheduled_run_status_class(status: str) -> str:
    return {
        ScheduledActionRun.Status.COMPLETED: "completed",
        ScheduledActionRun.Status.QUEUED: "queued",
        ScheduledActionRun.Status.PREFLIGHT: "running",
        ScheduledActionRun.Status.SUBMITTED: "running",
        ScheduledActionRun.Status.POLLING: "running",
        ScheduledActionRun.Status.FAILED: "failed",
        ScheduledActionRun.Status.TIMEOUT: "failed",
        ScheduledActionRun.Status.STALE: "failed",
        ScheduledActionRun.Status.SKIPPED: "warning",
        ScheduledActionRun.Status.MISSED: "warning",
    }.get(status, "")


def _trash_purge_schedule_state():
    return trash_purge_schedule_state()

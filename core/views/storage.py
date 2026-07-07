from __future__ import annotations

from .common import *  # noqa: F401,F403
from . import common
from ..services.storage import StorageScanner


STORAGE_CONTENT_TYPES = [
    {
        "key": "images",
        "label": "Disk image",
        "description": "VM disks and templates stored as Proxmox disk volumes.",
    },
    {
        "key": "iso",
        "label": "ISO image",
        "description": "Install media and other ISO files under template/iso.",
    },
    {
        "key": "vztmpl",
        "label": "Container template",
        "description": "LXC templates under template/cache.",
    },
    {
        "key": "backup",
        "label": "Backup",
        "description": "VZDUMP backup archives under dump.",
    },
    {
        "key": "rootdir",
        "label": "Container",
        "description": "LXC root filesystems and container mount volumes.",
    },
    {
        "key": "snippets",
        "label": "Snippets",
        "description": "Hook scripts and Cloud-Init snippets under snippets.",
    },
    {
        "key": "import",
        "label": "Import",
        "description": "Imported disk images for VM import workflows.",
    },
]
STORAGE_CONTENT_ORDER = [item["key"] for item in STORAGE_CONTENT_TYPES]


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


def _live_status_for(statuses: dict, node: str, object_type: str, vmid: int, default: str = "") -> str:
    return statuses.get((node or "", object_type, vmid), default)


# ---------------------------------------------------------------------------
# Local / API-only storages (local, local-lvm, ZFS, ...): read-only tabbed view
# built entirely from the Proxmox API since pve-helper cannot mount them.
# ---------------------------------------------------------------------------

_API_STORAGE_TABS = [
    ("summary", "Summary", "core:api_storage_summary"),
    ("monitor", "Monitor", "core:api_storage_monitor"),
    ("volumes", "Volumes", "core:api_storage_volumes"),
    ("vms", "VMs/CTs", "core:api_storage_vms"),
    ("content", "Content", "core:api_storage_content"),
    ("configure", "Configuration", "core:api_storage_configure"),
]


def _api_num(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _api_storage_get(node: str, storage: str, subpath: str):
    """GET nodes/{node}/storage/{storage}/{subpath}. Returns (data, found, error)."""
    node_q = quote(node, safe="")
    storage_q = quote(storage, safe="")
    error = ""
    for client in common.configured_clients():
        try:
            data = client.get(f"nodes/{node_q}/storage/{storage_q}/{subpath}")
        except ProxmoxAPIError as exc:
            error = str(exc)
            continue
        return data, True, ""
    return None, False, error


def _api_storage_context(node: str, storage: str, active_tab: str, *, status=None, found=None, error=""):
    if status is None:
        status, found, error = _api_storage_get(node, storage, "status")
        status = status if isinstance(status, dict) else {}
    total = _api_num(status.get("total"))
    used = _api_num(status.get("used"))
    avail = _api_num(status.get("avail"))
    if used is None and total is not None and avail is not None:
        used = total - avail
    used_pct = round(used / total * 100) if total and used is not None and total > 0 else None
    content_types = [c for c in str(status.get("content") or "").split(",") if c]
    tabs = [
        {"key": key, "label": label, "url": reverse(name, args=[node, storage]), "active": key == active_tab}
        for key, label, name in _API_STORAGE_TABS
    ]
    return {
        **navigation_context("datastores"),
        "node": node,
        "storage": storage,
        "status": status,
        "found": bool(found),
        "error": error,
        "capacity": {"total": total, "used": used, "avail": avail, "used_pct": used_pct},
        "content_types": content_types,
        "storage_type": status.get("type") or "",
        "storage_active": str(status.get("active") or "") in ("1", "True", "true"),
        "storage_enabled": str(status.get("enabled") or "1") in ("1", "True", "true"),
        "api_storage_tabs": tabs,
        "active_api_tab": active_tab,
        "active_api_node": node,
        "active_api_storage": storage,
    }


def _api_storage_volumes(node: str, storage: str, highlight_vmid=None):
    content, found, error = _api_storage_get(node, storage, "content")
    import_content, _imp_found, _imp_error = _api_storage_get(node, storage, "content?content=import")
    volumes = []
    seen: set[str] = set()
    for entry in list(content or []) + list(import_content or []):
        if not isinstance(entry, dict):
            continue
        volid = entry.get("volid", "")
        if volid in seen:
            continue
        seen.add(volid)
        entry_vmid = entry.get("vmid")
        volumes.append(
            {
                "volid": volid,
                "content": entry.get("content", ""),
                "format": entry.get("format", ""),
                "size": entry.get("size"),
                "used": entry.get("used"),
                "vmid": entry_vmid,
                "importable": entry.get("content") == "import",
                "highlight": highlight_vmid is not None and str(entry_vmid) == str(highlight_vmid),
            }
        )
    volumes.sort(key=lambda item: (str(item["vmid"] or ""), item["volid"]))
    return volumes, found, error


@app_login_required
def storage_api_inventory(request, node: str, storage: str):
    """Backward-compatible entry point; redirects to the Summary tab, keeping the
    optional ?vmid highlight used by the guest Datastores tab."""
    url = reverse("core:api_storage_summary", args=[node, storage])
    vmid = request.GET.get("vmid")
    if vmid:
        url = f"{url}?vmid={quote(str(vmid))}"
    return redirect(url)


@app_login_required
def api_storage_summary(request, node: str, storage: str):
    context = _api_storage_context(node, storage, "summary")
    volumes, _found, _error = _api_storage_volumes(node, storage)
    vmids = {str(v["vmid"]) for v in volumes if v.get("vmid")}
    context.update({"volume_count": len(volumes), "guest_count": len(vmids)})
    return render(request, "core/storage_api/summary.html", context)


@app_login_required
def api_storage_volumes(request, node: str, storage: str):
    highlight_vmid = _int_or_zero(request.GET.get("vmid")) or None
    volumes, found, error = _api_storage_volumes(node, storage, highlight_vmid)
    context = _api_storage_context(node, storage, "volumes")
    context.update({"volumes": volumes, "found": found or context["found"], "error": error or context["error"], "highlight_vmid": highlight_vmid})
    return render(request, "core/storage_api/volumes.html", context)


@app_login_required
def api_storage_vms(request, node: str, storage: str):
    scan = _latest_result_scan()
    guests = []
    if scan:
        prefix = f"{storage}:"
        for obj in ProxmoxInventory.objects.filter(
            scan_run=scan,
            node=node,
            object_type__in=[ProxmoxInventory.ObjectType.VM, ProxmoxInventory.ObjectType.CT],
        ).order_by("object_type", "vmid"):
            matching = [ref for ref in (obj.disk_references or []) if ref.startswith(prefix)]
            if matching:
                obj.matching_disk_references = matching
                guests.append(obj)
    if guests:
        live_status = common.fetch_live_guest_status()
        for guest in guests:
            guest.status = _live_status_for(live_status, guest.node or "", guest.object_type, guest.vmid, guest.status)
        _decorate_guests_with_scheduled_actions(guests)
    context = _api_storage_context(node, storage, "vms")
    context.update({"guests": guests, "live_status_cache_seconds": LIVE_GUEST_STATUS_CACHE_SECONDS})
    return render(request, "core/storage_api/vms.html", context)


def _api_live_content_values(storage: str) -> list[str]:
    for client in common.configured_clients():
        try:
            config = client.storage_config(storage)
        except ProxmoxAPIError:
            continue
        return _parse_storage_content_values(config.get("content", ""))
    return []


def _api_content_usage(node: str, storage: str) -> dict[str, dict]:
    """Count volumes per content type from the API volume list, so we can block
    removing a content type that is still in use (the local analog of the
    filesystem-scan blocker used for mounted storages)."""
    volumes, _found, _error = _api_storage_volumes(node, storage)
    usage: dict[str, dict] = {}
    for volume in volumes:
        key = str(volume.get("content") or "").strip()
        if not key:
            continue
        bucket = usage.setdefault(key, {"count": 0, "examples": []})
        bucket["count"] += 1
        if len(bucket["examples"]) < 3 and volume.get("volid"):
            bucket["examples"].append(volume["volid"])
    return usage


def _api_content_options(current: list[str], usage: dict[str, dict]) -> list[dict]:
    definitions = list(STORAGE_CONTENT_TYPES)
    for key in sorted(set(current) - set(STORAGE_CONTENT_ORDER)):
        definitions.append(
            {
                "key": key,
                "label": key,
                "description": "Unknown content type preserved from the current Proxmox storage configuration.",
            }
        )
    return [
        {
            **definition,
            "selected": definition["key"] in current,
            "usage_count": usage.get(definition["key"], {}).get("count", 0),
            "usage_examples": usage.get(definition["key"], {}).get("examples", [])[:3],
        }
        for definition in definitions
    ]


def _api_content_blockers(usage: dict[str, dict], removed: list[str]) -> list[dict]:
    labels = {item["key"]: item["label"] for item in STORAGE_CONTENT_TYPES}
    return [
        {
            "key": key,
            "label": labels.get(key, key),
            "count": usage.get(key, {}).get("count", 0),
            "examples": usage.get(key, {}).get("examples", []),
        }
        for key in removed
        if usage.get(key, {}).get("count", 0) > 0
    ]


@app_login_required
def api_storage_content(request, node: str, storage: str):
    context = _api_storage_context(node, storage, "content")
    current = _api_live_content_values(storage)
    usage = _api_content_usage(node, storage)
    context.update(
        {
            "content_options": _api_content_options(current, usage),
            "current_content": current,
            "storage_write_enabled": settings.STORAGE_WRITE_ENABLED,
        }
    )
    return render(request, "core/storage_api/content.html", context)


@require_POST
@app_login_required
def update_api_storage_content(request, node: str, storage: str):
    redirect_to = reverse("core:api_storage_content", args=[node, storage])
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    current = _api_live_content_values(storage)
    requested = _ordered_storage_content(request.POST.getlist("content"), current)
    if not requested:
        messages.error(request, "Select at least one content type.")
        return redirect(redirect_to)

    usage = _api_content_usage(node, storage)
    removed = [key for key in current if key not in requested]
    blockers = _api_content_blockers(usage, removed)
    if blockers:
        for blocker in blockers:
            examples = ", ".join(blocker["examples"][:3])
            suffix = f" Examples: {examples}." if examples else ""
            messages.error(
                request,
                f"Cannot disable {blocker['label']} because {blocker['count']} volume"
                f"{'' if blocker['count'] == 1 else 's'} on this storage use it.{suffix}",
            )
        return redirect(redirect_to)

    updated = False
    err = ""
    for client in common.configured_clients():
        try:
            client.set_storage_content(storage, requested)
            updated = True
            err = ""
            break
        except ProxmoxAPIError as exc:
            err = str(exc)
    if not updated:
        messages.error(request, f"Failed to update storage content: {err or 'No configured Proxmox endpoints.'}")
        return redirect(redirect_to)

    record_audit_event(
        request,
        action="storage.content.updated",
        object_type="storage",
        object_id=storage,
        details={"storage_id": storage, "node": node, "old_content": current, "new_content": requested},
    )
    return redirect(redirect_to)


@app_login_required
def api_storage_monitor(request, node: str, storage: str):
    context = _api_storage_context(node, storage, "monitor")
    chart_data = _api_storage_space_chart_data(node, storage, tz.now())
    context["space_chart_data_json"] = json.dumps(chart_data)
    return render(request, "core/storage_api/monitor.html", context)


@app_login_required
def api_storage_configure(request, node: str, storage: str):
    context = _api_storage_context(node, storage, "configure")
    scan = _latest_result_scan()
    config = {}
    if scan:
        row = (
            ProxmoxInventory.objects.filter(
                scan_run=scan, node=node, object_type=ProxmoxInventory.ObjectType.STORAGE, name=storage
            ).first()
        )
        if row and isinstance(row.config, dict):
            config = row.config
    # Present the interesting config keys in a stable order; skip the nested
    # node_status blob and empty values.
    skip = {"node_status", "storage", "total", "used", "avail", "used_fraction"}
    config_rows = [
        {"key": key, "value": config[key]}
        for key in sorted(config)
        if key not in skip and config[key] not in ("", None, [])
    ]
    context.update({"storage_config": config, "config_rows": config_rows})
    return render(request, "core/storage_api/configure.html", context)


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
        storage.space_info = common.storage_space_info(storage.path)
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

    # Link each referenced disk image to the VM/CT that owns it.
    from django.db.models import Max

    from core.services.classification import extract_vmid_from_image_path

    guest_scan_id = ProxmoxInventory.objects.filter(
        object_type__in=[ProxmoxInventory.ObjectType.VM, ProxmoxInventory.ObjectType.CT]
    ).aggregate(latest=Max("scan_run_id"))["latest"]
    guest_map: dict[int, ProxmoxInventory] = {}
    if guest_scan_id:
        for obj in ProxmoxInventory.objects.filter(
            scan_run_id=guest_scan_id,
            object_type__in=[ProxmoxInventory.ObjectType.VM, ProxmoxInventory.ObjectType.CT],
            vmid__isnull=False,
        ):
            guest_map.setdefault(obj.vmid, obj)
    for entry in entries:
        entry.referenced_guest = None
        if (
            entry.entry_type == FileInventory.EntryType.FILE
            and entry.classification == FileInventory.Classification.REFERENCED
        ):
            guest = guest_map.get(extract_vmid_from_image_path(entry.path) or -1)
            if guest is not None:
                entry.referenced_guest = {
                    "name": guest.name or f"VM {guest.vmid}",
                    "url": reverse("core:guest_summary", args=[guest.object_type, guest.vmid]),
                }

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
        "dest_storages": StorageMount.objects.filter(enabled=True).order_by("display_name"),
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
        include_parent_in_partial = request.GET.get("include_parent") == "1" and current_path and file_offset == 0
        return JsonResponse(
            {
                "rows_html": render_to_string(
                    "core/partials/storage_file_rows.html",
                    {**context, "include_parent_row": include_parent_in_partial},
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

    record_audit_event(
        request,
        action="file.downloaded",
        object_type="file",
        object_id=f"{storage.storage_id}:{entry.path}",
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
    return _space_chart_from_queryset(StorageSpaceSnapshot.objects.filter(storage=storage), now)


def _api_storage_space_chart_data(node: str, storage_id: str, now) -> list[dict[str, object]]:
    return _space_chart_from_queryset(
        StorageSpaceSnapshot.objects.filter(storage__isnull=True, node=node, api_storage_id=storage_id), now
    )


def _space_chart_from_queryset(base_qs, now) -> list[dict[str, object]]:
    cutoff = now - timedelta(days=SPACE_CHART_DAYS)
    scheduled_history = list(
        base_qs.filter(
            scan_run__isnull=True,
            recorded_at__gte=cutoff,
        ).order_by("recorded_at")
    )
    history = scheduled_history or list(
        base_qs.filter(
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
def storage_content(request, storage_id: str):
    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    _decorate_storage_with_space_info(storage)
    latest_scan = _latest_storage_result_scan(storage)
    content_scan = _latest_storage_content_scan(storage) or latest_scan
    current_content = _live_storage_content_values(storage)
    context = {
        **_storage_tab_context(storage, latest_scan, "content"),
        "content_options": _storage_content_options(storage, content_scan, current_content=current_content),
        "current_content": current_content,
    }
    return render(request, "core/storage_content.html", context)


@require_POST
@app_login_required
def update_storage_content(request, storage_id: str):
    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    _decorate_storage_with_space_info(storage)
    latest_scan = _latest_storage_result_scan(storage)
    config_scan = latest_scan
    current_content = _live_storage_content_values(storage)
    requested_content = _ordered_storage_content(request.POST.getlist("content"), current_content)
    redirect_to = reverse("core:storage_content", args=[storage.storage_id])

    if not requested_content:
        messages.error(request, "Select at least one content type.")
        return redirect(redirect_to)

    try:
        latest_scan = _run_storage_content_preflight_scan(storage)
    except Exception as exc:
        messages.error(request, f"Fresh preflight scan failed; storage content was not changed: {exc}")
        return redirect(redirect_to)

    preflight_errors = _storage_content_preflight_errors(latest_scan, storage)
    if preflight_errors:
        for error in preflight_errors:
            messages.error(request, error)
        return redirect(redirect_to)

    removed = [key for key in current_content if key not in requested_content]
    blockers = _storage_content_blockers(storage, latest_scan, removed)
    if blockers:
        for blocker in blockers:
            examples = ", ".join(blocker["examples"][:3])
            suffix = f" Examples: {examples}." if examples else ""
            messages.error(
                request,
                f"Cannot disable {blocker['label']} because {blocker['count']} existing item"
                f"{'' if blocker['count'] == 1 else 's'} use this storage.{suffix}",
            )
        return redirect(redirect_to)

    updated = False
    err = ""
    for client in common.configured_clients():
        try:
            client.set_storage_content(storage.storage_id, requested_content)
            updated = True
            err = ""
            break
        except ProxmoxAPIError as exc:
            err = str(exc)
    if not updated:
        if not err:
            err = "No configured Proxmox endpoints."
        messages.error(request, f"Failed to update storage content: {err}")
        return redirect(redirect_to)

    _update_latest_storage_config_content(storage, config_scan, requested_content)
    record_audit_event(
        request,
        action="storage.content.updated",
        object_type="storage",
        object_id=storage.storage_id,
        details={
            "storage_id": storage.storage_id,
            "storage_name": storage.display_name,
            "old_content": current_content,
            "new_content": requested_content,
        },
    )
    return redirect(redirect_to)


def _run_storage_content_preflight_scan(storage: StorageMount) -> ScanRun:
    now = tz.now()
    scan = ScanRun.objects.create(
        status=ScanRun.Status.RUNNING,
        started_at=now,
        queued_task_id="content-preflight",
        progress_message="Scanning storage content before applying changes.",
        target_storage=storage,
        target_label=storage.display_name,
    )

    scanner = StorageScanner(
        storage.storage_id,
        storage.path,
        ignored_paths=ignored_relative_paths_for_storage(storage),
    )
    rows = [
        FileInventory(
            scan_run=scan,
            storage=storage,
            path=entry.path,
            derived_volid=entry.derived_volid,
            content_category=entry.content_category,
            entry_type=entry.entry_type,
            size_bytes=entry.size_bytes,
            modified_at=_storage_content_preflight_timestamp(entry.modified_at),
        )
        for entry in scanner.iter_entries()
    ]
    FileInventory.objects.bulk_create(rows, batch_size=1000)

    scan.status = ScanRun.Status.COMPLETED
    scan.finished_at = tz.now()
    scan.filesystem_scan_at = scan.finished_at
    scan.summary_counts = {"files": len(rows), "proxmox_objects": 0, "classifications": {}}
    scan.error_details = (
        {"storage": {storage.storage_id: {"errors": scanner.errors}}}
        if scanner.errors
        else {}
    )
    scan.progress_message = (
        f"Content preflight scan completed with {len(scanner.errors)} warning(s)."
        if scanner.errors
        else "Content preflight scan completed."
    )
    scan.save(
        update_fields=[
            "status",
            "finished_at",
            "filesystem_scan_at",
            "summary_counts",
            "error_details",
            "progress_message",
            "updated_at",
        ]
    )
    return scan


def _storage_content_preflight_timestamp(value: float | None):
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=tz.get_current_timezone())


def _storage_content_preflight_errors(scan: ScanRun | None, storage: StorageMount) -> list[str]:
    if scan is None:
        return ["Fresh preflight scan did not complete; storage content was not changed."]
    if scan.status != ScanRun.Status.COMPLETED:
        return ["Fresh preflight scan did not complete; storage content was not changed."]

    details = scan.error_details if isinstance(scan.error_details, dict) else {}
    errors: list[str] = []
    if details.get("proxmox"):
        errors.append("Fresh preflight scan could not read all Proxmox inventory; storage content was not changed.")
    storage_errors = (details.get("storage") or {}).get(storage.storage_id)
    if storage_errors:
        errors.append("Fresh preflight scan could not read all files on this storage; storage content was not changed.")
    return errors


def _storage_content_values(storage: StorageMount) -> list[str]:
    content = getattr(getattr(storage, "details", None), "content", "") or ""
    return [part.strip() for part in str(content).split(",") if part.strip()]


def _live_storage_content_values(storage: StorageMount) -> list[str]:
    for client in common.configured_clients():
        try:
            config = client.storage_config(storage.storage_id)
        except ProxmoxAPIError:
            continue
        return _parse_storage_content_values(config.get("content", ""))
    return _storage_content_values(storage)


def _parse_storage_content_values(content) -> list[str]:
    return [part.strip() for part in str(content or "").split(",") if part.strip()]


def _ordered_storage_content(values: list[str], current_content: list[str]) -> list[str]:
    requested = {value for value in values if value}
    known = [key for key in STORAGE_CONTENT_ORDER if key in requested]
    unknown = sorted(key for key in requested if key in current_content and key not in STORAGE_CONTENT_ORDER)
    return known + unknown


def _storage_content_options(
    storage: StorageMount,
    latest_scan: ScanRun | None,
    *,
    current_content: list[str] | None = None,
) -> list[dict]:
    current = current_content if current_content is not None else _storage_content_values(storage)
    usage = _storage_content_usage(storage, latest_scan)
    definitions = list(STORAGE_CONTENT_TYPES)
    for key in sorted(set(current) - set(STORAGE_CONTENT_ORDER)):
        definitions.append(
            {
                "key": key,
                "label": key,
                "description": "Unknown content type preserved from the current Proxmox storage configuration.",
            }
        )
    return [
        {
            **definition,
            "selected": definition["key"] in current,
            "usage_count": usage.get(definition["key"], {"count": 0, "examples": []})["count"],
            "usage_examples": usage.get(definition["key"], {"count": 0, "examples": []})["examples"][:3],
        }
        for definition in definitions
    ]


def _storage_content_blockers(storage: StorageMount, latest_scan: ScanRun | None, removed: list[str]) -> list[dict]:
    if not removed:
        return []
    usage = _storage_content_usage(storage, latest_scan)
    labels = {item["key"]: item["label"] for item in STORAGE_CONTENT_TYPES}
    return [
        {
            "key": key,
            "label": labels.get(key, key),
            "count": usage.get(key, {"count": 0, "examples": []})["count"],
            "examples": usage.get(key, {"count": 0, "examples": []})["examples"],
        }
        for key in removed
        if usage.get(key, {"count": 0})["count"] > 0
    ]


def _storage_content_usage(storage: StorageMount, latest_scan: ScanRun | None) -> dict[str, dict]:
    usage = {key: {"items": set(), "examples": []} for key in STORAGE_CONTENT_ORDER}
    if latest_scan is None:
        return _finalize_storage_content_usage(usage)

    category_map = {
        "images": {"vm_disk", "base_image"},
        "iso": {"iso"},
        "vztmpl": {"ct_template"},
        "backup": {"backup"},
        "rootdir": {"ct_private"},
        "snippets": {"snippet", "snippets"},
    }
    for key, categories in category_map.items():
        entries = (
            FileInventory.objects.filter(
                scan_run=latest_scan,
                storage=storage,
                entry_type=FileInventory.EntryType.FILE,
                content_category__in=categories,
            )
            .order_by("path")
            .values_list("path", flat=True)[:1000]
        )
        for path in entries:
            _add_storage_content_usage(usage, key, path, path)

    inventory = ProxmoxInventory.objects.filter(
        scan_run=latest_scan,
        object_type__in=[ProxmoxInventory.ObjectType.VM, ProxmoxInventory.ObjectType.CT],
    ).order_by("node", "object_type", "vmid")
    for obj in inventory:
        if not isinstance(obj.config, dict):
            continue
        for key, value in _iter_config_strings(obj.config):
            volid = parse_config_value_volid(value)
            if not volid.startswith(f"{storage.storage_id}:"):
                continue
            content_key = _content_type_for_config_reference(obj, key, value, volid)
            if content_key not in usage:
                continue
            label = _guest_reference_label(obj, key)
            _add_storage_content_usage(usage, content_key, f"{obj.node}:{obj.object_type}:{obj.vmid}:{key}:{volid}", label)

    return _finalize_storage_content_usage(usage)


def _iter_config_strings(value, key: str = ""):
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            yield from _iter_config_strings(child_value, str(child_key))
        return
    if isinstance(value, list):
        for child_value in value:
            yield from _iter_config_strings(child_value, key)
        return
    if isinstance(value, str):
        yield key, value


def _content_type_for_config_reference(obj: ProxmoxInventory, key: str, value: str, volid: str) -> str:
    relative = volid.split(":", 1)[1] if ":" in volid else ""
    if relative.startswith("snippets/"):
        return "snippets"
    if relative.startswith("template/iso/") or "media=cdrom" in value:
        return "iso"
    if relative.startswith("template/cache/"):
        return "vztmpl"
    if obj.object_type == ProxmoxInventory.ObjectType.CT:
        return "rootdir"
    if key.startswith(("ide", "sata", "scsi", "virtio", "efidisk", "tpmstate", "unused")):
        return "images"
    return ""


def _guest_reference_label(obj: ProxmoxInventory, key: str) -> str:
    name = obj.name or f"{obj.object_type.upper()} {obj.vmid}"
    node = f" on {obj.node}" if obj.node else ""
    return f"{name}{node} ({key})"


def _add_storage_content_usage(usage: dict[str, dict], key: str, item: str, example: str) -> None:
    bucket = usage.setdefault(key, {"items": set(), "examples": []})
    if item in bucket["items"]:
        return
    bucket["items"].add(item)
    if len(bucket["examples"]) < 10:
        bucket["examples"].append(example)


def _finalize_storage_content_usage(usage: dict[str, dict]) -> dict[str, dict]:
    return {
        key: {
            "count": len(bucket["items"]),
            "examples": list(bucket["examples"]),
        }
        for key, bucket in usage.items()
    }


def _update_latest_storage_config_content(storage: StorageMount, latest_scan: ScanRun | None, content: list[str]) -> None:
    if latest_scan is None:
        return
    for obj in ProxmoxInventory.objects.filter(
        scan_run=latest_scan,
        object_type=ProxmoxInventory.ObjectType.STORAGE,
        name=storage.storage_id,
    ):
        config = dict(obj.config or {})
        config["content"] = ",".join(content)
        obj.config = config
        obj.save(update_fields=["config", "updated_at"])


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
        live_status = common.fetch_live_guest_status()
        for guest in guests:
            guest.status = _live_status_for(live_status, guest.node or "", guest.object_type, guest.vmid, guest.status)
        _decorate_guests_with_scheduled_actions(guests)

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

    dest_storage_id = request.POST.get("dest_storage", "").strip()
    dest_storage = storage
    if dest_storage_id:
        dest_storage = StorageMount.objects.filter(storage_id=dest_storage_id, enabled=True).first()
        if dest_storage is None:
            messages.error(request, "Unknown destination storage.")
            return redirect(redirect_to)

    try:
        _require_file_action_confirmations_for_entries(request, entries)
        if dest_storage_id:
            dest_directory = request.POST.get("dest_directory", "")
            results = [
                transfer_storage_file(
                    source_storage=storage,
                    entry=entry,
                    dest_storage=dest_storage,
                    dest_directory=dest_directory,
                    keep_source=False,
                )
                for entry in entries
            ]
        else:
            results = [
                move_storage_file(storage=storage, entry=entry, new_path=request.POST.get("new_path", ""))
                for entry in entries
            ]
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_to)

    refresh: dict[tuple[str, str], tuple[StorageMount, str]] = {}
    for result in results:
        if dest_storage_id:
            _audit_file_action(
                request,
                action="file.moved",
                storage=dest_storage,
                path=str(result["dest_path"]),
                details={"old_path": result["source_path"], "source_storage": storage.storage_id},
            )
            refresh[(storage.storage_id, str(result["source_directory_path"]))] = (storage, str(result["source_directory_path"]))
            dest_dir = str(result["dest_directory_path"])
            refresh[(dest_storage.storage_id, dest_dir)] = (dest_storage, dest_dir)
            dest_parent = dest_dir.rsplit("/", 1)[0] if "/" in dest_dir else ""
            refresh[(dest_storage.storage_id, dest_parent)] = (dest_storage, dest_parent)
        else:
            _audit_file_action(
                request,
                action="file.moved",
                storage=storage,
                path=str(result["new_path"]),
                details={"old_path": result["old_path"]},
            )
            refresh[(storage.storage_id, str(result["source_directory_path"]))] = (storage, str(result["source_directory_path"]))
            refresh[(storage.storage_id, str(result["target_directory_path"]))] = (storage, str(result["target_directory_path"]))
    for st, directory_path in refresh.values():
        _refresh_latest_storage_directory(st, directory_path)
    return redirect(redirect_to)


@require_POST
@app_login_required
def copy_storage_file_view(request, storage_id: str):
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
    dest_storage = StorageMount.objects.filter(
        storage_id=request.POST.get("dest_storage", "").strip(), enabled=True
    ).first()
    if dest_storage is None:
        messages.error(request, "Unknown destination storage.")
        return redirect(redirect_to)

    try:
        result = transfer_storage_file(
            source_storage=storage,
            entry=entry,
            dest_storage=dest_storage,
            dest_directory=request.POST.get("dest_directory", ""),
            dest_name=request.POST.get("dest_name", ""),
            keep_source=True,
        )
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_to)

    _audit_file_action(
        request,
        action="file.copied",
        storage=dest_storage,
        path=str(result["dest_path"]),
        details={"source_storage": storage.storage_id, "source_path": result["source_path"]},
    )
    dest_directory = str(result["dest_directory_path"])
    _refresh_latest_storage_directory(dest_storage, dest_directory)
    # Also refresh the parent so a newly created destination folder shows up.
    if "/" in dest_directory or dest_directory:
        _refresh_latest_storage_directory(dest_storage, dest_directory.rsplit("/", 1)[0] if "/" in dest_directory else "")
    # No success toast — the outcome is recorded as file.copied in the audit log.
    return redirect(redirect_to)


@app_login_required
def storage_folders_view(request, storage_id: str):
    """JSON list of the folders in a storage, for the move/copy destination picker
    (so a folder can be chosen from a dropdown instead of typed by hand)."""
    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    scan = _latest_storage_result_scan(storage)
    folders: list[str] = []
    if scan:
        ignored_paths = ignored_relative_paths_for_storage(storage)
        folders = sorted(
            (
                path
                for path in FileInventory.objects.filter(
                    scan_run=scan,
                    storage=storage,
                    entry_type=FileInventory.EntryType.DIRECTORY,
                )
                .order_by("path")
                .values_list("path", flat=True)
                if not is_ignored_storage_path(path, ignored_paths)
            ),
            key=lambda item: [part.lower() for part in item.split("/")],
        )
    return JsonResponse({"folders": folders})


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

    task_id = common.async_task(
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
    if request.POST.get("confirm_basic") != "yes":
        messages.error(request, "Permanent delete was not confirmed.")
        return redirect(redirect_to)
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
def classified_files(request):
    """Drill-down list behind the dashboard classification counters: every file
    with the requested classification across the enabled storages."""
    classification = request.GET.get("classification", "")
    if classification not in FileInventory.Classification.values:
        messages.error(request, "Unknown classification.")
        return redirect("core:dashboard")
    # Likely orphans have their own workspace (register / trash actions).
    if classification == FileInventory.Classification.LIKELY_ORPHAN:
        return redirect("core:orphan_finder")

    storage_id = request.GET.get("storage", "").strip()
    storages = StorageMount.objects.filter(enabled=True).order_by("display_name")
    if storage_id:
        storages = storages.filter(storage_id=storage_id)

    files: list[FileInventory] = []
    for storage in storages:
        scan = _latest_storage_result_scan(storage)
        if not scan:
            continue
        files.extend(
            FileInventory.objects.select_related("storage", "scan_run")
            .filter(scan_run=scan, storage=storage, classification=classification)
            .order_by("path")
        )
    files = sorted(files, key=lambda item: (item.storage.display_name, item.path))

    page_size = 200
    total = len(files)
    try:
        page = max(0, int(request.GET.get("page", "0")))
    except ValueError:
        page = 0
    max_page = max(0, (total - 1) // page_size) if total else 0
    page = min(page, max_page)
    start = page * page_size
    page_files = files[start : start + page_size]
    for entry in page_files:
        entry.category_label = _content_category_label(entry.content_category, entry.path)
        entry.browser_url = _browser_url_for_file(entry)

    query = {"classification": classification}
    if storage_id:
        query["storage"] = storage_id

    context = {
        **navigation_context("orphans"),
        "latest_scan": _latest_result_scan(),
        "classification_value": classification,
        "classification_label": FileInventory.Classification(classification).label,
        "files": page_files,
        "total": total,
        "page": page,
        "has_prev": page > 0,
        "has_next": start + page_size < total,
        "start_index": start + 1 if total else 0,
        "end_index": min(start + page_size, total),
        "prev_query": urlencode({**query, "page": page - 1}),
        "next_query": urlencode({**query, "page": page + 1}),
    }
    return render(request, "core/classified_files.html", context)


def _browser_url_for_file(entry: FileInventory) -> str:
    """Link to the storage browser opened at the file's containing folder."""
    url = reverse("core:storage_browser", args=[entry.storage.storage_id])
    parent = PurePosixPath(entry.path).parent
    parent_str = "" if str(parent) in (".", "") else str(parent)
    if parent_str:
        url = f"{url}?{urlencode({'path': parent_str})}"
    return url


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

    record_audit_event(
        request,
        action="trash.purge.schedule.updated",
        object_type="trash_purge_schedule",
        object_id="automatic-trash-purge",
        details={
            "enabled": state.enabled,
            "max_age_days": state.max_age_days,
            "next_run": state.next_run.isoformat() if state.next_run else "",
        },
    )

    return redirect("core:dashboard")


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


def _latest_storage_result_scan(storage: StorageMount) -> ScanRun | None:
    return (
        ScanRun.objects.filter(status=ScanRun.Status.COMPLETED)
        .exclude(queued_task_id="content-preflight")
        .filter(Q(target_storage=storage) | Q(target_storage__isnull=True))
        .order_by(F("filesystem_scan_at").desc(nulls_last=True), F("finished_at").desc(nulls_last=True), "-created_at")
        .first()
    )


def _latest_storage_content_scan(storage: StorageMount) -> ScanRun | None:
    return (
        ScanRun.objects.filter(status=ScanRun.Status.COMPLETED)
        .filter(Q(target_storage=storage) | Q(target_storage__isnull=True))
        .order_by(F("filesystem_scan_at").desc(nulls_last=True), F("finished_at").desc(nulls_last=True), "-created_at")
        .first()
    )


def _decorate_storage_with_space_info(storage: StorageMount) -> None:
    storage.space_info = common.storage_space_info(storage.path)
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
    record_audit_event(
        request,
        action=action,
        object_type="file",
        object_id=f"{storage.storage_id}:{path}",
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
    common.async_task(
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
        "app_internal": "App internal",
        "backup": "Backups",
        "base_image": "Base image",
        "ct_private": "CT private data",
        "ct_template": "CT templates",
        "import_content": "Import content",
        "import_directory": "Import content",
        "iso": "ISO images",
        "snippet": "Snippets",
        "template_directory": "Templates",
        "trash": "Trash",
        "vm_disk": "VM disk",
        "vm_image_directory": "VM image directory",
        "vm_images": "VM images",
    }
    return labels.get(category, "Other / unknown")


def _trash_purge_schedule_state():
    return trash_purge_schedule_state()

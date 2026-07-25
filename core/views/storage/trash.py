"""Recycle Bin: trash listing, restore, purge and the purge schedule."""

from __future__ import annotations

from django.template.defaultfilters import filesizeformat

from core.models import (
    CurrentGuestInventory,
)

from ..common import (
    FileInventory,
    PermissionDenied,
    StorageActionError,
    StorageMount,
    TrashItem,
    _safe_next_url,
    adopt_discovered_trash_items,
    app_login_required,
    cleanup_empty_app_trash_directories,
    get_object_or_404,
    is_nfs_silly_rename_path,
    messages,
    navigation_context,
    purge_trash_item_action,
    record_audit_event,
    redirect,
    render,
    require_POST,
    restore_trash_item,
    settings,
    tz,
    update_trash_purge_schedule,
)
from ._shared import (
    _GUESTS_SHOWN_IN_CONFIRM,
    _audit_file_action,
    _audit_file_action_failure,
    _decorate_storage_with_space_info,
    _latest_storage_result_scan,
    _mount_or_404,
    _parent_path,
    _refresh_latest_storage_directory,
    _storage_browser_url,
    _storage_write_disabled_response,
)


@app_login_required
def storage_trash(request, storage_id: str):
    storage = _mount_or_404(storage_id)
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
            mount=storage,
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
        **navigation_context("datastore", page_title=(storage.display_name, "Trash")),
        "storage": storage,
        "files_base_url": _storage_browser_url(storage),
        "items": _trash_rows(storage, items),
    }
    return render(request, "core/storage_trash.html", context)


def _trash_rows(storage: StorageMount, items: list[TrashItem]) -> list[dict[str, object]]:
    """Trash entries with the facts a permanent delete has to state.

    Purging is the only genuinely irreversible file operation in the app, and it
    had the weakest guard of any of them. What matters at that moment is what the
    file was, how long it has been recoverable, and whether a guest configuration
    still points at it — a still-referenced disk means restoring is the only way
    back for that guest.
    """
    bindings = list(
        storage.cluster_bindings.select_related("cluster_storage__cluster").filter(
            cluster_storage__cluster__retired_at__isnull=True,
            cluster_storage__unmanaged_at__isnull=True,
        )
    )
    references: dict[str, list[str]] = {}
    if bindings:
        clusters = {binding.cluster_storage.cluster_id: binding.cluster_storage.cluster for binding in bindings}
        guests = list(
            CurrentGuestInventory.objects.filter(cluster_id__in=clusters).only(
                "object_type", "vmid", "status", "disk_references", "cluster_id"
            )
        )
        for item in items:
            relative = str(item.original_path).lstrip("/").removeprefix("images/")
            volids = {f"{binding.cluster_storage.storage_id}:{relative}" for binding in bindings}
            references[item.trash_path] = sorted(
                f"{guest.object_type}:{guest.vmid} ({guest.status or 'unknown'})"
                for guest in guests
                if any(str(ref) in volids for ref in guest.disk_references or [])
            )

    now = tz.now()
    rows = []
    for item in items:
        size = (item.metadata or {}).get("original_size_bytes")
        facts = [f"original path {item.original_path}"]
        if isinstance(size, int):
            facts.append(f"{filesizeformat(size)}")
        if item.moved_at:
            days = max(0, (now - item.moved_at).days)
            facts.append(f"recoverable here for {days} day(s)")
        referencing = references.get(item.trash_path) or []
        if referencing:
            shown = ", ".join(referencing[:_GUESTS_SHOWN_IN_CONFIRM])
            hidden = len(referencing) - _GUESTS_SHOWN_IN_CONFIRM
            if hidden > 0:
                shown += f", and {hidden} more"
            facts.append(f"still referenced by {len(referencing)} guest config(s): {shown}")
        summary = "; ".join(facts)
        rows.append(
            {
                "item": item,
                "confirm": (
                    f"Permanently delete this file? {summary}. "
                    "This deletes it from disk immediately and cannot be undone."
                ),
                "confirm_second": f"Are you really sure? This cannot be undone. {summary}.",
            }
        )
    return rows


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
        _audit_file_action_failure(
            request,
            action="file.restored",
            storage=item.mount,
            path=item.original_path,
            exc=exc,
        )
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
        _audit_file_action_failure(
            request,
            action="file.purged",
            storage=item.mount,
            path=item.original_path,
            exc=exc,
            details={"trash_item": item.id, "trash_path": item.trash_path},
        )
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

"""Recycle Bin: trash listing, restore, purge and the purge schedule."""

from __future__ import annotations

from django.db.models import Count, Q

from ..common import (
    FileInventory,
    PermissionDenied,
    StorageActionError,
    StorageMount,
    TrashItem,
    _safe_next_url,
    app_login_required,
    get_object_or_404,
    messages,
    navigation_context,
    purge_trash_item_action,
    record_audit_event,
    redirect,
    render,
    require_POST,
    restore_trash_item,
    settings,
    update_trash_purge_schedule,
)
from ._shared import (
    _audit_file_action,
    _audit_file_action_failure,
    _mount_or_404,
    _parent_path,
    _recycle_bin_rows,
    _refresh_latest_storage_directory,
    _storage_browser_url,
    _storage_recycle_bin_url,
    _storage_write_disabled_response,
)


@app_login_required
def storage_trash(request, storage_id: str):
    storage = _mount_or_404(storage_id)
    context = {
        **navigation_context("datastore", page_title=(storage.display_name, "Trash")),
        "storage": storage,
        "trash_mount": storage,
        "files_base_url": _storage_browser_url(storage),
        "items": _recycle_bin_rows(storage),
    }
    return render(request, "core/storage_trash.html", context)


@app_login_required
def recycle_bins(request):
    """Passive landing page for the mount-scoped Recycle Bins."""
    mounts = list(
        StorageMount.objects.filter(enabled=True)
        .annotate(
            recycle_bin_item_count=Count(
                "trash_items",
                filter=Q(trash_items__restore_status=TrashItem.RestoreStatus.TRASHED),
            )
        )
        .order_by("display_name", "mount_key")
    )
    for mount in mounts:
        mount.recycle_bin_url = _storage_recycle_bin_url(mount)
    context = {
        **navigation_context("dashboard", page_title="Recycle Bins"),
        "mounts": mounts,
        "total_items": sum(mount.recycle_bin_item_count for mount in mounts),
    }
    return render(request, "core/recycle_bins.html", context)


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

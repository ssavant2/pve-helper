"""Helpers used by more than one storage domain module.

A helper belongs to the module that owns it; this is only for the ones two or
more domains genuinely share, so a domain module never imports a sibling.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from django.db.models import Count
from django.template.defaultfilters import filesizeformat

from core.models import CurrentGuestInventory, TrashItem
from core.services.cluster_scopes import managed_clusters
from core.services.datastore_nav import datastore_url
from core.services.storage_mounts import (
    mount_datastore_scope,
    registered_mount_health,
    resolve_storage_mount,
)
from core.services.task_failures import failure_fields

from .. import common
from ..common import (
    MIN_INFLATE_ALLOCATED_PERCENT,
    F,
    FileInventory,
    Http404,
    HttpResponseForbidden,
    Q,
    ScanRun,
    StorageActionError,
    StorageMount,
    adopt_discovered_trash_items,
    cleanup_empty_app_trash_directories,
    file_action_risk,
    full_inflate_already_recorded,
    is_nfs_silly_rename_path,
    record_audit_event,
    refresh_storage_directory,
    reverse,
    storage_details,
    urlencode,
)

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

_GUESTS_SHOWN_IN_CONFIRM = 6


def _clusters_for_mounts(mount_ids):
    return list(
        managed_clusters()
        .filter(enabled=True)
        .filter(
            Q(storage_consumers__storage_id__in=mount_ids)
            | Q(
                storage_definitions__mount_bindings__mount_id__in=mount_ids,
                storage_definitions__unmanaged_at__isnull=True,
            )
        )
        .distinct()
        .order_by("display_name", "key")
    )


def _storage_clusters(storage: StorageMount):
    return _clusters_for_mounts([storage.pk])


def _lineage_by_cluster(clusters=None) -> dict[str, dict[int, int]]:
    """Linked-clone lineage per cluster, narrowed to `clusters` when the caller
    knows which ones can own the volumes it is about to reason over.

    Lineage is keyed on VMID, and a VMID identifies a guest only within its own
    cluster. Merging every cluster's lineage into one map is therefore not a
    superset of the truth — it is a different, wrong map, in which two unrelated
    templates that happen to share a VMID answer for each other.
    """
    if clusters is None:
        clusters = managed_clusters().filter(enabled=True).order_by("key")
    return {cluster.key: common.stored_guest_lineage(cluster) for cluster in clusters}


def _mount_or_404(reference: str, *, enabled: bool = True) -> StorageMount:
    try:
        return resolve_storage_mount(reference, enabled=enabled)
    except StorageMount.DoesNotExist as exc:
        raise Http404("Storage mount not found.") from exc


def _ordered_storage_content(values: list[str], current_content: list[str]) -> list[str]:
    requested = {value for value in values if value}
    known = [key for key in STORAGE_CONTENT_ORDER if key in requested]
    unknown = sorted(key for key in requested if key in current_content and key not in STORAGE_CONTENT_ORDER)
    return known + unknown


def _classification_counts(queryset) -> dict[str, int]:
    return {
        item["classification"]: item["count"]
        for item in queryset.values("classification").order_by().annotate(count=Count("id"))
    }


def _latest_storage_result_scan(storage: StorageMount) -> ScanRun | None:
    return (
        ScanRun.objects.filter(status=ScanRun.Status.COMPLETED)
        .exclude(queued_task_id="content-preflight")
        .filter(Q(target_storage=storage) | Q(target_storage__isnull=True))
        .order_by(F("filesystem_scan_at").desc(nulls_last=True), F("finished_at").desc(nulls_last=True), "-created_at")
        .first()
    )


def _decorate_storage_with_space_info(storage: StorageMount) -> None:
    storage.space_info = common.storage_space_info(storage)
    storage.mount_health = registered_mount_health(storage)
    storage.storage_actions_enabled = storage.mount_health.available and storage.mount_health.writable
    storage.details = storage_details(storage, _latest_storage_result_scan(storage), storage.space_info)


def _refresh_latest_storage_directory(storage: StorageMount, directory_path: str = "") -> None:
    latest_scan = _latest_storage_result_scan(storage)
    if latest_scan is None:
        return
    refresh_storage_directory(storage=storage, scan=latest_scan, directory_path=directory_path)


def _storage_browser_url(storage: StorageMount, path: str = "", **params: object) -> str:
    scope = mount_datastore_scope(storage)
    if scope is None:
        return reverse("core:settings_storage")
    url = datastore_url("core:api_storage_files", *scope)
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


def _storage_recycle_bin_url(storage: StorageMount) -> str:
    """Open a mount's bin through its datastore when it still has a binding.

    An unbound mount can briefly remain while an operator is changing storage
    access. Its mount-scoped Recycle Bin is still the only route from which its
    files can be recovered, so that route is the deliberate fallback.
    """
    scope = mount_datastore_scope(storage)
    if scope is None:
        return reverse("core:storage_trash", args=[storage.mount_ref])
    return datastore_url("core:api_storage_recycle_bin", *scope)


def _recycle_bin_rows(storage: StorageMount) -> list[dict[str, object]]:
    """Current recoverable items for a mount, including existing reconciliation.

    Both the legacy mount route and the datastore tab use this function so opening
    the bin has exactly the same adoption and cleanup behaviour through either
    navigation path. The global overview deliberately does not call it; listing
    bins must remain a passive database read.
    """
    _decorate_storage_with_space_info(storage)
    latest_scan = _latest_storage_result_scan(storage)
    if common.settings.STORAGE_WRITE_ENABLED and storage.storage_actions_enabled:
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
    visible = [
        item
        for item in items
        if not is_nfs_silly_rename_path(item.original_path) and not is_nfs_silly_rename_path(item.trash_path)
    ]
    return _trash_rows(storage, visible)


def _trash_rows(storage: StorageMount, items: list[TrashItem]) -> list[dict[str, object]]:
    """Trash entries with the facts a permanent delete has to state."""
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

    now = common.tz.now()
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


def _audit_file_action(
    request,
    *,
    action: str,
    storage: StorageMount,
    path: str,
    details: dict[str, object],
    outcome: str = "success",
    unverified_nodes: tuple[str, ...] = (),
) -> None:
    if unverified_nodes:
        details = {**details, "unverified_nodes": list(unverified_nodes)}
    record_audit_event(
        request,
        action=action,
        object_type="file",
        object_id=f"{storage.mount_ref}:{path}",
        outcome=outcome,
        details={
            "storage_id": storage.storage_id,
            "mount_ref": storage.mount_ref,
            "storage_name": storage.display_name,
            "path": path,
            **details,
        },
    )


def _audit_file_action_failure(
    request,
    *,
    action: str,
    storage: StorageMount,
    path: str,
    exc: Exception,
    details: dict[str, object] | None = None,
) -> None:
    """Record a file action that was refused or failed.

    A refusal is evidence too, and often the more interesting kind: it is what
    an operator saw when they were told they could not do something, and what a
    later "why is this file still here" question needs answered. Leaving it out
    made Recent Tasks and Audit agree only about the successes, which is the one
    case nobody investigates.

    The stored reason is the stable public message the operator was shown, never
    a raw exception string - the same boundary every other surface holds.
    """
    _audit_file_action(
        request,
        action=action,
        storage=storage,
        path=path,
        details={
            **(details or {}),
            **failure_fields(exc, operation=f"file_action.{action}", fallback="The file action failed."),
        },
        outcome="failed",
    )


def _storage_write_disabled_response() -> HttpResponseForbidden:
    return HttpResponseForbidden("Storage write actions are disabled.")


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
        entry.qcow2_allocation_percent is not None and entry.qcow2_allocation_percent >= MIN_INFLATE_ALLOCATED_PERCENT
    )
    entry.full_inflate_already_recorded = (
        entry.entry_type == FileInventory.EntryType.FILE
        and full_inflate_already_recorded(
            entry,
            current_virtual_size_bytes=entry.virtual_size_bytes if isinstance(entry.virtual_size_bytes, int) else None,
        )
    )
    entry.has_thin_usage = (
        entry.disk_size_bytes is not None
        and entry.virtual_size_bytes is not None
        and entry.disk_size_bytes != entry.virtual_size_bytes
    )
    entry.action_risk = file_action_risk(entry)
    entry.inflate_action_risk = file_action_risk(entry, block_running_guests=False)
    entry.can_trash = (
        entry.entry_type in {FileInventory.EntryType.FILE, FileInventory.EntryType.DIRECTORY}
        and not entry.action_risk.blocked
    )
    entry.can_rename = entry.entry_type == FileInventory.EntryType.FILE and entry.can_trash
    entry.can_inflate_action = (
        entry.entry_type == FileInventory.EntryType.FILE and not entry.inflate_action_risk.blocked
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
    entry.action_blocked = (
        entry.entry_type in {FileInventory.EntryType.FILE, FileInventory.EntryType.DIRECTORY}
        and entry.action_risk.blocked
    )
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
        "import_disk": "Import disk",
        "import_manifest": "OVF manifest",
        "import_package": "OVA/OVF package",
        "iso": "ISO images",
        "snippet": "Snippets",
        "template_directory": "Templates",
        "trash": "Trash",
        "vm_disk": "VM disk",
        "vm_image_directory": "VM image directory",
        "vm_images": "VM images",
    }
    return labels.get(category, "Other / unknown")


def _int_request_param(request, name: str, default: int) -> int:
    try:
        return int(request.GET.get(name, default))
    except (TypeError, ValueError):
        return default

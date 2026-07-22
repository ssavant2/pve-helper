from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import UploadedFile
from django.utils import timezone

from core.models import AuditEvent, FileInventory, ProxmoxCluster, StorageMount, TrashItem
from core.services.cluster_resolver import ClusterResolutionError, cluster_wide_read
from core.services.confined_filesystem import (
    ConfinedCrossDeviceError,
    ConfinedFilesystemError,
    ConfinedPathExistsError,
    confined_directory,
    confined_directory_free_bytes,
    confined_entry_stat,
    copy_regular_file_noreplace,
    create_confined_directories,
    create_directory_noreplace,
    create_regular_file_exclusive,
    list_confined_directory,
    remove_confined_empty_directory,
    remove_confined_file,
    remove_confined_tree,
    rename_entry_noreplace,
    rename_regular_file_noreplace,
    set_confined_owner_and_mode,
)
from core.services.file_actions import ReferencedObject, file_action_risk, guest_objects_for_entry
from core.services.image_info import probe_qemu_image_info, qemu_img_failure_cause
from core.services.public_errors import PublicMessageError
from core.services.storage_catalog import StorageCatalogChanged, StorageOperationScope, UsageState
from core.services.storage_mounts import (
    registered_mount_health,
    resolve_storage_mount,
)
from core.services.storage_paths import (
    storage_mount_root,
    storage_trash_root,
)

logger = logging.getLogger(__name__)


class StorageActionError(PublicMessageError, Exception):
    pass


class StorageOperationAborted(StorageActionError):
    """The published generation moved, so the remaining objects were not attempted."""


@dataclass(frozen=True)
class InflatePreflight:
    """What the inflate preflight established, with each fact in its own field.

    This used to be a `dict[str, object]`, which forced the caller to re-narrow
    every value it read back (`str(...)`, `isinstance(..., Path)`) and put the
    interpreter to execute in the same container as the operator-supplied image
    path — so anything reading the dict inherited the path's provenance whether it
    touched the path or not. The executable comes from `shutil.which`; the path
    comes from a request. They are not the same kind of value and no longer travel
    together.
    """

    qemu_img: str
    root: Path
    relative_path: str
    virtual_size_bytes: int
    disk_size_bytes: int
    target_preallocation: str
    free_bytes: int


def public_storage_upload_error(exc: StorageActionError) -> str:
    """Map internal upload failures to an explicit user-safe message set."""
    message = str(exc)
    exact_messages = {
        "Storage path is not available.": "Storage path is not available.",
        "PVE-helper storage mount is read-only.": "PVE-helper storage mount is read-only.",
        "Invalid upload filename.": "Invalid upload filename.",
        "Target directory does not exist.": "Target directory does not exist.",
        "Target file already exists.": "Target file already exists.",
        "Storage write failed.": "Storage write failed.",
        "No upload files selected.": "No upload files selected.",
        "Folder upload metadata is incomplete.": "Folder upload metadata is incomplete.",
        "Invalid folder upload path.": "Invalid folder upload path.",
        "Folder upload contains duplicate file paths.": "Folder upload contains duplicate file paths.",
        "Folder upload failed.": "Folder upload failed.",
    }
    if message in exact_messages:
        return exact_messages[message]
    if message.startswith("Upload exceeds "):
        return "Upload exceeds the configured size limit."
    if message.startswith("Folder upload exceeds "):
        return "Folder upload exceeds the configured size limit."
    return "The upload could not be completed."


MIN_INFLATE_HEADROOM_BYTES = 256 * 1024 * 1024
MIN_INFLATE_ALLOCATED_PERCENT = 95
INFLATE_PREALLOCATION_METADATA = "metadata"
INFLATE_PREALLOCATION_FULL = "full"
INFLATE_PREALLOCATION_MODES = {INFLATE_PREALLOCATION_METADATA, INFLATE_PREALLOCATION_FULL}


def full_inflate_already_recorded(
    entry: FileInventory,
    *,
    current_virtual_size_bytes: int | None = None,
) -> bool:
    event = (
        AuditEvent.objects.filter(
            action="file.inflated",
            outcome="success",
            storage_id=entry.storage.storage_id,
            path=entry.path,
            target_preallocation=INFLATE_PREALLOCATION_FULL,
        )
        .order_by("-timestamp")
        .first()
    )
    if event is None:
        return False
    details = event.details if isinstance(event.details, dict) else {}
    after = details.get("after") if isinstance(details.get("after"), dict) else {}
    recorded_virtual_size = after.get("virtual_size_bytes")
    if isinstance(current_virtual_size_bytes, int) and isinstance(recorded_virtual_size, int):
        return current_virtual_size_bytes <= recorded_virtual_size
    if entry.modified_at is not None and event.timestamp < entry.modified_at:
        return False
    return True


def require_live_guest_stopped(entry: FileInventory) -> list[dict[str, object]]:
    guests = [
        guest
        for guest in guest_objects_for_entry(entry)
        if guest.vmid is not None and guest.object_type in {"vm", "ct"}
    ]
    if not guests:
        return []

    checked: list[dict[str, object]] = []
    for guest in guests:
        status = _live_guest_status(guest).strip().lower()
        if status != "stopped":
            raise StorageActionError(
                f"{_guest_label(guest)} is {status or 'unknown'}. "
                "Stop it manually in Proxmox before running this file action."
            )
        checked.append(
            {
                "object_type": guest.object_type,
                "vmid": guest.vmid,
                "node": guest.node,
                "status": status,
            }
        )
    return checked


def _live_guest_status(guest: ReferencedObject) -> str:
    # File-action safety checks must bypass the display cache used by the VMs/CTs tab.
    guest_ref = guest.guest_ref
    if guest_ref is None:
        raise StorageActionError(
            f"Could not verify cluster identity for {_guest_label(guest)}. "
            "The file action is blocked until the inventory reference is cluster-qualified."
        )

    cluster = ProxmoxCluster.objects.filter(key=guest_ref.cluster_key).first()
    if cluster is None:
        raise StorageActionError(
            f"Could not resolve cluster '{guest_ref.cluster_key}' for {_guest_label(guest)}. "
            "The file action is blocked until the guest can be confirmed stopped."
        )

    try:
        result = cluster_wide_read(
            cluster,
            operation="storage_file_guest_status",
            call=lambda client: client.guest_status(
                node=guest_ref.node,
                object_type=guest_ref.object_type,
                vmid=guest_ref.vmid,
            ),
        )
    except ClusterResolutionError as exc:
        raise StorageActionError(
            f"Could not verify live Proxmox status for {_guest_label(guest)}. "
            "The file action is blocked until the guest can be confirmed stopped."
        ) from exc

    if result.complete:
        return str(result.value)

    detail = f" Last error: {result.errors[-1]}" if result.errors else ""
    raise StorageActionError(
        f"Could not verify live Proxmox status for {_guest_label(guest)}. "
        f"The file action is blocked until the guest can be confirmed stopped.{detail}"
    )


def _guest_label(guest: ReferencedObject) -> str:
    kind = "VM" if guest.object_type == "vm" else "CT"
    name = f" ({guest.name})" if guest.name else ""
    return f"{kind} {guest.vmid}{name} on {guest.node}"


def require_storage_write_enabled() -> None:
    if not settings.STORAGE_WRITE_ENABLED:
        raise PermissionDenied("Storage write actions are disabled.")


def require_storage_write_access(storage: StorageMount) -> None:
    require_storage_write_enabled()
    health = registered_mount_health(storage)
    if not health.available:
        raise StorageActionError(health.reason or "Storage path is not available.")
    if not health.writable:
        raise StorageActionError(health.reason or "PVE-helper storage mount is read-only.")


def upload_to_storage(
    *,
    storage: StorageMount,
    directory_path: str,
    uploaded_file: UploadedFile,
) -> dict[str, object]:
    require_storage_write_access(storage)
    filename = _safe_upload_filename(uploaded_file.name)
    max_bytes = _upload_max_bytes()
    if max_bytes and uploaded_file.size and uploaded_file.size > max_bytes:
        raise StorageActionError(f"Upload exceeds {settings.STORAGE_UPLOAD_MAX_SIZE_MB} MB.")

    root = _storage_root(storage)
    directory_relative = _normalize_relative_path(directory_path)
    _require_confined_directory(directory_relative, root=root)
    target_relative = _join_relative(directory_path, filename)
    # Reported early so a large upload is not spent before the collision is
    # found; the publishing rename below is what actually refuses to overwrite.
    if confined_entry_stat(root, target_relative) is not None:
        raise StorageActionError("Target file already exists.")

    temp_relative = _join_relative(directory_relative, f".pve-helper-upload-{uuid.uuid4().hex}.part")
    written = 0
    try:
        handle = create_regular_file_exclusive(root, temp_relative)
    except ConfinedFilesystemError as exc:
        raise StorageActionError("Storage write failed.") from exc
    try:
        with handle:
            written = _write_upload(handle, uploaded_file, max_bytes)
        rename_entry_noreplace(root, temp_relative, target_relative, expected="file", create_target_parents=False)
    except ConfinedPathExistsError as exc:
        remove_confined_file(root, temp_relative, missing_ok=True)
        raise StorageActionError("Target file already exists.") from exc
    except StorageActionError:
        remove_confined_file(root, temp_relative, missing_ok=True)
        raise
    except (ConfinedFilesystemError, OSError) as exc:
        remove_confined_file(root, temp_relative, missing_ok=True)
        raise StorageActionError("Storage write failed.") from exc

    return {
        "path": _join_relative(directory_path, filename),
        "size_bytes": written,
    }


def upload_folder_to_storage(
    *,
    storage: StorageMount,
    directory_path: str,
    uploaded_files: list[UploadedFile],
    relative_paths: list[str],
) -> dict[str, object]:
    require_storage_write_access(storage)
    if not uploaded_files:
        raise StorageActionError("No upload files selected.")
    if len(uploaded_files) != len(relative_paths):
        raise StorageActionError("Folder upload metadata is incomplete.")

    root = _storage_root(storage)
    directory_relative = _normalize_relative_path(directory_path)
    _require_confined_directory(directory_relative, root=root)
    max_bytes = _upload_max_bytes()
    plan = _folder_upload_plan(
        directory_path=directory_path,
        uploaded_files=uploaded_files,
        relative_paths=relative_paths,
        root=root,
        max_bytes=max_bytes,
    )

    created_dirs: set[str] = set()
    written_paths: list[str] = []
    temp_relative: str | None = None
    try:
        for item in plan:
            target_relative = str(item["relative_path"])
            parent_relative = _parent_relative(target_relative)
            if parent_relative != directory_relative:
                created_dirs.update(_missing_directory_chain(parent_relative, stop_at=directory_relative, root=root))
                create_confined_directories(root, parent_relative)
            temp_relative = _join_relative(parent_relative, f".pve-helper-upload-{uuid.uuid4().hex}.part")
            with create_regular_file_exclusive(root, temp_relative) as handle:
                _write_upload(handle, item["file"], max_bytes)
            rename_entry_noreplace(root, temp_relative, target_relative, expected="file", create_target_parents=False)
            written_paths.append(target_relative)
            temp_relative = None
    except StorageActionError:
        _undo_folder_upload(root, temp_relative, written_paths, created_dirs)
        raise
    except ConfinedPathExistsError as exc:
        _undo_folder_upload(root, temp_relative, written_paths, created_dirs)
        raise StorageActionError("Target file already exists.") from exc
    except (ConfinedFilesystemError, OSError) as exc:
        _undo_folder_upload(root, temp_relative, written_paths, created_dirs)
        raise StorageActionError("Folder upload failed.") from exc

    directory_paths = sorted({_parent_relative(str(item["relative_path"])) for item in plan})
    directory_paths.append(_normalize_relative_path(directory_path))
    return {
        "directory_path": _normalize_relative_path(directory_path),
        "file_count": len(plan),
        "size_bytes": sum(int(item["size_bytes"]) for item in plan),
        "directory_paths": sorted(set(directory_paths)),
        "paths": [str(item["relative_path"]) for item in plan],
    }


def create_storage_directory(
    *,
    storage: StorageMount,
    directory_path: str,
    folder_name: str,
) -> dict[str, object]:
    require_storage_write_access(storage)
    safe_name = _safe_upload_filename(folder_name)
    root = _storage_root(storage)
    directory_relative = _normalize_relative_path(directory_path)
    _require_confined_directory(directory_relative, root=root)

    try:
        create_directory_noreplace(root, _join_relative(directory_path, safe_name))
    except ConfinedPathExistsError as exc:
        raise StorageActionError("Target folder already exists.") from exc
    except ConfinedFilesystemError as exc:
        raise StorageActionError("Folder creation failed.") from exc

    return {
        "path": _join_relative(directory_path, safe_name),
        "directory_path": _normalize_relative_path(directory_path),
    }


def adopt_discovered_trash_items(*, storage: StorageMount, scan) -> int:
    root = _storage_root(storage)
    trash_root_relative = _trash_root_relative(storage, root)
    prefix = f"{trash_root_relative}/"
    discovered = 0
    known_trash_paths = set(
        TrashItem.objects.filter(
            mount=storage,
        ).values_list("trash_path", flat=True)
    )
    entries = FileInventory.objects.filter(
        scan_run=scan,
        storage=storage,
        entry_type=FileInventory.EntryType.FILE,
        path__startswith=prefix,
    ).order_by("path")

    for entry in entries:
        if is_nfs_silly_rename_path(entry.path):
            continue
        if entry.path in known_trash_paths:
            continue
        original_path = _original_path_from_trash_path(entry.path, trash_root_relative)
        if not original_path:
            continue
        TrashItem.objects.create(
            original_path=original_path,
            trash_path=entry.path,
            mount=storage,
            storage_id=storage.storage_id,
            moved_at=entry.modified_at,
            metadata={
                "storage_id": storage.storage_id,
                "mount_ref": storage.mount_ref,
                "storage_name": storage.display_name,
                "original_size_bytes": entry.size_bytes,
                "original_classification": entry.classification,
                "original_content_category": entry.content_category,
                "scan_run": entry.scan_run_id,
                "discovered_from_trash_scan": True,
            },
        )
        known_trash_paths.add(entry.path)
        discovered += 1

    return discovered


def cleanup_empty_app_trash_directories(*, storage: StorageMount) -> int:
    require_storage_write_access(storage)
    root = _storage_root(storage)
    trash_root_relative = _trash_root_relative(storage, root)
    trash_root_stat = confined_entry_stat(root, trash_root_relative)
    if trash_root_stat is None or not stat.S_ISDIR(trash_root_stat.st_mode):
        return 0

    protected_paths = {
        _normalize_relative_path(item.trash_path)
        for item in TrashItem.objects.filter(
            mount=storage,
            restore_status=TrashItem.RestoreStatus.TRASHED,
        )
        if item.trash_path
    }
    removed = 0
    directories = sorted(
        _confined_directory_tree(trash_root_relative, root=root),
        key=lambda value: len(PurePosixPath(value).parts),
        reverse=True,
    )
    for directory in directories:
        if directory in protected_paths:
            continue
        try:
            remove_confined_empty_directory(root, directory, missing_ok=False)
        except ConfinedFilesystemError:
            continue
        removed += 1
    return removed


def move_file_to_trash(
    *,
    storage: StorageMount,
    entry: FileInventory,
    user,
    scope: StorageOperationScope | None = None,
    acknowledged_risk: bool = False,
) -> TrashItem:
    require_storage_write_access(storage)
    if entry.entry_type not in {FileInventory.EntryType.FILE, FileInventory.EntryType.DIRECTORY}:
        raise StorageActionError("Only files and directories can be moved to trash.")
    _require_file_not_blocked(entry, scope=scope, acknowledged_risk=acknowledged_risk)

    root = _storage_root(storage)
    original_relative = _normalize_relative_path(entry.path)
    _require_confined_entry(original_relative, root=root)
    if (
        entry.entry_type == FileInventory.EntryType.DIRECTORY
        and _is_guest_directory(entry.path)
        and list_confined_directory(root, original_relative)
    ):
        raise StorageActionError("Guest image/private directories must be empty before they can be moved to trash.")
    trash_relative = _trash_relative_path(storage, root, entry.path)

    try:
        create_confined_directories(root, _parent_relative(trash_relative))
    except ConfinedFilesystemError as exc:
        raise StorageActionError("Trash directory is not writable.") from exc

    # A trash name that is already taken must lose here rather than replace what
    # holds it: the TrashItem row below would otherwise point at a path whose
    # contents belong to an earlier deletion, and restoring it would put the
    # wrong file back under a real name.
    try:
        rename_entry_noreplace(root, original_relative, trash_relative, create_target_parents=False)
    except ConfinedPathExistsError as exc:
        raise StorageActionError("Trash target already exists.") from exc
    except ConfinedCrossDeviceError as exc:
        raise StorageActionError("Trash target must be on the same filesystem/export.") from exc
    except ConfinedFilesystemError as exc:
        raise StorageActionError("Move to trash failed.") from exc

    return TrashItem.objects.create(
        original_path=entry.path,
        trash_path=trash_relative,
        mount=storage,
        storage_id=storage.storage_id,
        moved_by=user if getattr(user, "is_authenticated", False) else None,
        moved_at=timezone.now(),
        metadata={
            "storage_id": storage.storage_id,
            "mount_ref": storage.mount_ref,
            "storage_name": storage.display_name,
            "original_size_bytes": entry.size_bytes,
            "original_classification": entry.classification,
            "original_content_category": entry.content_category,
            "original_entry_type": entry.entry_type,
            "scan_run": entry.scan_run_id,
        },
    )


def rename_storage_file(
    *,
    storage: StorageMount,
    entry: FileInventory,
    new_name: str,
    scope: StorageOperationScope | None = None,
    acknowledged_risk: bool = False,
) -> dict[str, object]:
    require_storage_write_access(storage)
    if entry.entry_type != FileInventory.EntryType.FILE:
        raise StorageActionError("Only files can be renamed.")
    _require_file_not_blocked(entry, scope=scope, acknowledged_risk=acknowledged_risk)

    safe_name = _safe_upload_filename(new_name)
    root = _storage_root(storage)
    try:
        new_relative_path = rename_regular_file_noreplace(root, entry.path, safe_name)
    except ConfinedPathExistsError as exc:
        raise StorageActionError("Target file already exists.") from exc
    except ConfinedFilesystemError as exc:
        raise StorageActionError("Rename failed.") from exc

    parent_relative = _parent_relative(entry.path)
    return {
        "old_path": _normalize_relative_path(entry.path),
        "new_path": new_relative_path,
        "directory_path": parent_relative,
    }


def move_storage_file(
    *,
    storage: StorageMount,
    entry: FileInventory,
    new_path: str,
    scope: StorageOperationScope | None = None,
    acknowledged_risk: bool = False,
) -> dict[str, object]:
    require_storage_write_access(storage)
    if entry.entry_type != FileInventory.EntryType.FILE:
        raise StorageActionError("Only files can be moved.")
    _require_file_not_blocked(entry, scope=scope, acknowledged_risk=acknowledged_risk)

    old_path = _normalize_relative_path(entry.path)
    target_relative = _normalize_move_target(old_path, new_path)
    if target_relative == old_path:
        raise StorageActionError("Target path is unchanged.")

    root = _storage_root(storage)
    _require_confined_file(old_path, root=root)
    target_stat = confined_entry_stat(root, target_relative)
    if target_stat is not None and stat.S_ISDIR(target_stat.st_mode):
        target_relative = _join_relative(target_relative, PurePosixPath(old_path).name)
    _require_confined_directory(_parent_relative(target_relative), root=root)

    try:
        rename_entry_noreplace(root, old_path, target_relative, expected="file", create_target_parents=False)
    except ConfinedPathExistsError as exc:
        raise StorageActionError("Target file already exists.") from exc
    except ConfinedCrossDeviceError as exc:
        raise StorageActionError("Move target must be on the same filesystem/export.") from exc
    except ConfinedFilesystemError as exc:
        raise StorageActionError("Move failed.") from exc

    return {
        "old_path": old_path,
        "new_path": target_relative,
        "source_directory_path": _parent_relative(old_path),
        "target_directory_path": _parent_relative(target_relative),
    }


def transfer_storage_file(
    *,
    source_storage: StorageMount,
    entry: FileInventory,
    dest_storage: StorageMount,
    dest_directory: str,
    dest_name: str = "",
    keep_source: bool,
    scope: StorageOperationScope | None = None,
    acknowledged_risk: bool = False,
) -> dict[str, object]:
    """Copy (``keep_source``) or move a file to any storage/folder.

    Never overwrites: a destination file with the same name is refused. A move
    within one export is an atomic rename; across exports it falls back to copy
    then delete. Copy leaves the source untouched.
    """
    require_storage_write_access(dest_storage)
    if entry.entry_type != FileInventory.EntryType.FILE:
        raise StorageActionError("Only files can be copied or moved.")
    if not keep_source:
        require_storage_write_access(source_storage)
        _require_file_not_blocked(entry, scope=scope, acknowledged_risk=acknowledged_risk)

    source_root = _storage_root(source_storage)
    source_relative = _normalize_relative_path(entry.path)
    _require_confined_file(source_relative, root=source_root)

    dest_root = _storage_root(dest_storage)
    name = (dest_name or "").strip() or PurePosixPath(source_relative).name
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise StorageActionError("Invalid destination file name.")
    dest_directory = _normalize_relative_path(dest_directory)
    dest_relative = _join_relative(dest_directory, name) if dest_directory else name
    if dest_root == source_root and dest_relative == source_relative:
        raise StorageActionError("Source and destination are the same file.")
    if dest_directory:
        try:
            create_confined_directories(dest_root, dest_directory)
        except ConfinedFilesystemError as exc:
            raise StorageActionError("Could not create the destination folder.") from exc

    def copy_to_destination(failure_message: str) -> None:
        try:
            copy_regular_file_noreplace(
                source_root,
                source_relative,
                dest_relative,
                target_root=dest_root,
                create_target_parents=False,
            )
        except ConfinedPathExistsError as exc:
            raise StorageActionError("A file with that name already exists in the destination.") from exc
        except ConfinedFilesystemError as exc:
            raise StorageActionError(failure_message) from exc

    if keep_source:
        copy_to_destination("Copy failed.")
    else:
        try:
            rename_entry_noreplace(
                source_root,
                source_relative,
                dest_relative,
                target_root=dest_root,
                expected="file",
                create_target_parents=False,
            )
        except ConfinedPathExistsError as exc:
            raise StorageActionError("A file with that name already exists in the destination.") from exc
        except ConfinedCrossDeviceError:
            # Two exports cannot be joined by a rename, so the move becomes copy
            # then delete. The copy still refuses an occupied name, and the
            # source is only unlinked once the copy is complete.
            copy_to_destination("Move failed.")
            try:
                remove_confined_file(source_root, source_relative)
            except ConfinedFilesystemError as exc:
                remove_confined_file(dest_root, dest_relative, missing_ok=True)
                raise StorageActionError("Move failed.") from exc
        except ConfinedFilesystemError as exc:
            raise StorageActionError("Move failed.") from exc

    # Ownership/mode are left as copied; the PVE node reads as root regardless,
    # so (unlike uploads) no root-only chown normalisation is needed here.
    source_relative = _normalize_relative_path(entry.path)
    return {
        "source_path": source_relative,
        "source_directory_path": _parent_relative(source_relative),
        "dest_storage_id": dest_storage.storage_id,
        "dest_path": dest_relative,
        "dest_directory_path": _parent_relative(dest_relative),
        "kept_source": keep_source,
    }


def validate_inflate_storage_file(
    *,
    storage: StorageMount,
    entry: FileInventory,
    target_preallocation: str = INFLATE_PREALLOCATION_FULL,
    validate_owner_locally: bool = True,
    scope: StorageOperationScope | None = None,
    acknowledged_risk: bool = False,
) -> InflatePreflight:
    if target_preallocation not in INFLATE_PREALLOCATION_MODES:
        raise StorageActionError("Unknown inflate target.")

    require_storage_write_access(storage)
    if entry.entry_type != FileInventory.EntryType.FILE:
        raise StorageActionError("Only regular files can be inflated.")
    if entry.content_category != "vm_disk":
        raise StorageActionError("Only VM qcow2 disk images can be inflated.")
    # Inflate rewrites the disk in place under the same volid, so the guest that
    # owns it must be stopped rather than gone.
    _require_file_not_blocked(
        entry,
        block_running_guests=False,
        relocates_file=False,
        scope=scope,
        acknowledged_risk=acknowledged_risk,
    )

    qemu_img = shutil.which("qemu-img")
    if not qemu_img:
        raise StorageActionError("qemu-img is not installed in the pve-helper container.")

    root = _storage_root(storage)
    image_relative = _normalize_relative_path(entry.path)
    image_stat = _require_confined_file(image_relative, root=root)
    if validate_owner_locally:
        _require_inflate_owner_preservable(image_stat)
    image_info = _probe_confined_image(
        root=root,
        relative_path=image_relative,
        entry=entry,
    )
    if image_info.get("error"):
        logger.warning("qemu-img info failed: entry=%s error=%s", entry.pk, image_info["error"])
        raise StorageActionError("Could not read the disk image with qemu-img.")
    if image_info.get("format") != "qcow2":
        raise StorageActionError("Only qcow2 images can be inflated.")

    virtual_size = image_info.get("virtual_size_bytes")
    disk_size = image_info.get("disk_size_bytes")
    if not isinstance(virtual_size, int) or virtual_size <= 0:
        raise StorageActionError("qemu-img did not report a valid virtual size.")
    if not isinstance(disk_size, int) or disk_size <= 0:
        raise StorageActionError("qemu-img did not report a valid disk size.")
    allocation_percent = image_info.get("qcow2_allocation_percent")
    if not isinstance(allocation_percent, (int, float)):
        allocation_error = image_info.get("qcow2_allocation_error")
        if allocation_error:
            logger.warning("qemu-img check failed: entry=%s error=%s", entry.pk, allocation_error)
            raise StorageActionError("qemu-img could not report the image's qcow2 allocation.")
        raise StorageActionError("qemu-img check did not report qcow2 allocation.")
    if target_preallocation == INFLATE_PREALLOCATION_METADATA and allocation_percent >= MIN_INFLATE_ALLOCATED_PERCENT:
        raise StorageActionError("Disk image already appears to have fully mapped qcow2 clusters.")
    if target_preallocation == INFLATE_PREALLOCATION_FULL and full_inflate_already_recorded(
        entry, current_virtual_size_bytes=virtual_size
    ):
        raise StorageActionError(
            "Disk image has already been full-inflated by pve-helper. "
            "Run a fresh scan after expanding or replacing the image before retrying."
        )

    try:
        free_bytes = confined_directory_free_bytes(root, _parent_relative(image_relative))
    except ConfinedFilesystemError as exc:
        raise StorageActionError("Storage path is not available.") from exc
    expected_target_size = virtual_size if target_preallocation == INFLATE_PREALLOCATION_FULL else disk_size
    required_bytes = expected_target_size + MIN_INFLATE_HEADROOM_BYTES
    if free_bytes < required_bytes:
        raise StorageActionError(
            "Not enough free space to inflate this disk. "
            f"Need at least {required_bytes} bytes free in the image directory."
        )

    return InflatePreflight(
        qemu_img=qemu_img,
        root=root,
        relative_path=image_relative,
        virtual_size_bytes=virtual_size,
        disk_size_bytes=disk_size,
        target_preallocation=target_preallocation,
        free_bytes=free_bytes,
    )


def _inflate_failure_message(stderr: str) -> str:
    """A stable, actionable sentence for a failed `qemu-img convert`.

    The raw output is external, unstructured text carrying host paths: the most
    useful thing to have in the log and the least useful thing to put in a dialog.
    Both branches promise the original file is untouched, which is true at every
    point this can be reached — the rename happens only after the conversion has
    been probed and accepted.
    """
    cause = qemu_img_failure_cause(stderr)
    if cause:
        return f"Inflate failed. {cause} The original file was left unchanged."
    return (
        "Inflate failed, and qemu-img gave no cause pve-helper recognises. The original "
        "file was left unchanged; the raw output is in the application log."
    )


def inflate_storage_file(
    *,
    storage: StorageMount,
    entry: FileInventory,
    target_preallocation: str = INFLATE_PREALLOCATION_FULL,
    acknowledged_risk: bool = False,
) -> dict[str, object]:
    preflight = validate_inflate_storage_file(
        storage=storage,
        entry=entry,
        target_preallocation=target_preallocation,
        validate_owner_locally=True,
        acknowledged_risk=acknowledged_risk,
    )
    qemu_img = preflight.qemu_img
    root = preflight.root
    image_relative = preflight.relative_path
    image_name = PurePosixPath(image_relative).name
    directory_relative = _parent_relative(image_relative)

    token = uuid.uuid4().hex
    temp_name = f".pve-helper-inflate-{token}-{image_name}"
    backup_name = f"{image_name}.pve-helper-backup-{token}"
    temp_relative = _join_relative(directory_relative, temp_name)
    backup_relative = _join_relative(directory_relative, backup_name)

    try:
        # qemu-img needs a path, not a descriptor, so the image directory is
        # pinned open for the whole operation and addressed through /proc/self/fd.
        # Everything below - the convert, both probes, the swap - then works on
        # the directory this process confined, not on a name re-walked each time.
        with confined_directory(root, directory_relative) as image_dir:
            result = subprocess.run(
                [
                    qemu_img,
                    "convert",
                    "-O",
                    "qcow2",
                    "-o",
                    f"preallocation={preflight.target_preallocation}",
                    image_dir.child_path(image_name),
                    image_dir.child_path(temp_name),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=settings.STORAGE_INFLATE_TIMEOUT_SECONDS,
                pass_fds=image_dir.pass_fds,
            )
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                logger.warning(
                    "qemu-img convert failed: storage=%s entry=%s returncode=%s stderr=%s",
                    storage.storage_id,
                    entry.pk,
                    result.returncode,
                    stderr,
                )
                raise StorageActionError(_inflate_failure_message(stderr))

            converted_info = probe_qemu_image_info(
                path=image_dir.child_path(temp_name),
                entry_type=entry.entry_type,
                content_category=entry.content_category,
                pass_fds=image_dir.pass_fds,
            )
        if converted_info.get("format") != "qcow2":
            raise StorageActionError("Converted image is not qcow2.")
        if converted_info.get("virtual_size_bytes") != preflight.virtual_size_bytes:
            raise StorageActionError("Converted image virtual size does not match the original.")
        converted_allocation = converted_info.get("qcow2_allocation_percent")
        if not isinstance(converted_allocation, (int, float)) or converted_allocation < MIN_INFLATE_ALLOCATED_PERCENT:
            raise StorageActionError(
                "qemu-img convert did not produce an image with fully mapped qcow2 clusters; "
                "original file was left unchanged."
            )
        _apply_reference_file_metadata(root=root, source_relative=image_relative, target_relative=temp_relative)
        _swap_inflated_image(
            root=root,
            image_relative=image_relative,
            temp_relative=temp_relative,
            backup_relative=backup_relative,
        )
    except subprocess.TimeoutExpired as exc:
        remove_confined_file(root, temp_relative, missing_ok=True)
        raise StorageActionError("qemu-img convert timed out.") from exc
    except StorageActionError:
        remove_confined_file(root, temp_relative, missing_ok=True)
        raise
    except (ConfinedFilesystemError, OSError) as exc:
        remove_confined_file(root, temp_relative, missing_ok=True)
        raise StorageActionError("Inflate failed.") from exc

    final_info = _probe_confined_image(root=root, relative_path=image_relative, entry=entry)
    return {
        "path": _normalize_relative_path(entry.path),
        "directory_path": _parent_relative(entry.path),
        "target_preallocation": preflight.target_preallocation,
        "before": {
            "virtual_size_bytes": preflight.virtual_size_bytes,
            "disk_size_bytes": preflight.disk_size_bytes,
        },
        "after": final_info,
    }


def restore_trash_item(*, item: TrashItem) -> dict[str, object]:
    if item.restore_status != TrashItem.RestoreStatus.TRASHED:
        raise StorageActionError("Trash item is not restorable.")

    storage = _storage_for_trash_item(item)
    require_storage_write_access(storage)
    root = _storage_root(storage)
    trash_relative = _normalize_relative_path(item.trash_path)
    restore_relative = _normalize_relative_path(item.original_path)
    _require_confined_entry(trash_relative, root=root)

    restore_parent = _parent_relative(restore_relative)
    if restore_parent:
        try:
            create_confined_directories(root, restore_parent)
        except ConfinedFilesystemError as exc:
            raise StorageActionError("Restore directory is not writable.") from exc

    # Restore is the one path where an overwrite would destroy something a guest
    # is actively using: the original name may have been recreated by Proxmox
    # since the file was trashed. The kernel refuses; nothing here retries.
    try:
        rename_entry_noreplace(root, trash_relative, restore_relative, create_target_parents=False)
    except ConfinedPathExistsError as exc:
        raise StorageActionError("Original path already exists.") from exc
    except ConfinedCrossDeviceError as exc:
        raise StorageActionError("Restore target must be on the same filesystem/export.") from exc
    except ConfinedFilesystemError as exc:
        raise StorageActionError("Restore failed.") from exc

    metadata = dict(item.metadata or {})
    metadata["restored_at"] = timezone.now().isoformat()
    item.restore_status = TrashItem.RestoreStatus.RESTORED
    item.metadata = metadata
    item.save(update_fields=["restore_status", "metadata", "updated_at"])
    return {
        "storage": storage,
        "path": item.original_path,
        "trash_path": item.trash_path,
        "entry_type": (item.metadata or {}).get("original_entry_type", FileInventory.EntryType.FILE),
    }


def purge_trash_item(*, item: TrashItem) -> dict[str, object]:
    if item.restore_status != TrashItem.RestoreStatus.TRASHED:
        raise StorageActionError("Trash item is not purgeable.")

    storage = _storage_for_trash_item(item)
    require_storage_write_access(storage)
    root = _storage_root(storage)
    # Deletion is the write that cannot be taken back, so it is the one that must
    # not be aimed by a resolved path: `shutil.rmtree` follows symlinks while
    # walking, and a component swapped after the containment check would send it
    # outside the export entirely. Every level here is entered by descriptor.
    try:
        remove_confined_tree(root, _normalize_relative_path(item.trash_path))
    except ConfinedFilesystemError as exc:
        raise StorageActionError("Failed to delete the trashed file.") from exc

    item.restore_status = TrashItem.RestoreStatus.PURGED
    item.save(update_fields=["restore_status", "updated_at"])
    return {
        "storage": storage,
        "path": item.original_path,
        "trash_path": item.trash_path,
    }


def normalize_uploaded_proxmox_image_paths(
    *,
    storage: StorageMount,
    paths: list[str],
) -> dict[str, object]:
    root = _storage_root(storage)
    normalized: list[str] = []
    skipped: list[str] = []
    for relative_path in paths:
        path = _normalize_relative_path(relative_path)
        if not _is_proxmox_image_upload_path(path):
            skipped.append(path)
            continue

        file_stat = confined_entry_stat(root, path)
        if file_stat is None or not stat.S_ISREG(file_stat.st_mode):
            skipped.append(path)
            continue

        _apply_proxmox_upload_metadata(root, _parent_relative(path), is_directory=True)
        _apply_proxmox_upload_metadata(root, path, is_directory=False)
        normalized.append(path)

    return {
        "normalized": normalized,
        "skipped": skipped,
    }


def _require_file_not_blocked(
    entry: FileInventory,
    *,
    block_running_guests: bool = True,
    relocates_file: bool = True,
    scope: StorageOperationScope | None = None,
    acknowledged_risk: bool = False,
) -> None:
    """Guard a file action with the check that matches what the action does.

    Two different questions live here, and they are not interchangeable:

    * An action that makes the file leave its path — trash, rename, move,
      transfer — breaks any guest still pointing at it. The gate is that a fresh
      catalog read finds nothing referencing it. A stopped guest does not help:
      it breaks on next boot instead of immediately.
    * An action that rewrites the file in place under the same volid — inflate —
      is *for* the guest that owns it, so demanding no references would forbid
      the only case it exists for. The gate there is that the guest is stopped.

    A third question runs underneath both: what does refusing actually ask of the
    operator. "Detach it from the guest in Proxmox first" is only an instruction
    where Proxmox can still be reached and the guest still exists. A node can die
    for good and be replaced by a differently named one, and its guests' configs
    die with it — so a gate that waits for that detach is not being careful, it is
    stranding the file permanently. The same holds for a node that simply did not
    report: unknown must not harden into forbidden.

    `acknowledged_risk` carries the operator's explicit answer to a question that
    named these facts. Both the reference and the unknown yield to it. What does
    not yield is a *reachable* node reporting a guest running on the file: that is
    live breakage rather than an inconvenient unknown, and there the operator has
    somewhere to go — stop the guest.

    `relocates_file` picks between them.
    """
    risk = file_action_risk(entry, block_running_guests=block_running_guests)
    if risk.blocked:
        raise StorageActionError(risk.warning_message)
    if relocates_file and entry.content_category in {"vm_disk", "base_image", "ct_private"}:
        scope = scope or StorageOperationScope()
        bindings = entry.storage.cluster_bindings.select_related("cluster_storage", "cluster_storage__cluster")
        if not bindings.exists():
            if not acknowledged_risk:
                raise StorageActionError(
                    "The storage is not associated with the current API catalog; guest-file safety is unknown."
                )
            logger.warning(
                "Guest-file action proceeding without catalog association: storage=%s path=%s",
                entry.storage.storage_id,
                entry.path,
            )
            return
        relative = str(entry.path).lstrip("/").removeprefix("images/")
        for binding in bindings:
            definition = binding.cluster_storage
            try:
                result = scope.preflight(
                    definition,
                    volid=f"{definition.storage_id}:{relative}",
                    node=binding.node or "",
                )
            except StorageCatalogChanged as exc:
                logger.warning("Storage catalog changed during a file operation: %s", exc)
                raise StorageOperationAborted(
                    "The storage catalog was republished while this operation was running; retry the remaining files."
                ) from exc
            if result.state is UsageState.REFERENCED or result.state is UsageState.REFERENCED_ELSEWHERE:
                if not acknowledged_risk:
                    raise StorageActionError(
                        f"A guest configuration still references this disk: {result.reason} "
                        "Detach it in Proxmox, or confirm that you are breaking that reference "
                        "on purpose."
                    )
                logger.warning(
                    "Guest-file action proceeding over a live guest reference: storage=%s path=%s reason=%s",
                    entry.storage.storage_id,
                    entry.path,
                    result.reason,
                )
                continue
            if result.state is not UsageState.UNREFERENCED:
                # Everything that is neither "referenced" nor "unreferenced" is
                # the catalog saying it could not tell. Refusing that outright is
                # what locked an operator out of a crashed node's disks.
                if not acknowledged_risk:
                    raise StorageActionError(f"Guest-file action blocked by fresh storage preflight: {result.reason}")
                logger.warning(
                    "Guest-file action proceeding on unverified storage evidence: "
                    "storage=%s path=%s state=%s reason=%s",
                    entry.storage.storage_id,
                    entry.path,
                    result.state,
                    result.reason,
                )
        # The fresh preflight is the authority here, and it just confirmed that
        # nothing references the file. Whether some guest happens to be running is
        # then no longer a question about *this* file, and asking it again from
        # scan-derived evidence would only let stale data refuse an action the
        # live catalog permits.
        return
    require_live_guest_stopped(entry)


def _require_inflate_owner_preservable(stat_result: os.stat_result) -> None:
    current_uid = os.geteuid()
    current_gid = os.getegid()
    if current_uid == 0:
        return

    if stat_result.st_uid == current_uid and stat_result.st_gid == current_gid:
        return

    raise StorageActionError(
        "Cannot safely inflate this disk from pve-helper because replacing it would change "
        f"the file owner from UID:GID {stat_result.st_uid}:{stat_result.st_gid} "
        f"to {current_uid}:{current_gid}. Run the inflate manually on a Proxmox node "
        "or use a node-side helper."
    )


def _apply_reference_file_metadata(*, root: Path, source_relative: str, target_relative: str) -> None:
    source_stat = confined_entry_stat(root, source_relative)
    if source_stat is None:
        raise StorageActionError(
            "Cannot preserve original disk ownership and mode on the inflated image; original file was left unchanged."
        )
    try:
        set_confined_owner_and_mode(
            root,
            target_relative,
            uid=source_stat.st_uid,
            gid=source_stat.st_gid,
            mode=stat.S_IMODE(source_stat.st_mode),
            expected="file",
        )
    except ConfinedFilesystemError as exc:
        raise StorageActionError(
            "Cannot preserve original disk ownership and mode on the inflated image; original file was left unchanged."
        ) from exc


def _swap_inflated_image(*, root: Path, image_relative: str, temp_relative: str, backup_relative: str) -> None:
    """Put the converted image in place, keeping the original until it is safe.

    The original is moved aside rather than replaced, so a failure between the
    two renames still has a complete file to put back. If the name has been taken
    in the meantime the restore is refused rather than forced: the backup then
    survives under its own name, which is recoverable, while an overwrite of
    whatever now holds the name would not be.
    """
    rename_entry_noreplace(root, image_relative, backup_relative, expected="file", create_target_parents=False)
    try:
        rename_entry_noreplace(root, temp_relative, image_relative, expected="file", create_target_parents=False)
    except ConfinedFilesystemError:
        try:
            rename_entry_noreplace(root, backup_relative, image_relative, expected="file", create_target_parents=False)
        except ConfinedFilesystemError:
            logger.error(
                "Inflate could not restore the original image; it remains at %s",
                backup_relative,
            )
        raise
    remove_confined_file(root, backup_relative, missing_ok=True)


def _probe_confined_image(*, root: Path, relative_path: str, entry: FileInventory) -> dict[str, object]:
    with confined_directory(root, _parent_relative(relative_path)) as image_dir:
        return probe_qemu_image_info(
            path=image_dir.child_path(PurePosixPath(relative_path).name),
            entry_type=entry.entry_type,
            content_category=entry.content_category,
            pass_fds=image_dir.pass_fds,
        )


def _confined_directory_tree(base_relative: str, *, root: Path) -> list[str]:
    """Every directory beneath a confined base, walked by descriptor."""
    found: list[str] = []
    pending = [base_relative]
    while pending:
        current = pending.pop()
        try:
            entries = list_confined_directory(root, current)
        except ConfinedFilesystemError:
            continue
        for name, entry_stat in entries:
            if not stat.S_ISDIR(entry_stat.st_mode):
                continue
            child = _join_relative(current, name)
            found.append(child)
            pending.append(child)
    return found


def _missing_directory_chain(relative_path: str, *, stop_at: str, root: Path) -> set[str]:
    """The directories a folder upload would have to create, for its own rollback."""
    missing: set[str] = set()
    current = relative_path
    while current and current != stop_at:
        if confined_entry_stat(root, current) is not None:
            break
        missing.add(current)
        current = _parent_relative(current)
    return missing


def _undo_folder_upload(
    root: Path,
    temp_relative: str | None,
    written_paths: list[str],
    created_dirs: set[str],
) -> None:
    if temp_relative:
        remove_confined_file(root, temp_relative, missing_ok=True)
    for path in written_paths:
        remove_confined_file(root, path, missing_ok=True)
    for directory in sorted(created_dirs, key=lambda value: len(PurePosixPath(value).parts), reverse=True):
        remove_confined_empty_directory(root, directory)


def _apply_proxmox_upload_metadata(root: Path, relative_path: str, *, is_directory: bool) -> None:
    # This hands a file to root. `os.chown(path, ...)` would re-walk the name and
    # follow a final symlink, so a swapped component turns a root-owned chmod into
    # a gift to whatever the link points at. The descriptor cannot be redirected.
    try:
        set_confined_owner_and_mode(
            root,
            relative_path,
            uid=0,
            gid=0,
            mode=0o775 if is_directory else 0o664,
            expected="directory" if is_directory else "file",
        )
    except ConfinedFilesystemError as exc:
        raise StorageActionError(
            "Cannot normalize uploaded Proxmox image ownership and mode. "
            "The uploaded file remains in place, but Proxmox may need manual owner/mode repair."
        ) from exc


def _storage_root(storage: StorageMount) -> Path:
    try:
        root = storage_mount_root(storage).resolve(strict=True)
    except OSError as exc:
        raise StorageActionError("Storage path is not available.") from exc
    if not root.is_dir():
        raise StorageActionError("Storage path is not a directory.")
    return root


def _require_confined_directory(relative_path: str, *, root: Path) -> None:
    """Refuse early when a target directory is missing.

    Advisory, like every stat: it produces the useful message instead of a
    generic write failure. It is never what makes the following write safe —
    that is the confined helper's job, in the kernel.
    """
    if not relative_path:
        return
    entry = confined_entry_stat(root, relative_path)
    if entry is None or not stat.S_ISDIR(entry.st_mode):
        raise StorageActionError("Target directory does not exist.")


def _require_confined_file(relative_path: str, *, root: Path) -> os.stat_result:
    entry = confined_entry_stat(root, relative_path)
    if entry is None or not stat.S_ISREG(entry.st_mode):
        raise StorageActionError("File does not exist.")
    return entry


def _require_confined_entry(relative_path: str, *, root: Path) -> os.stat_result:
    entry = confined_entry_stat(root, relative_path)
    if entry is None:
        raise StorageActionError("File or directory does not exist.")
    if not stat.S_ISREG(entry.st_mode) and not stat.S_ISDIR(entry.st_mode):
        raise StorageActionError("Only files and directories can be changed.")
    return entry


def _normalize_relative_path(path: str) -> str:
    path = (path or "").strip().strip("/")
    if not path:
        return ""
    parts = PurePosixPath(path).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise StorageActionError("Invalid storage path.")
    return PurePosixPath(*parts).as_posix()


def _safe_upload_filename(filename: str) -> str:
    filename = (filename or "").strip()
    if not filename or "/" in filename or "\\" in filename or filename in {".", ".."}:
        raise StorageActionError("Invalid upload filename.")
    if PurePosixPath(filename).name != filename:
        raise StorageActionError("Invalid upload filename.")
    return filename


def _safe_folder_upload_path(path: str) -> str:
    raw_path = (path or "").strip().strip("/")
    if not raw_path or "\\" in raw_path:
        raise StorageActionError("Invalid folder upload path.")
    parts = PurePosixPath(raw_path).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise StorageActionError("Invalid folder upload path.")
    return PurePosixPath(*parts).as_posix()


def _is_proxmox_image_upload_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return len(parts) >= 3 and parts[0] == "images" and parts[1].isdigit()


def _folder_upload_plan(
    *,
    directory_path: str,
    uploaded_files: list[UploadedFile],
    relative_paths: list[str],
    root: Path,
    max_bytes: int | None,
) -> list[dict[str, object]]:
    plan: list[dict[str, object]] = []
    seen_paths: set[str] = set()
    total_bytes = 0
    for uploaded_file, relative_path in zip(uploaded_files, relative_paths, strict=True):
        safe_relative = _safe_folder_upload_path(relative_path)
        target_relative = _join_relative(directory_path, safe_relative)
        if target_relative in seen_paths:
            raise StorageActionError("Folder upload contains duplicate file paths.")
        seen_paths.add(target_relative)

        file_size = int(uploaded_file.size or 0)
        if max_bytes and file_size > max_bytes:
            raise StorageActionError(f"Upload exceeds {settings.STORAGE_UPLOAD_MAX_SIZE_MB} MB.")
        total_bytes += file_size
        if max_bytes and total_bytes > max_bytes:
            raise StorageActionError(f"Folder upload exceeds {settings.STORAGE_UPLOAD_MAX_SIZE_MB} MB.")

        # Advisory, so a large folder fails before anything is written. The
        # publishing rename per file is what refuses to overwrite.
        if confined_entry_stat(root, target_relative) is not None:
            raise StorageActionError("Target file already exists.")
        plan.append(
            {
                "file": uploaded_file,
                "relative_path": target_relative,
                "size_bytes": file_size,
            }
        )
    return plan


def _join_relative(directory_path: str, filename: str) -> str:
    directory_path = _normalize_relative_path(directory_path)
    if not directory_path:
        return filename
    return PurePosixPath(directory_path, filename).as_posix()


def _parent_relative(path: str) -> str:
    normalized = _normalize_relative_path(path)
    parent = PurePosixPath(normalized).parent
    if str(parent) == ".":
        return ""
    return parent.as_posix()


def _is_guest_directory(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return parts[:1] in {("images",), ("private",)}


def _normalize_move_target(old_path: str, target_path: str) -> str:
    raw_target = (target_path or "").strip()
    if not raw_target:
        raise StorageActionError("Target path is required.")
    if raw_target.endswith("/"):
        return _join_relative(_normalize_relative_path(raw_target), PurePosixPath(old_path).name)
    return _normalize_relative_path(raw_target)


def _upload_max_bytes() -> int | None:
    max_mb = settings.STORAGE_UPLOAD_MAX_SIZE_MB
    if max_mb <= 0:
        return None
    return max_mb * 1024 * 1024


def _write_upload(handle: BinaryIO, uploaded_file: UploadedFile, max_bytes: int | None) -> int:
    written = 0
    for chunk in uploaded_file.chunks():
        written += len(chunk)
        if max_bytes and written > max_bytes:
            raise StorageActionError(f"Upload exceeds {settings.STORAGE_UPLOAD_MAX_SIZE_MB} MB.")
        handle.write(chunk)
    return written


def _trash_relative_path(storage: StorageMount, root: Path, original_path: str) -> str:
    stamp = timezone.now().strftime("%Y%m%dT%H%M%S%fZ")
    trash_root_relative = _trash_root_relative(storage, root)
    return PurePosixPath(trash_root_relative, stamp, _normalize_relative_path(original_path)).as_posix()


def _trash_root_relative(storage: StorageMount, root: Path) -> str:
    trash_root = storage_trash_root(storage).resolve(strict=False)
    if not trash_root.is_relative_to(root):
        raise StorageActionError("Trash path must be inside the storage root.")
    return trash_root.relative_to(root).as_posix()


def _original_path_from_trash_path(trash_path: str, trash_root_relative: str) -> str:
    try:
        normalized = _normalize_relative_path(trash_path)
    except StorageActionError:
        return ""
    prefix = f"{trash_root_relative}/"
    if not normalized.startswith(prefix):
        return ""
    remainder = normalized[len(prefix) :]
    parts = PurePosixPath(remainder).parts
    if len(parts) < 2:
        return ""
    return PurePosixPath(*parts[1:]).as_posix()


def _storage_for_trash_item(item: TrashItem) -> StorageMount:
    if item.mount_id:
        if item.mount.enabled:
            return item.mount
        raise StorageActionError("Trash item storage is not available.")
    storage_id = item.storage_id or (item.metadata or {}).get("storage_id", "")
    if not storage_id:
        raise StorageActionError("Trash item is missing storage metadata.")
    try:
        return resolve_storage_mount(storage_id, enabled=True)
    except StorageMount.DoesNotExist as exc:
        raise StorageActionError("Trash item storage is not available.") from exc


def is_nfs_silly_rename_path(path: str) -> bool:
    return any(part.startswith(".nfs") for part in PurePosixPath(path).parts)

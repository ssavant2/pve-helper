from __future__ import annotations

import errno
import os
import shutil
import stat
import subprocess
import uuid
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import UploadedFile
from django.utils import timezone

from core.models import AuditEvent, FileInventory, ProxmoxEndpoint, StorageMount, TrashItem
from core.services.confined_filesystem import (
    ConfinedFilesystemError,
    ConfinedPathExistsError,
    rename_regular_file_noreplace,
)
from core.services.file_actions import ReferencedObject, file_action_risk, guest_objects_for_entry
from core.services.filesystem import storage_space_info
from core.services.image_info import probe_qemu_image_info
from core.services.proxmox import ProxmoxAPIError, ProxmoxClient


class StorageActionError(Exception):
    pass


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
    endpoints = _candidate_endpoints_for_node(guest.node)
    if not endpoints:
        raise StorageActionError(
            f"Could not verify live Proxmox status for {_guest_label(guest)}. "
            "The file action is blocked until the guest can be confirmed stopped."
        )

    errors: list[str] = []
    for endpoint in endpoints:
        try:
            return ProxmoxClient(endpoint.url).guest_status(
                node=guest.node,
                object_type=guest.object_type,
                vmid=int(guest.vmid),
            )
        except ProxmoxAPIError as exc:
            errors.append(str(exc))

    detail = f" Last error: {errors[-1]}" if errors else ""
    raise StorageActionError(
        f"Could not verify live Proxmox status for {_guest_label(guest)}. "
        f"The file action is blocked until the guest can be confirmed stopped.{detail}"
    )


def _candidate_endpoints_for_node(node: str) -> list[ProxmoxEndpoint]:
    endpoints = list(ProxmoxEndpoint.objects.filter(enabled=True).order_by("name"))
    matching: list[ProxmoxEndpoint] = []
    fallback: list[ProxmoxEndpoint] = []
    for endpoint in endpoints:
        endpoint_node = str((endpoint.details or {}).get("node") or endpoint.name)
        if endpoint.name == node or endpoint_node == node:
            matching.append(endpoint)
        else:
            fallback.append(endpoint)
    return matching + fallback


def _guest_label(guest: ReferencedObject) -> str:
    kind = "VM" if guest.object_type == "vm" else "CT"
    name = f" ({guest.name})" if guest.name else ""
    return f"{kind} {guest.vmid}{name} on {guest.node}"


def require_storage_write_enabled() -> None:
    if not settings.STORAGE_WRITE_ENABLED:
        raise PermissionDenied("Storage write actions are disabled.")


def require_storage_write_access(storage: StorageMount) -> None:
    require_storage_write_enabled()
    info = storage_space_info(storage.path)
    if not info.ok:
        raise StorageActionError("Storage path is not available.")
    if not info.can_write:
        raise StorageActionError("PVE-helper storage mount is read-only.")


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
    target_dir = _storage_directory(directory_path, root=root)
    target_path = _storage_child_path(_join_relative(directory_path, filename), root=root)
    if target_path.exists():
        raise StorageActionError("Target file already exists.")

    temp_path = target_dir / f".pve-helper-upload-{uuid.uuid4().hex}.part"
    written = 0
    try:
        with temp_path.open("xb") as handle:
            written = _write_upload(handle, uploaded_file, max_bytes)
        temp_path.rename(target_path)
    except StorageActionError:
        temp_path.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
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
    target_root = _storage_directory(directory_path, root=root)
    max_bytes = _upload_max_bytes()
    plan = _folder_upload_plan(
        directory_path=directory_path,
        uploaded_files=uploaded_files,
        relative_paths=relative_paths,
        root=root,
        max_bytes=max_bytes,
    )

    created_dirs: set[Path] = set()
    written_paths: list[Path] = []
    temp_path: Path | None = None
    try:
        for item in plan:
            parent = item["target_path"].parent
            if not parent.is_relative_to(target_root) and parent != target_root:
                raise StorageActionError("Invalid folder upload path.")
            if not parent.exists():
                current = parent
                missing_dirs: list[Path] = []
                while current != target_root and current.is_relative_to(target_root) and not current.exists():
                    missing_dirs.append(current)
                    current = current.parent
                parent.mkdir(parents=True)
                created_dirs.update(missing_dirs)
            temp_path = parent / f".pve-helper-upload-{uuid.uuid4().hex}.part"
            with temp_path.open("xb") as handle:
                _write_upload(handle, item["file"], max_bytes)
            temp_path.rename(item["target_path"])
            written_paths.append(item["target_path"])
            temp_path = None
    except StorageActionError:
        if temp_path:
            temp_path.unlink(missing_ok=True)
        for path in written_paths:
            path.unlink(missing_ok=True)
        _remove_empty_directories(created_dirs, stop_at=target_root)
        raise
    except OSError as exc:
        if temp_path:
            temp_path.unlink(missing_ok=True)
        for path in written_paths:
            path.unlink(missing_ok=True)
        _remove_empty_directories(created_dirs, stop_at=target_root)
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
    target_parent = _storage_directory(directory_path, root=root)
    target_path = _storage_child_path(_join_relative(directory_path, safe_name), root=root)
    if target_path.exists():
        raise StorageActionError("Target folder already exists.")
    if target_path.parent != target_parent:
        raise StorageActionError("Invalid folder path.")

    try:
        target_path.mkdir()
    except OSError as exc:
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
            storage_id=storage.storage_id,
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
            storage_id=storage.storage_id,
            moved_at=entry.modified_at,
            metadata={
                "storage_id": storage.storage_id,
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
    trash_root = _storage_child_path(_trash_root_relative(storage, root), root=root)
    if not trash_root.exists() or not trash_root.is_dir():
        return 0

    protected_paths = {
        _storage_child_path(item.trash_path, root=root)
        for item in TrashItem.objects.filter(
            storage_id=storage.storage_id,
            restore_status=TrashItem.RestoreStatus.TRASHED,
        )
        if item.trash_path
    }
    removed = 0
    directories = sorted(
        (path for path in trash_root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.relative_to(trash_root).parts),
        reverse=True,
    )
    for directory in directories:
        if directory in protected_paths:
            continue
        try:
            directory.rmdir()
        except OSError:
            continue
        removed += 1
    return removed


def move_file_to_trash(
    *,
    storage: StorageMount,
    entry: FileInventory,
    user,
) -> TrashItem:
    require_storage_write_access(storage)
    if entry.entry_type not in {FileInventory.EntryType.FILE, FileInventory.EntryType.DIRECTORY}:
        raise StorageActionError("Only files and directories can be moved to trash.")
    _require_file_not_blocked(entry)

    root = _storage_root(storage)
    original_path = _storage_existing_entry(entry.path, root=root)
    if entry.entry_type == FileInventory.EntryType.DIRECTORY and _is_guest_directory(entry.path) and any(original_path.iterdir()):
        raise StorageActionError("Guest image/private directories must be empty before they can be moved to trash.")
    trash_relative = _trash_relative_path(storage, root, entry.path)
    trash_path = _storage_child_path(trash_relative, root=root)
    try:
        trash_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageActionError("Trash directory is not writable.") from exc
    if trash_path.exists():
        raise StorageActionError("Trash target already exists.")

    try:
        original_path.rename(trash_path)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise StorageActionError("Trash target must be on the same filesystem/export.") from exc
        raise StorageActionError("Move to trash failed.") from exc

    return TrashItem.objects.create(
        original_path=entry.path,
        trash_path=trash_relative,
        storage_id=storage.storage_id,
        moved_by=user if getattr(user, "is_authenticated", False) else None,
        moved_at=timezone.now(),
        metadata={
            "storage_id": storage.storage_id,
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
) -> dict[str, object]:
    require_storage_write_access(storage)
    if entry.entry_type != FileInventory.EntryType.FILE:
        raise StorageActionError("Only files can be renamed.")
    _require_file_not_blocked(entry)

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
) -> dict[str, object]:
    require_storage_write_access(storage)
    if entry.entry_type != FileInventory.EntryType.FILE:
        raise StorageActionError("Only files can be moved.")
    _require_file_not_blocked(entry)

    old_path = _normalize_relative_path(entry.path)
    target_relative = _normalize_move_target(old_path, new_path)
    if target_relative == old_path:
        raise StorageActionError("Target path is unchanged.")

    root = _storage_root(storage)
    original_path = _storage_existing_file(old_path, root=root)
    target_path = _storage_child_path(target_relative, root=root)
    if target_path.exists() and target_path.is_dir():
        target_relative = _join_relative(target_relative, original_path.name)
        target_path = _storage_child_path(target_relative, root=root)
    if target_path.exists():
        raise StorageActionError("Target file already exists.")
    if not target_path.parent.is_dir():
        raise StorageActionError("Target directory does not exist.")

    try:
        original_path.rename(target_path)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise StorageActionError("Move target must be on the same filesystem/export.") from exc
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
        _require_file_not_blocked(entry)

    source_root = _storage_root(source_storage)
    source_path = _storage_existing_file(_normalize_relative_path(entry.path), root=source_root)

    dest_root = _storage_root(dest_storage)
    name = (dest_name or "").strip() or source_path.name
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise StorageActionError("Invalid destination file name.")
    dest_directory = _normalize_relative_path(dest_directory)
    dest_relative = _join_relative(dest_directory, name) if dest_directory else name
    dest_path = _storage_child_path(dest_relative, root=dest_root)
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageActionError("Could not create the destination folder.") from exc
    if dest_path == source_path:
        raise StorageActionError("Source and destination are the same file.")
    if dest_path.exists():
        raise StorageActionError("A file with that name already exists in the destination.")

    if keep_source:
        try:
            shutil.copy2(source_path, dest_path)
        except OSError as exc:
            raise StorageActionError("Copy failed.") from exc
    else:
        try:
            source_path.rename(dest_path)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise StorageActionError("Move failed.") from exc
            try:
                shutil.copy2(source_path, dest_path)
                source_path.unlink()
            except OSError as inner:
                dest_path.unlink(missing_ok=True)
                raise StorageActionError("Move failed.") from inner

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
) -> dict[str, object]:
    if target_preallocation not in INFLATE_PREALLOCATION_MODES:
        raise StorageActionError("Unknown inflate target.")

    require_storage_write_access(storage)
    if entry.entry_type != FileInventory.EntryType.FILE:
        raise StorageActionError("Only regular files can be inflated.")
    if entry.content_category != "vm_disk":
        raise StorageActionError("Only VM qcow2 disk images can be inflated.")
    _require_file_not_blocked(entry, block_running_guests=False)

    qemu_img = shutil.which("qemu-img")
    if not qemu_img:
        raise StorageActionError("qemu-img is not installed in the pve-helper container.")

    root = _storage_root(storage)
    image_path = _storage_existing_file(entry.path, root=root)
    if validate_owner_locally:
        _require_inflate_owner_preservable(image_path)
    image_info = probe_qemu_image_info(
        path=image_path.as_posix(),
        entry_type=entry.entry_type,
        content_category=entry.content_category,
    )
    if image_info.get("error"):
        raise StorageActionError(f"qemu-img info failed: {image_info['error']}")
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
            raise StorageActionError(f"qemu-img check failed: {allocation_error}")
        raise StorageActionError("qemu-img check did not report qcow2 allocation.")
    if (
        target_preallocation == INFLATE_PREALLOCATION_METADATA
        and allocation_percent >= MIN_INFLATE_ALLOCATED_PERCENT
    ):
        raise StorageActionError("Disk image already appears to have fully mapped qcow2 clusters.")
    if (
        target_preallocation == INFLATE_PREALLOCATION_FULL
        and full_inflate_already_recorded(entry, current_virtual_size_bytes=virtual_size)
    ):
        raise StorageActionError(
            "Disk image has already been full-inflated by pve-helper. "
            "Run a fresh scan after expanding or replacing the image before retrying."
        )

    free_bytes = shutil.disk_usage(image_path.parent).free
    expected_target_size = virtual_size if target_preallocation == INFLATE_PREALLOCATION_FULL else disk_size
    required_bytes = expected_target_size + MIN_INFLATE_HEADROOM_BYTES
    if free_bytes < required_bytes:
        raise StorageActionError(
            "Not enough free space to inflate this disk. "
            f"Need at least {required_bytes} bytes free in the image directory."
        )

    return {
        "qemu_img": qemu_img,
        "path": image_path,
        "virtual_size_bytes": virtual_size,
        "disk_size_bytes": disk_size,
        "target_preallocation": target_preallocation,
        "free_bytes": free_bytes,
    }


def inflate_storage_file(
    *,
    storage: StorageMount,
    entry: FileInventory,
    target_preallocation: str = INFLATE_PREALLOCATION_FULL,
) -> dict[str, object]:
    preflight = validate_inflate_storage_file(
        storage=storage,
        entry=entry,
        target_preallocation=target_preallocation,
        validate_owner_locally=True,
    )
    qemu_img = str(preflight["qemu_img"])
    image_path = preflight["path"]
    if not isinstance(image_path, Path):
        raise StorageActionError("Invalid image path.")

    token = uuid.uuid4().hex
    temp_path = image_path.with_name(f".pve-helper-inflate-{token}-{image_path.name}")
    backup_path = image_path.with_name(f"{image_path.name}.pve-helper-backup-{token}")
    if temp_path.exists() or backup_path.exists():
        raise StorageActionError("Temporary inflate path already exists.")

    try:
        result = subprocess.run(
            [
                qemu_img,
                "convert",
                "-O",
                "qcow2",
                "-o",
                f"preallocation={preflight['target_preallocation']}",
                image_path.as_posix(),
                temp_path.as_posix(),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=settings.STORAGE_INFLATE_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise StorageActionError(f"qemu-img convert failed: {(result.stderr or '').strip()[:240]}")

        converted_info = probe_qemu_image_info(
            path=temp_path.as_posix(),
            entry_type=entry.entry_type,
            content_category=entry.content_category,
        )
        if converted_info.get("format") != "qcow2":
            raise StorageActionError("Converted image is not qcow2.")
        if converted_info.get("virtual_size_bytes") != preflight["virtual_size_bytes"]:
            raise StorageActionError("Converted image virtual size does not match the original.")
        converted_allocation = converted_info.get("qcow2_allocation_percent")
        if not isinstance(converted_allocation, (int, float)) or converted_allocation < MIN_INFLATE_ALLOCATED_PERCENT:
            raise StorageActionError(
                "qemu-img convert did not produce an image with fully mapped qcow2 clusters; "
                "original file was left unchanged."
            )
        _apply_reference_file_metadata(source_path=image_path, target_path=temp_path)

        image_path.rename(backup_path)
        try:
            temp_path.rename(image_path)
        except OSError:
            backup_path.rename(image_path)
            raise
        backup_path.unlink()
    except subprocess.TimeoutExpired as exc:
        raise StorageActionError("qemu-img convert timed out.") from exc
    except StorageActionError:
        temp_path.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        if backup_path.exists() and not image_path.exists():
            backup_path.rename(image_path)
        raise StorageActionError("Inflate failed.") from exc

    final_info = probe_qemu_image_info(
        path=image_path.as_posix(),
        entry_type=entry.entry_type,
        content_category=entry.content_category,
    )
    return {
        "path": _normalize_relative_path(entry.path),
        "directory_path": _parent_relative(entry.path),
        "target_preallocation": preflight["target_preallocation"],
        "before": {
            "virtual_size_bytes": preflight["virtual_size_bytes"],
            "disk_size_bytes": preflight["disk_size_bytes"],
        },
        "after": final_info,
    }


def restore_trash_item(*, item: TrashItem) -> dict[str, object]:
    if item.restore_status != TrashItem.RestoreStatus.TRASHED:
        raise StorageActionError("Trash item is not restorable.")

    storage = _storage_for_trash_item(item)
    require_storage_write_access(storage)
    root = _storage_root(storage)
    trash_path = _storage_existing_entry(item.trash_path, root=root)
    restore_path = _storage_child_path(item.original_path, root=root)
    if restore_path.exists():
        raise StorageActionError("Original path already exists.")

    try:
        restore_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageActionError("Restore directory is not writable.") from exc
    try:
        trash_path.rename(restore_path)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise StorageActionError("Restore target must be on the same filesystem/export.") from exc
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
    trash_path = _storage_child_path(item.trash_path, root=root)

    if trash_path.exists():
        try:
            if trash_path.is_dir():
                shutil.rmtree(trash_path)
            else:
                trash_path.unlink()
        except OSError as exc:
            raise StorageActionError(f"Failed to delete: {exc}") from exc

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

        file_path = _storage_child_path(path, root=root)
        if not file_path.is_file():
            skipped.append(path)
            continue

        vm_dir = _storage_child_path(PurePosixPath(path).parent.as_posix(), root=root)
        _apply_proxmox_upload_metadata(vm_dir, is_directory=True)
        _apply_proxmox_upload_metadata(file_path, is_directory=False)
        normalized.append(path)

    return {
        "normalized": normalized,
        "skipped": skipped,
    }


def _require_file_not_blocked(entry: FileInventory, *, block_running_guests: bool = True) -> None:
    risk = file_action_risk(entry, block_running_guests=block_running_guests)
    if risk.blocked:
        raise StorageActionError(risk.warning_message)
    require_live_guest_stopped(entry)


def _require_inflate_owner_preservable(path: Path) -> None:
    current_uid = os.geteuid()
    current_gid = os.getegid()
    if current_uid == 0:
        return

    stat_result = path.stat()
    if stat_result.st_uid == current_uid and stat_result.st_gid == current_gid:
        return

    raise StorageActionError(
        "Cannot safely inflate this disk from pve-helper because replacing it would change "
        f"the file owner from UID:GID {stat_result.st_uid}:{stat_result.st_gid} "
        f"to {current_uid}:{current_gid}. Run the inflate manually on a Proxmox node "
        "or use a node-side helper."
    )


def _apply_reference_file_metadata(*, source_path: Path, target_path: Path) -> None:
    source_stat = source_path.stat()
    try:
        os.chmod(target_path, stat.S_IMODE(source_stat.st_mode))
        os.chown(target_path, source_stat.st_uid, source_stat.st_gid)
    except OSError as exc:
        raise StorageActionError(
            "Cannot preserve original disk ownership and mode on the inflated image; "
            "original file was left unchanged."
        ) from exc


def _apply_proxmox_upload_metadata(path: Path, *, is_directory: bool) -> None:
    mode = 0o775 if is_directory else 0o664
    try:
        os.chown(path, 0, 0)
        os.chmod(path, mode)
    except OSError as exc:
        raise StorageActionError(
            "Cannot normalize uploaded Proxmox image ownership and mode. "
            "The uploaded file remains in place, but Proxmox may need manual owner/mode repair."
        ) from exc


def _storage_root(storage: StorageMount) -> Path:
    try:
        root = Path(storage.path).resolve(strict=True)
    except OSError as exc:
        raise StorageActionError("Storage path is not available.") from exc
    if not root.is_dir():
        raise StorageActionError("Storage path is not a directory.")
    return root


def _storage_directory(relative_path: str, *, root: Path) -> Path:
    directory = root if not relative_path else _storage_child_path(relative_path, root=root)
    if not directory.is_dir():
        raise StorageActionError("Target directory does not exist.")
    return directory


def _storage_existing_file(relative_path: str, *, root: Path) -> Path:
    path = _storage_child_path(relative_path, root=root)
    if not path.is_file():
        raise StorageActionError("File does not exist.")
    return path


def _storage_existing_entry(relative_path: str, *, root: Path) -> Path:
    path = _storage_child_path(relative_path, root=root)
    if not path.exists():
        raise StorageActionError("File or directory does not exist.")
    if not path.is_file() and not path.is_dir():
        raise StorageActionError("Only files and directories can be changed.")
    return path


def _storage_child_path(relative_path: str, *, root: Path) -> Path:
    normalized = _normalize_relative_path(relative_path)
    candidate = root.joinpath(*PurePosixPath(normalized).parts).resolve(strict=False)
    if not candidate.is_relative_to(root):
        raise StorageActionError("Invalid storage path.")
    return candidate


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

        target_path = _storage_child_path(target_relative, root=root)
        if target_path.exists():
            raise StorageActionError("Target file already exists.")
        plan.append(
            {
                "file": uploaded_file,
                "relative_path": target_relative,
                "target_path": target_path,
                "size_bytes": file_size,
            }
        )
    return plan


def _remove_empty_directories(paths: set[Path], *, stop_at: Path) -> None:
    for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
        if path == stop_at or not path.is_relative_to(stop_at):
            continue
        try:
            path.rmdir()
        except OSError:
            pass


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
    trash_root = Path(storage.trash_path or root / ".trash" / "pve-helper").resolve(strict=False)
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
    storage_id = item.storage_id or (item.metadata or {}).get("storage_id", "")
    if not storage_id:
        raise StorageActionError("Trash item is missing storage metadata.")
    try:
        return StorageMount.objects.get(storage_id=storage_id, enabled=True)
    except StorageMount.DoesNotExist as exc:
        raise StorageActionError("Trash item storage is not available.") from exc


def is_nfs_silly_rename_path(path: str) -> bool:
    return any(part.startswith(".nfs") for part in PurePosixPath(path).parts)

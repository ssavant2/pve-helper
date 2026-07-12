from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from core.models import FileInventory, ProxmoxInventory
from core.services.classification import extract_vmid_from_image_path
from core.services.proxmox import fetch_live_guest_status


@dataclass(frozen=True)
class ReferencedObject:
    object_type: str
    vmid: int | None
    name: str
    node: str
    status: str

    @property
    def label(self) -> str:
        kind = self.object_type.upper()
        name = f" ({self.name})" if self.name else ""
        return f"{kind} {self.vmid}{name} on {self.node} [{self.status}]"


@dataclass(frozen=True)
class FileActionRisk:
    level: str
    reason: str
    referenced_objects: list[ReferencedObject]
    requires_extra_confirmation: bool = False
    blocked: bool = False

    @property
    def referenced_labels(self) -> str:
        return ", ".join(item.label for item in self.referenced_objects)

    @property
    def warning_message(self) -> str:
        if self.referenced_objects:
            return f"{self.reason}: {self.referenced_labels}"
        return self.reason


def file_action_risk(entry: FileInventory, *, block_running_guests: bool = True) -> FileActionRisk:
    if entry.classification == FileInventory.Classification.TRASH or entry.content_category == "trash":
        return FileActionRisk(level="blocked", reason="Items already in trash are managed from the trash view.", referenced_objects=[], blocked=True)
    if entry.entry_type == FileInventory.EntryType.DIRECTORY:
        return _directory_action_risk(entry, block_running_guests=block_running_guests)
    if entry.entry_type != FileInventory.EntryType.FILE:
        return FileActionRisk(level="blocked", reason="Only regular files and directories can be changed.", referenced_objects=[], blocked=True)

    referenced_objects = _referenced_objects(entry)
    running_objects = [item for item in referenced_objects if item.status == "running"]
    if block_running_guests and running_objects:
        return FileActionRisk(
            level="blocked",
            reason="This file belongs to a running Proxmox guest. Stop the guest manually in Proxmox before changing this file",
            referenced_objects=running_objects,
            requires_extra_confirmation=True,
            blocked=True,
        )

    vmid_objects = _vmid_objects(entry)
    running_vmid_objects = [item for item in vmid_objects if item.status == "running"]
    if block_running_guests and running_vmid_objects:
        return FileActionRisk(
            level="blocked",
            reason="This file is in the image directory of a running Proxmox guest. Stop the guest manually in Proxmox before changing this file",
            referenced_objects=running_vmid_objects,
            requires_extra_confirmation=True,
            blocked=True,
        )

    if _storage_gate_blocked(entry) and _is_proxmox_guest_file(entry):
        return FileActionRisk(
            level="blocked",
            reason="Storage consumer inventory is incomplete, so guest-file safety cannot be verified.",
            referenced_objects=[],
            requires_extra_confirmation=True,
            blocked=True,
        )

    if referenced_objects:
        return FileActionRisk(
            level="danger",
            reason="This file is referenced by Proxmox inventory",
            referenced_objects=referenced_objects,
            requires_extra_confirmation=True,
        )

    if entry.content_category == "base_image":
        return FileActionRisk(
            level="danger",
            reason="This is a base image and may be used by linked clones; backing-chain analysis is not available in V1.",
            referenced_objects=vmid_objects,
            requires_extra_confirmation=True,
        )

    if _is_proxmox_guest_file(entry):
        return FileActionRisk(
            level="warning",
            reason="This looks like a Proxmox guest-related file.",
            referenced_objects=vmid_objects,
            requires_extra_confirmation=True,
        )

    return FileActionRisk(level="normal", reason="Standard file action.", referenced_objects=[])


def guest_objects_for_entry(entry: FileInventory) -> list[ReferencedObject]:
    objects = [*_referenced_objects(entry), *_vmid_objects(entry)]
    deduped: list[ReferencedObject] = []
    seen: set[tuple[str, str, int | None]] = set()
    for item in objects:
        key = (item.node, item.object_type, item.vmid)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    # Override each scanned status with the live status so an action-time
    # warning ("belongs to a running guest") is honest, not stale from the last
    # scan. Live status is cached and falls back to the scanned value on error.
    live = fetch_live_guest_status()
    return [
        replace(item, status=live.get((item.node or "", item.object_type, item.vmid), item.status))
        if item.vmid is not None
        else item
        for item in deduped
    ]


def _referenced_objects(entry: FileInventory) -> list[ReferencedObject]:
    if not entry.derived_volid:
        return []
    return [
        _referenced_object(obj)
        for obj in ProxmoxInventory.objects.filter(scan_run=entry.scan_run)
        if entry.derived_volid in (obj.disk_references or [])
    ]


def _vmid_objects(entry: FileInventory) -> list[ReferencedObject]:
    vmid = extract_vmid_from_image_path(entry.path)
    if vmid is None:
        return []
    return [
        _referenced_object(obj)
        for obj in ProxmoxInventory.objects.filter(
            scan_run=entry.scan_run,
            vmid=vmid,
            object_type__in=[ProxmoxInventory.ObjectType.VM, ProxmoxInventory.ObjectType.CT],
        )
    ]


def _referenced_object(obj: ProxmoxInventory) -> ReferencedObject:
    return ReferencedObject(
        object_type=obj.object_type,
        vmid=obj.vmid,
        name=obj.name,
        node=obj.node,
        status=obj.status,
    )


def _storage_gate_blocked(entry: FileInventory) -> bool:
    gate = (entry.scan_run.storage_gate_status or {}).get(entry.storage.storage_id, {})
    return bool(gate) and not bool(gate.get("ok"))


def _is_proxmox_guest_file(entry: FileInventory) -> bool:
    path = PurePosixPath(entry.path)
    return (
        entry.content_category in {"vm_disk", "base_image", "ct_private"}
        or path.parts[:1] == ("images",)
        or path.parts[:1] == ("private",)
    )


def _directory_action_risk(entry: FileInventory, *, block_running_guests: bool) -> FileActionRisk:
    path = PurePosixPath(entry.path)
    if path.parts[:1] in {("images",), ("private",)}:
        referenced_objects = _vmid_objects(entry)
        running_objects = [item for item in referenced_objects if item.status == "running"]
        if len(path.parts) == 1:
            return FileActionRisk(
                level="blocked",
                reason="Top-level Proxmox guest directories are managed by Proxmox and cannot be changed from the file browser.",
                referenced_objects=[],
                requires_extra_confirmation=True,
                blocked=True,
            )
        if block_running_guests and running_objects:
            return FileActionRisk(
                level="blocked",
                reason="This directory belongs to a running Proxmox guest. Stop the guest manually in Proxmox before changing it",
                referenced_objects=running_objects,
                requires_extra_confirmation=True,
                blocked=True,
            )
        return FileActionRisk(
            level="warning",
            reason="This empty Proxmox guest directory will be moved to the Recycle Bin.",
            referenced_objects=referenced_objects,
            requires_extra_confirmation=True,
        )

    if path.parts[:1] == (".trash",):
        return FileActionRisk(level="blocked", reason="Trash directories are managed from the trash view.", referenced_objects=[], blocked=True)

    return FileActionRisk(
        level="warning",
        reason="This directory and all contents will be moved to the Recycle Bin.",
        referenced_objects=[],
        requires_extra_confirmation=True,
    )

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from django.conf import settings

from core.models import CurrentGuestInventory, FileInventory, ProxmoxCluster, ProxmoxInventory
from core.services.refs import GuestRef
from core.services.classification import extract_vmid_from_image_path
from core.services.proxmox import fetch_live_guest_status


@dataclass(frozen=True)
class ReferencedObject:
    cluster_key: str
    object_type: str
    vmid: int | None
    name: str
    node: str
    status: str

    @property
    def guest_ref(self) -> GuestRef | None:
        if not self.cluster_key or self.vmid is None or self.object_type not in {"vm", "ct"}:
            return None
        return GuestRef(self.cluster_key, self.object_type, self.vmid, self.node)

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
    seen: set[tuple[str, str, str, int | None]] = set()
    for item in objects:
        key = (item.cluster_key, item.node, item.object_type, item.vmid)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    # Override each scanned status with the live status so an action-time
    # warning ("belongs to a running guest") is honest, not stale from the last
    # scan. Live status is cached and falls back to the scanned value on error.
    statuses_by_cluster: dict[str, dict] = {}
    for cluster_key in {item.cluster_key for item in deduped if item.cluster_key}:
        cluster = ProxmoxCluster.objects.filter(key=cluster_key).first()
        if cluster is not None:
            statuses_by_cluster[cluster_key] = fetch_live_guest_status(cluster=cluster)
    return [
        replace(
            item,
            status=statuses_by_cluster.get(item.cluster_key, {}).get(
                (item.node or "", item.object_type, item.vmid),
                item.status,
            ),
        )
        if item.vmid is not None
        else item
        for item in deduped
    ]


def _referenced_objects(entry: FileInventory) -> list[ReferencedObject]:
    if not entry.derived_volid:
        return []
    _, separator, suffix = entry.derived_volid.partition(":")
    if not separator:
        return []
    objects: list[ReferencedObject] = []
    bindings = list(entry.storage.cluster_bindings.select_related(
        "cluster_storage__cluster"
    ))
    if not bindings and settings.PVE_TEST_NETWORK_DISABLED:
        return [
            _legacy_referenced_object(obj)
            for obj in ProxmoxInventory.objects.filter(scan_run=entry.scan_run)
            if entry.derived_volid in (obj.disk_references or [])
        ]
    for binding in bindings:
        expected_volid = f"{binding.cluster_storage.storage_id}:{suffix}"
        query = CurrentGuestInventory.objects.filter(cluster=binding.cluster_storage.cluster)
        if binding.node:
            query = query.filter(node=binding.node)
        objects.extend(
            _referenced_object(obj)
            for obj in query
            if expected_volid in (obj.disk_references or [])
        )
    return objects


def _vmid_objects(entry: FileInventory) -> list[ReferencedObject]:
    vmid = extract_vmid_from_image_path(entry.path)
    if vmid is None:
        return []
    objects: list[ReferencedObject] = []
    bindings = list(entry.storage.cluster_bindings.select_related(
        "cluster_storage__cluster"
    ))
    if not bindings and settings.PVE_TEST_NETWORK_DISABLED:
        return [
            _legacy_referenced_object(obj)
            for obj in ProxmoxInventory.objects.filter(
                scan_run=entry.scan_run,
                vmid=vmid,
                object_type__in=[ProxmoxInventory.ObjectType.VM, ProxmoxInventory.ObjectType.CT],
            )
        ]
    for binding in bindings:
        query = CurrentGuestInventory.objects.filter(
            cluster=binding.cluster_storage.cluster,
            vmid=vmid,
            object_type__in=["vm", "ct"],
        )
        if binding.node:
            query = query.filter(node=binding.node)
        objects.extend(_referenced_object(obj) for obj in query)
    return objects


def _referenced_object(obj: CurrentGuestInventory) -> ReferencedObject:
    return ReferencedObject(
        cluster_key=obj.cluster.key if obj.cluster_id else "",
        object_type=obj.object_type,
        vmid=obj.vmid,
        name=obj.name,
        node=obj.node,
        status=obj.status,
    )


def _legacy_referenced_object(obj: ProxmoxInventory) -> ReferencedObject:
    return ReferencedObject(
        cluster_key=obj.cluster.key if obj.cluster_id else "",
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

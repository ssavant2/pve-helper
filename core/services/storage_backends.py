"""Single source of truth for Proxmox storage backend capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ContentListMode(StrEnum):
    PVE_API = "pve-api"
    UNSUPPORTED = "unsupported"


class FilesystemMode(StrEnum):
    NONE = "none"
    DIRECTORY = "directory"
    NETWORK_MOUNT = "network-mount"


class VolumeDeleteMode(StrEnum):
    PVE_API = "pve-api"
    DISABLED = "disabled"


@dataclass(frozen=True)
class StorageBackendProfile:
    storage_type: str
    content_list_mode: ContentListMode
    filesystem_mode: FilesystemMode
    volume_delete_mode: VolumeDeleteMode
    expected_filesystems: frozenset[str] = frozenset()
    known: bool = True

    @property
    def filesystem_eligible(self) -> bool:
        return self.filesystem_mode is not FilesystemMode.NONE

    @property
    def requires_mountpoint(self) -> bool:
        return self.filesystem_mode is FilesystemMode.NETWORK_MOUNT


def _profile(storage_type: str, filesystem_mode: FilesystemMode, *filesystems: str) -> StorageBackendProfile:
    return StorageBackendProfile(
        storage_type=storage_type,
        content_list_mode=ContentListMode.PVE_API,
        filesystem_mode=filesystem_mode,
        volume_delete_mode=VolumeDeleteMode.PVE_API,
        expected_filesystems=frozenset(filesystems),
    )


_PROFILES = {
    "dir": _profile("dir", FilesystemMode.DIRECTORY),
    "nfs": _profile("nfs", FilesystemMode.NETWORK_MOUNT, "nfs", "nfs4"),
    "cifs": _profile("cifs", FilesystemMode.NETWORK_MOUNT, "cifs", "smb3"),
    "cephfs": _profile("cephfs", FilesystemMode.NETWORK_MOUNT, "ceph", "cephfs"),
    "glusterfs": _profile("glusterfs", FilesystemMode.NETWORK_MOUNT, "fuse.glusterfs", "glusterfs"),
    "btrfs": _profile("btrfs", FilesystemMode.DIRECTORY, "btrfs"),
    "lvm": _profile("lvm", FilesystemMode.NONE),
    "lvmthin": _profile("lvmthin", FilesystemMode.NONE),
    "iscsi": _profile("iscsi", FilesystemMode.NONE),
    "iscsidirect": _profile("iscsidirect", FilesystemMode.NONE),
    "zfspool": _profile("zfspool", FilesystemMode.NONE),
    "rbd": _profile("rbd", FilesystemMode.NONE),
    "pbs": _profile("pbs", FilesystemMode.NONE),
    "esxi": _profile("esxi", FilesystemMode.NONE),
}


def backend_profile(storage_type: str) -> StorageBackendProfile:
    normalized = str(storage_type or "").strip().lower()
    profile = _PROFILES.get(normalized)
    if profile is not None:
        return profile
    return StorageBackendProfile(
        storage_type=normalized or "unknown",
        content_list_mode=ContentListMode.UNSUPPORTED,
        filesystem_mode=FilesystemMode.NONE,
        volume_delete_mode=VolumeDeleteMode.DISABLED,
        known=False,
    )


def supported_storage_types() -> frozenset[str]:
    return frozenset(_PROFILES)

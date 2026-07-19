from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from core.models import StorageMount
from core.services.storage_paths import storage_mount_root


@dataclass(frozen=True)
class MountInfo:
    mount_point: str = ""
    filesystem_type: str = ""
    source: str = ""
    mount_options: str = ""
    super_options: str = ""


@dataclass(frozen=True)
class StorageSpaceInfo:
    ok: bool
    total_bytes: int | None = None
    available_bytes: int | None = None
    used_bytes: int | None = None
    used_percent: float | None = None
    filesystem_type: str = ""
    source: str = ""
    mount_point: str = ""
    access_mode: str = "unknown"
    access_label: str = "Unknown"
    access_class: str = "unknown"
    can_write: bool = False
    error: str = ""


def storage_space_info(storage: StorageMount) -> StorageSpaceInfo:
    try:
        storage_path = storage_mount_root(storage).resolve(strict=True)
        stats = os.statvfs(storage_path)
    except OSError as exc:
        return StorageSpaceInfo(ok=False, error=exc.__class__.__name__)

    total_bytes = stats.f_blocks * stats.f_frsize
    available_bytes = stats.f_bavail * stats.f_frsize
    used_bytes = max(total_bytes - available_bytes, 0)
    used_percent = (used_bytes / total_bytes * 100) if total_bytes else 0
    mount = mount_info_for_path(storage_path)
    access_mode = mount_access_mode(mount)

    return StorageSpaceInfo(
        ok=True,
        total_bytes=total_bytes,
        available_bytes=available_bytes,
        used_bytes=used_bytes,
        used_percent=used_percent,
        filesystem_type=mount.filesystem_type,
        source=mount.source,
        mount_point=mount.mount_point,
        access_mode=access_mode,
        access_label=mount_access_label(access_mode),
        access_class=mount_access_class(access_mode),
        can_write=access_mode == "read_write",
    )


def mount_info_for_path(path: Path) -> MountInfo:
    best_match: MountInfo | None = None
    best_length = -1

    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return MountInfo()

    for line in lines:
        mount = _parse_mountinfo_line(line)
        if not mount.mount_point:
            continue

        mount_path = Path(mount.mount_point)
        if not _is_path_relative_to(path, mount_path):
            continue

        match_length = len(mount_path.as_posix())
        if match_length > best_length:
            best_match = mount
            best_length = match_length

    return best_match or MountInfo()


def mount_access_mode(mount: MountInfo) -> str:
    options = _option_set(mount.mount_options) | _option_set(mount.super_options)
    if "ro" in options:
        return "read_only"
    if "rw" in options:
        return "read_write"
    return "unknown"


def mount_access_label(access_mode: str) -> str:
    return {
        "read_only": "Read-only",
        "read_write": "Read/write",
    }.get(access_mode, "Unknown")


def mount_access_class(access_mode: str) -> str:
    return {
        "read_only": "warning",
        "read_write": "success",
    }.get(access_mode, "unknown")


def _parse_mountinfo_line(line: str) -> MountInfo:
    before, separator, after = line.partition(" - ")
    if not separator:
        return MountInfo()

    before_fields = before.split()
    after_fields = after.split()
    if len(before_fields) < 5 or len(after_fields) < 2:
        return MountInfo()

    return MountInfo(
        mount_point=_decode_mountinfo_field(before_fields[4]),
        mount_options=_decode_mountinfo_field(before_fields[5]) if len(before_fields) > 5 else "",
        filesystem_type=after_fields[0],
        source=_decode_mountinfo_field(after_fields[1]),
        super_options=_decode_mountinfo_field(after_fields[2]) if len(after_fields) > 2 else "",
    )


def _decode_mountinfo_field(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _option_set(options: str) -> set[str]:
    return {option.split("=", 1)[0] for option in options.split(",") if option}


def _is_path_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True

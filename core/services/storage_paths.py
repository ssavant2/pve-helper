"""Trusted path construction for registered storage mounts."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings

from core.models import StorageMount
from core.services.public_errors import PublicMessageError


class StorageMountError(PublicMessageError, ValueError):
    pass


# The one place that decides where a mount's trash lives. Every writer derives
# it from here: a mount whose trash is spelled differently is invisible to the
# file browser's `trash` category and to retention, so the shape is part of the
# contract, not a formatting detail.
TRASH_DIRECTORY = ".trash/pve-helper"


def normalized_relative_path(value: str) -> str:
    raw = str(value or "").strip().replace("\\", "/").strip("/")
    if not raw or any(part in {"", ".", ".."} for part in raw.split("/")):
        raise StorageMountError("Storage path must be a directory beneath /storages.")
    return raw


def default_trash_relative_path(relative_path: str) -> str:
    return f"{normalized_relative_path(relative_path)}/{TRASH_DIRECTORY}"


def storage_mount_root(mount: StorageMount) -> Path:
    relative = getattr(mount, "relative_path", "")
    if not relative:
        # Bounded test/migration compatibility for pre-S0 rows. New writers
        # always persist relative_path and never accept arbitrary absolute paths.
        legacy = Path(str(getattr(mount, "path", "") or ""))
        root = Path(settings.PVE_HELPER_STORAGE_CONTAINER_ROOT)
        try:
            relative = str(legacy.relative_to(root))
        except ValueError as exc:
            if settings.PVE_TEST_NETWORK_DISABLED:
                return legacy
            raise StorageMountError("Legacy storage path is outside /storages.") from exc
    return Path(settings.PVE_HELPER_STORAGE_CONTAINER_ROOT) / normalized_relative_path(relative)


def storage_trash_root(mount: StorageMount) -> Path:
    if getattr(mount, "trash_relative_path", ""):
        return Path(settings.PVE_HELPER_STORAGE_CONTAINER_ROOT) / normalized_relative_path(mount.trash_relative_path)
    if getattr(mount, "trash_path", ""):
        legacy = Path(mount.trash_path)
        try:
            relative = legacy.relative_to(Path(settings.PVE_HELPER_STORAGE_CONTAINER_ROOT))
        except ValueError as exc:
            raise StorageMountError("Legacy trash path is outside /storages.") from exc
        return Path(settings.PVE_HELPER_STORAGE_CONTAINER_ROOT) / normalized_relative_path(str(relative))
    return storage_mount_root(mount) / TRASH_DIRECTORY

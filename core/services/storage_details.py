from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.models import ProxmoxInventory, ScanRun, StorageMount

from .filesystem import StorageSpaceInfo, mount_info_for_path


@dataclass(frozen=True)
class StorageDetails:
    storage_id: str
    storage_type: str = ""
    server: str = ""
    export_path: str = ""
    proxmox_path: str = ""
    app_path: str = ""
    app_filesystem_type: str = ""
    app_source: str = ""
    app_mount_options: str = ""
    app_super_options: str = ""
    options: str = ""
    preallocation: str = "default"
    content: str = ""
    shared: str = ""
    active: str = ""


def storage_details(storage: StorageMount, scan: ScanRun | None, space_info: StorageSpaceInfo) -> StorageDetails:
    config = _latest_proxmox_storage_config(storage, scan)
    server, export_path = _split_export(storage.export)
    mount = mount_info_for_path(Path(storage.path).resolve(strict=False)) if space_info.ok else None

    return StorageDetails(
        storage_id=storage.storage_id,
        storage_type=str(config.get("type") or space_info.filesystem_type or ""),
        server=str(config.get("server") or server),
        export_path=str(config.get("export") or export_path),
        proxmox_path=str(config.get("path") or _default_proxmox_path(storage, config, space_info)),
        app_path=storage.path,
        app_filesystem_type=space_info.filesystem_type,
        app_source=space_info.source,
        app_mount_options=mount.mount_options if mount else "",
        app_super_options=mount.super_options if mount else "",
        options=_display_options(str(config.get("options") or ""), mount.super_options if mount else ""),
        preallocation=str(config.get("preallocation") or "default"),
        content=str(config.get("content") or ""),
        shared=_boolish_label(config.get("shared")),
        active=_boolish_label(config.get("active")),
    )


def _latest_proxmox_storage_config(storage: StorageMount, scan: ScanRun | None) -> dict:
    if scan is None:
        return {}

    inventory = (
        ProxmoxInventory.objects.filter(
            scan_run=scan,
            object_type=ProxmoxInventory.ObjectType.STORAGE,
            name=storage.storage_id,
        )
        .order_by("node", "id")
        .first()
    )
    return inventory.config if inventory and isinstance(inventory.config, dict) else {}


def _split_export(export: str) -> tuple[str, str]:
    if ":" not in export:
        return "", export
    server, path = export.split(":", 1)
    return server, path


def _default_proxmox_path(storage: StorageMount, config: dict, space_info: StorageSpaceInfo) -> str:
    storage_type = str(config.get("type") or space_info.filesystem_type or "")
    if storage_type.startswith("nfs") or storage.export:
        return f"/mnt/pve/{storage.storage_id}"
    return ""


def _display_options(proxmox_options: str, app_super_options: str) -> str:
    if proxmox_options:
        return proxmox_options

    keep_flags = {"ro", "rw", "hard", "soft"}
    keep_keys = {"vers", "nfsvers", "nconnect", "proto"}
    selected = []
    for option in [item.strip() for item in app_super_options.split(",") if item.strip()]:
        if option in keep_flags:
            selected.append(option)
            continue
        key = option.split("=", 1)[0]
        if key in keep_keys:
            selected.append(option)

    return ",".join(selected) or app_super_options


def _boolish_label(value) -> str:
    if value in {True, 1, "1", "true", "True", "yes", "on"}:
        return "yes"
    if value in {False, 0, "0", "false", "False", "no", "off"}:
        return "no"
    return str(value or "")

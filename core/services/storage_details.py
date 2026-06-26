from __future__ import annotations

from dataclasses import dataclass

from core.models import ProxmoxInventory, ScanRun, StorageMount

from .filesystem import StorageSpaceInfo


@dataclass(frozen=True)
class StorageDetails:
    storage_id: str
    storage_type: str = ""
    server: str = ""
    export_path: str = ""
    proxmox_path: str = ""
    app_filesystem_type: str = ""
    app_source: str = ""
    options: str = ""
    preallocation: str = "default"
    content: str = ""
    shared: str = ""
    active: str = ""


def storage_details(storage: StorageMount, scan: ScanRun | None, space_info: StorageSpaceInfo) -> StorageDetails:
    config = _latest_proxmox_storage_config(storage, scan)
    server, export_path = _split_export(storage.export)

    return StorageDetails(
        storage_id=storage.storage_id,
        storage_type=str(config.get("type") or ""),
        server=str(config.get("server") or server),
        export_path=str(config.get("export") or export_path),
        proxmox_path=str(config.get("path") or ""),
        app_filesystem_type=space_info.filesystem_type,
        app_source=space_info.source,
        options=_normalize_options(str(config.get("options") or "")),
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


def _normalize_options(options: str) -> str:
    parts = [part.strip() for part in options.split(",") if part.strip()]
    if not parts:
        return ""

    priority = {
        "vers": 10,
        "nfsvers": 10,
        "nconnect": 20,
    }

    def sort_key(option: str) -> tuple[int, str]:
        key = option.split("=", 1)[0]
        return priority.get(key, 100), key

    return ",".join(sorted(parts, key=sort_key))


def _boolish_label(value) -> str:
    if value in {True, 1, "1", "true", "True", "yes", "on"}:
        return "yes"
    if value in {False, 0, "0", "false", "False", "no", "off"}:
        return "no"
    return str(value or "")

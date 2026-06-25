from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class DerivedVolume:
    storage_id: str
    relative_path: str
    volid: str
    content_category: str


def categorize_proxmox_path(relative_path: str) -> str:
    path = PurePosixPath(relative_path)
    parts = path.parts
    if len(parts) >= 3 and parts[0] == "images" and parts[2].startswith("base-"):
        return "base_image"
    if len(parts) >= 3 and parts[0] == "images" and parts[2].startswith("vm-"):
        return "vm_disk"
    if parts[:1] == ("dump",):
        return "backup"
    if parts[:2] == ("template", "iso"):
        return "iso"
    if parts[:2] == ("template", "cache"):
        return "ct_template"
    if parts[:1] == ("snippets",):
        return "snippet"
    if parts[:1] == ("private",):
        return "ct_private"
    return "unknown"


def derive_volid(storage_id: str, relative_path: str) -> DerivedVolume | None:
    category = categorize_proxmox_path(relative_path)
    if category in {"vm_disk", "base_image"}:
        path = PurePosixPath(relative_path)
        return DerivedVolume(
            storage_id=storage_id,
            relative_path=relative_path,
            volid=f"{storage_id}:{path.parts[1]}/{path.name}",
            content_category=category,
        )
    return None

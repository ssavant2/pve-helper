from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GuestIdentity:
    """Canonical VM/CT label data.

    Single source of truth for how a guest is presented, so the app-wide
    guest-name toggle can hide the VMID everywhere through one markup
    contract instead of each surface baking its own label string.
    """

    object_type: str  # "vm" | "ct"
    vmid: int | None = None
    name: str = ""

    @property
    def type_label(self) -> str:
        """Short badge form (VM / CT)."""
        return {"vm": "VM", "ct": "CT"}.get(self.object_type, (self.object_type or "Guest").upper())

    @property
    def type_word(self) -> str:
        """Long form matching Proxmox/`ScheduledAction` display (VM / Container)."""
        return {"vm": "VM", "ct": "Container"}.get(self.object_type, (self.object_type or "Guest").upper())

    @property
    def has_name(self) -> bool:
        return bool(self.name)

    @property
    def vmid_text(self) -> str:
        return "" if self.vmid is None else str(self.vmid)

    @property
    def full_label(self) -> str:
        """Plain-text `500 (name)` for titles/aria and controls that cannot
        render toggled markup (e.g. ``<option>`` text)."""
        if self.vmid is None:
            return self.name or "?"
        if self.name:
            return f"{self.vmid} ({self.name})"
        return str(self.vmid)

    @property
    def full_label_with_type(self) -> str:
        """Plain-text `VM 500 (name)`; preserves the legacy label format used
        by the target picker and string fallbacks."""
        if self.vmid is None:
            return f"{self.type_word} {self.name}".strip() or self.type_word
        base = f"{self.type_word} {self.vmid}"
        return f"{base} ({self.name})" if self.name else base

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.object_type, "vmid": self.vmid, "name": self.name}


def guest_identity(object_type: Any, vmid: Any, name: Any = "") -> GuestIdentity:
    try:
        vmid_int = int(vmid) if vmid is not None and str(vmid) != "" else None
    except (TypeError, ValueError):
        vmid_int = None
    return GuestIdentity(object_type=str(object_type or ""), vmid=vmid_int, name=str(name or ""))


def guest_identity_from_inventory(obj: Any) -> GuestIdentity:
    """Build from a `ProxmoxInventory` / `ProxmoxObject` / live guest summary."""
    return guest_identity(
        getattr(obj, "object_type", ""),
        getattr(obj, "vmid", None),
        getattr(obj, "name", ""),
    )


def guest_identity_from_scheduled_action(action: Any) -> GuestIdentity:
    """Build from a `ScheduledAction`; name comes from the creation-time snapshot."""
    return guest_identity(
        getattr(action, "target_type", ""),
        getattr(action, "target_vmid", None),
        getattr(action, "target_name_snapshot", "") or "",
    )


def is_template(config: Any) -> bool:
    """Whether a QEMU guest config marks the guest as a template."""
    if not isinstance(config, dict):
        return False
    value = config.get("template")
    return value is True or str(value) == "1"


def parse_guest_tags(config: Any) -> list[str]:
    """Split the Proxmox `tags` field (``;``-separated, also tolerating space
    and comma) into a normalized list. Read-only view; Module 4 owns writes."""
    if not isinstance(config, dict):
        return []
    raw = config.get("tags")
    if not raw:
        return []
    return [part for part in re.split(r"[;,\s]+", str(raw).strip()) if part]

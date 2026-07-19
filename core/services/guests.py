from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.services.refs import GuestRef, RefParseError


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
    ref: GuestRef | None = None

    def __post_init__(self) -> None:
        if self.ref is not None and (self.object_type, self.vmid) != (
            self.ref.object_type,
            self.ref.vmid,
        ):
            raise ValueError("Presentation identity and GuestRef disagree.")

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
        payload = {"type": self.object_type, "vmid": self.vmid, "name": self.name}
        if self.ref is not None:
            payload["guest_ref"] = self.ref.serialize()
            payload["cluster_key"] = self.ref.cluster_key
        return payload


def guest_identity(
    object_type: Any,
    vmid: Any,
    name: Any = "",
    *,
    cluster_key: Any = "",
    node: Any = "",
    ref: GuestRef | None = None,
) -> GuestIdentity:
    try:
        vmid_int = int(vmid) if vmid is not None and str(vmid) != "" else None
    except (TypeError, ValueError):
        vmid_int = None
    object_type_text = str(object_type or "")
    if ref is None and cluster_key and vmid_int is not None:
        try:
            ref = GuestRef(str(cluster_key), object_type_text, vmid_int, str(node or ""))
        except RefParseError:
            ref = None
    return GuestIdentity(
        object_type=object_type_text,
        vmid=vmid_int,
        name=str(name or ""),
        ref=ref,
    )


def guest_identity_from_inventory(obj: Any) -> GuestIdentity:
    """Build from a `ProxmoxInventory` / `ProxmoxObject` / live guest summary."""
    return guest_identity(
        getattr(obj, "object_type", ""),
        getattr(obj, "vmid", None),
        getattr(obj, "name", ""),
        cluster_key=getattr(getattr(obj, "cluster", None), "key", "") or getattr(obj, "cluster_key", ""),
        node=getattr(obj, "node", ""),
    )


def guest_identity_from_scheduled_action(action: Any) -> GuestIdentity:
    """Build from a `ScheduledAction`; name comes from the creation-time snapshot."""
    return guest_identity(
        getattr(action, "target_type", ""),
        getattr(action, "target_vmid", None),
        getattr(action, "target_name_snapshot", "") or "",
        cluster_key=getattr(getattr(action, "cluster", None), "key", ""),
        node=getattr(action, "target_node", ""),
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
    from core.services.tags import parse_tags

    return parse_tags(config)

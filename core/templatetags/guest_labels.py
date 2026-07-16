from __future__ import annotations

from typing import Any

from django import template

from core.services.guests import GuestIdentity, guest_identity

register = template.Library()


@register.inclusion_tag("core/_guest_label.html")
def guest_label(guest: Any) -> dict[str, Any]:
    """Render a VM/CT label through the shared markup contract so the
    app-wide guest-name toggle can hide the VMID.

    Accepts a ``GuestIdentity``, a mapping with ``type``/``vmid``/``name``,
    or any object exposing ``object_type``/``vmid``/``name``.
    """
    return {"guest": _coerce(guest)}


def _coerce(guest: Any) -> GuestIdentity:
    if isinstance(guest, GuestIdentity):
        return guest
    if isinstance(guest, dict):
        return guest_identity(
            guest.get("type") or guest.get("object_type"),
            guest.get("vmid"),
            guest.get("name", ""),
        )
    return guest_identity(
        getattr(guest, "object_type", "") or getattr(guest, "type", ""),
        getattr(guest, "vmid", None),
        getattr(guest, "name", ""),
    )

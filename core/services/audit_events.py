from __future__ import annotations

from typing import Any

from core.models import AuditEvent
from core.services.request_metadata import client_ip


def audit_module_key(action: str, object_type: str = "", details: Any = None) -> str:
    """Return the persisted Audit module for one normalized event."""
    details = details if isinstance(details, dict) else {}
    action = action or ""
    object_type = object_type or ""

    if action.startswith("auth."):
        return "auth"
    if action.startswith("network.") or object_type.startswith("network"):
        return "network"
    if (
        action.startswith("vm.")
        or action.startswith("scheduled_action.")
        or object_type in {"vm", "ct", "guest", "scheduled_action", "scheduled_action_run"}
    ):
        return "vms"
    if action.startswith("cluster.") or object_type.startswith("cluster"):
        return "clusters"
    if (
        action.startswith("scan.")
        or action.startswith("file.")
        or action.startswith("trash.")
        or object_type in {"scan_run", "scan_schedule", "storage", "file"}
        or details.get("target_storage")
    ):
        return "storage"
    return "system"


def record_audit_event(
    request=None,
    *,
    user=None,
    username: str = "",
    action: str,
    object_type: str = "",
    object_id: str = "",
    outcome: str = "success",
    details: dict | None = None,
    system_username: str = "",
) -> AuditEvent:
    """Create a normalized Audit event for an HTTP request or background actor.

    Request identity is authoritative when supplied. Workers, signals and
    management commands instead pass ``user`` and/or ``username`` explicitly.
    All callers share module classification and model-level detail
    denormalization.
    """
    details = details if isinstance(details, dict) else {}
    resolved_user = user
    resolved_username = str(username or "")
    source_ip = None

    if request is not None:
        request_user = getattr(request, "user", None)
        if request_user is not None and getattr(request_user, "is_authenticated", False):
            resolved_user = request_user
            resolved_username = request_user.get_username()
        elif resolved_user is not None and getattr(resolved_user, "is_authenticated", False):
            if not resolved_username:
                resolved_username = resolved_user.get_username()
        else:
            resolved_user = None
            resolved_username = str(system_username or resolved_username)
        source_ip = client_ip(request)
    elif resolved_user is not None:
        if getattr(resolved_user, "is_authenticated", False) and not resolved_username:
            resolved_username = resolved_user.get_username()
        elif not getattr(resolved_user, "is_authenticated", False):
            resolved_user = None

    return AuditEvent.objects.create(
        user=resolved_user,
        username=resolved_username,
        source_ip=source_ip,
        action=action,
        object_type=object_type,
        object_id=object_id,
        outcome=outcome,
        module=audit_module_key(action, object_type, details),
        details=details,
    )

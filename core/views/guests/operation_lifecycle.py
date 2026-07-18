"""Shared provider-write and task/Audit lifecycle for guest views."""

from __future__ import annotations

from types import SimpleNamespace

from ..common import *  # noqa: F401,F403
from .. import common
from core.services.cluster_resolver import (
    cluster_write,
)
from core.services.current_guest_inventory import refresh_current_guest_from_client
from core.services.public_errors import public_exception_message
from core.services.guest_scope import guest_ref_from_legacy_identity
from core.services.refs import GuestRef, RefParseError

# Sentinel returned by the multi-disk migration path when its worker owns the
# running Audit event. Keeping it here makes ownership explicit for every view
# that submits or completes a guest operation.
MIGRATE_ASYNC = object()


def guest_kind(detail: SimpleNamespace) -> str:
    return "qemu" if detail.object_type == ProxmoxInventory.ObjectType.VM else "lxc"


def guest_cluster(detail: SimpleNamespace):
    """The cluster a guest operation acts inside.

    Every guest write funnels through this module. Phase 3 resolves the legacy URL
    to a GuestRef before lookup, so a write arriving here without its owning cluster
    is a programming error rather than a reason to guess from global state.
    """
    cluster = getattr(detail, "cluster", None)
    ref = getattr(detail, "guest_ref", None)
    if cluster is None or ref is None or cluster.key != ref.cluster_key:
        raise ValueError("Guest operation requires a matching cluster-qualified GuestRef.")
    return cluster


def _guest_path(detail: SimpleNamespace, subpath: str = "") -> str:
    path = f"nodes/{quote(detail.node, safe='')}/{guest_kind(detail)}/{detail.vmid}"
    return f"{path}/{subpath}" if subpath else path


def _guest_write(detail: SimpleNamespace, *, operation: str, fallback: str, call):
    """Run one guest mutation inside the guest's own cluster.

    Replaces the old fan-out over every configured endpoint, which both risked
    mutating a same-VMID guest in another cluster and replayed a write whose outcome
    was unknown. cluster_write() advances to another endpoint only when the failure
    proves the request never left.
    """
    return cluster_write(
        guest_cluster(detail),
        operation=operation,
        call=call,
        error_message=lambda exc: public_exception_message(
            exc, operation=operation, fallback=fallback
        ),
    )


def guest_post_with_client(detail: SimpleNamespace, subpath: str, data: dict | None = None):
    if not detail.node:
        return None, "The guest's node could not be resolved.", None
    result = _guest_write(
        detail,
        operation="guest_post",
        fallback="Proxmox could not complete the guest operation.",
        call=lambda client: client.post(_guest_path(detail, subpath), data=data or {}),
    )
    if not result.ok:
        return None, result.error, None
    return result.value, None, result.client


def guest_post(detail: SimpleNamespace, subpath: str, data: dict | None = None):
    response, err, _client = guest_post_with_client(detail, subpath, data)
    return response, err


def guest_delete_with_client(detail: SimpleNamespace, subpath: str):
    if not detail.node:
        return None, "The guest's node could not be resolved.", None
    result = _guest_write(
        detail,
        operation="guest_delete",
        fallback="Proxmox could not complete the guest operation.",
        call=lambda client: client.delete(_guest_path(detail, subpath)),
    )
    if not result.ok:
        return None, result.error, None
    return result.value, None, result.client


def guest_delete(detail: SimpleNamespace, subpath: str):
    response, err, _client = guest_delete_with_client(detail, subpath)
    return response, err


def wait_for_proxmox_task_if_returned(client, node: str, response, *, timeout_seconds: int) -> str:
    if not (isinstance(response, str) and response.startswith("UPID:")):
        return ""
    if not hasattr(client, "wait_for_task"):
        return ""
    try:
        result = client.wait_for_task(node=node, upid=response, timeout_seconds=timeout_seconds)
    except ProxmoxTaskTimeout as exc:
        return public_exception_message(
            exc,
            operation="guest_task_wait",
            fallback="The Proxmox task did not finish before the timeout.",
        )
    if not result.success:
        return f"Proxmox task exitstatus: {result.exitstatus or result.status or 'unknown'}"
    return ""


def guest_post_wait_task(
    detail: SimpleNamespace,
    subpath: str,
    data: dict | None = None,
    *,
    timeout_seconds: int = SNAPSHOT_TASK_WAIT_SECONDS,
):
    if not detail.node:
        return None, "The guest's node could not be resolved."
    result = _guest_write(
        detail,
        operation="guest_post_wait",
        fallback="Proxmox could not complete the guest operation.",
        call=lambda client: client.post(_guest_path(detail, subpath), data=data or {}),
    )
    if not result.ok:
        return None, result.error
    return result.value, wait_for_proxmox_task_if_returned(
        result.client,
        detail.node,
        result.value,
        timeout_seconds=timeout_seconds,
    )


def guest_delete_wait_task(
    detail: SimpleNamespace,
    subpath: str,
    *,
    timeout_seconds: int = SNAPSHOT_TASK_WAIT_SECONDS,
):
    if not detail.node:
        return None, "The guest's node could not be resolved."
    result = _guest_write(
        detail,
        operation="guest_delete_wait",
        fallback="Proxmox could not complete the guest operation.",
        call=lambda client: client.delete(_guest_path(detail, subpath)),
    )
    if not result.ok:
        return None, result.error
    return result.value, wait_for_proxmox_task_if_returned(
        result.client,
        detail.node,
        result.value,
        timeout_seconds=timeout_seconds,
    )


def guest_destroy_with_client(detail: SimpleNamespace, query: str):
    if not detail.node:
        return None, "The guest's node could not be resolved.", None
    path = _guest_path(detail)
    if query:
        path = f"{path}?{query}"
    result = _guest_write(
        detail,
        operation="guest_destroy",
        fallback="Proxmox could not delete the guest.",
        call=lambda client: client.delete(path),
    )
    if not result.ok:
        return None, result.error, None
    return result.value, None, result.client


def guest_put(detail: SimpleNamespace, subpath: str, data: dict | None = None):
    if not detail.node:
        return None, "The guest's node could not be resolved."
    result = _guest_write(
        detail,
        operation="guest_put",
        fallback="Proxmox could not update the guest.",
        call=lambda client: client.put(_guest_path(detail, subpath), data=data or {}),
    )
    if not result.ok:
        return None, result.error
    return result.value, None


def audit_guest(
    request,
    detail: SimpleNamespace,
    action: str,
    details: dict | None = None,
    *,
    outcome: str = "success",
) -> AuditEvent:
    return record_audit_event(
        request,
        action=action,
        object_type="guest",
        outcome=outcome,
        cluster=guest_cluster(detail),
        guest_ref=detail.guest_ref,
        node_ref=detail.node_ref,
        details={
            "node": detail.node,
            "vmid": detail.vmid,
            "target_type": detail.object_type,
            "name": detail.name,
            **(details or {}),
        },
        system_username="system",
    )


def audit_guest_task_or_success(
    request,
    detail: SimpleNamespace,
    audit_action: str,
    response,
    client,
    audit_details: dict | None = None,
    *,
    timeout_seconds: int | None = None,
) -> AuditEvent:
    details = dict(audit_details or {})
    if isinstance(response, str) and response.startswith("UPID:") and client is not None:
        details.update(
            {
                "proxmox_task_upid": response,
                "proxmox_task_node": detail.node,
                "proxmox_endpoint": getattr(client, "endpoint", ""),
            }
        )
        event = audit_guest(request, detail, audit_action, details, outcome="running")
        event.details = {
            **event.details,
            "operation_payload_version": 1,
            "task_timeout_seconds": timeout_seconds or settings.SCHEDULED_ACTION_TIMEOUT_SECONDS,
        }
        event.save(update_fields=["details"])
        task_id = common.enqueue_bulk_task(
            "core.tasks.poll_guest_audit_task",
            event.id,
        )
        event.details = {**event.details, "poll_task_id": task_id}
        event.save(update_fields=["details"])
        return event
    return audit_guest(request, detail, audit_action, details)


def finish_guest_running_audit(
    event: AuditEvent,
    detail: SimpleNamespace,
    response,
    client,
    *,
    err: str = "",
    audit_details: dict | None = None,
    timeout_seconds: int | None = None,
) -> AuditEvent:
    details = dict(event.details if isinstance(event.details, dict) else {})
    details.update(audit_details or {})
    if response is MIGRATE_ASYNC:
        event.details = details
        event.save(update_fields=["details"])
        return event
    if err:
        event.outcome = "failed"
        details["error"] = err
        details["finished_at"] = tz.now().isoformat()
        event.details = details
        event.save(update_fields=["outcome", "details"])
        return event

    if isinstance(response, str) and response.startswith("UPID:") and client is not None:
        details.update(
            {
                "operation_payload_version": 1,
                "proxmox_task_upid": response,
                "proxmox_task_node": detail.node,
                "proxmox_endpoint": getattr(client, "endpoint", ""),
                "task_timeout_seconds": timeout_seconds or settings.SCHEDULED_ACTION_TIMEOUT_SECONDS,
            }
        )
        task_id = common.enqueue_bulk_task(
            "core.tasks.poll_guest_audit_task",
            event.id,
        )
        details["poll_task_id"] = task_id
        event.details = details
        event.save(update_fields=["details"])
        return event

    event.outcome = "success"
    details["finished_at"] = tz.now().isoformat()
    event.details = details
    event.save(update_fields=["outcome", "details"])
    if client is not None:
        try:
            refresh = refresh_current_guest_from_client(
                client,
                node=detail.node,
                object_type=detail.object_type,
                vmid=detail.vmid,
                cluster=guest_cluster(detail),
            )
            if refresh.error:
                details["projection_refresh_error"] = refresh.error
        except Exception as exc:
            public_exception_message(
                exc,
                operation="guest_projection_refresh",
                fallback="The guest operation succeeded, but its local projection could not be refreshed.",
            )
            details["projection_refresh_error"] = "Targeted guest projection refresh failed."
        if details.get("projection_refresh_error"):
            event.details = details
            event.save(update_fields=["details"])
    return event


def wants_task_json(request) -> bool:
    """Return whether the detail-page action expects local JSON feedback."""
    return request.headers.get("X-Requested-With") == "fetch"


def guest_action_response(request, object_type, vmid, error_label="", *, redirect_name):
    if wants_task_json(request):
        return JsonResponse({"ok": not error_label, "errors": [error_label] if error_label else []})
    if error_label:
        messages.error(request, error_label)
    return redirect(redirect_name, object_type=object_type, vmid=vmid)


def write_result(request, detail, redirect_name, err, audit_action, audit_details=None):
    if err:
        if "403" in err:
            messages.error(request, proxmox_permission_hint("the required privilege"))
        else:
            messages.error(request, f"Failed: {err}")
    else:
        audit_guest(request, detail, audit_action, audit_details)
    return redirect(redirect_name, object_type=detail.object_type, vmid=detail.vmid)


def parse_guest_target_value(value: str) -> tuple[str | None, int | None, str]:
    ref = guest_ref_from_target_value(value)
    return (ref.object_type, ref.vmid, ref.node) if ref is not None else (None, None, "")


def guest_ref_from_target_value(value: str) -> GuestRef | None:
    raw = str(value or "").strip()
    if raw.startswith("gr"):
        try:
            return GuestRef.parse(raw)
        except RefParseError:
            return None
    target_text, _node_separator, node = str(value or "").partition("@")
    object_type, separator, vmid_text = target_text.partition(":")
    if separator != ":" or object_type not in GUEST_OBJECT_TYPES:
        return None
    try:
        return guest_ref_from_legacy_identity(object_type, int(vmid_text), node=node)
    except (TypeError, ValueError):
        return None


# Compatibility aliases while call sites move to explicit public helper names.
_MIGRATE_ASYNC = MIGRATE_ASYNC
_audit_guest = audit_guest
_audit_guest_task_or_success = audit_guest_task_or_success
_guest_ref_from_target_value = guest_ref_from_target_value
_finish_guest_running_audit = finish_guest_running_audit
_guest_action_response = guest_action_response
_guest_delete = guest_delete
_guest_delete_wait_task = guest_delete_wait_task
_guest_delete_with_client = guest_delete_with_client
_guest_destroy_with_client = guest_destroy_with_client
_guest_kind = guest_kind
_guest_post = guest_post
_guest_post_wait_task = guest_post_wait_task
_guest_post_with_client = guest_post_with_client
_guest_put = guest_put
_parse_guest_target_value = parse_guest_target_value
_wait_for_proxmox_task_if_returned = wait_for_proxmox_task_if_returned
_wants_task_json = wants_task_json
_write_result = write_result

from __future__ import annotations

from urllib.parse import quote

from django.conf import settings
from django.utils import timezone

from .models import AuditEvent
from .services.durable_guest_operations import DurableGuestOperationError, client_for_audit_event
from .services.proxmox import ProxmoxAPIError, ProxmoxTaskTimeout, clear_live_guest_caches
from .services.public_errors import (
    ERROR_CODE_INCOMPLETE,
    ERROR_CODE_PROVIDER,
    PROVIDER_FAILURE_MESSAGE,
    PublicFailure,
    public_failure,
)
from .services.task_failures import record_event_failure


def clone_guest_to_template_task(
    audit_event_id: int,
) -> None:
    event = AuditEvent.objects.filter(pk=audit_event_id).first()
    if event is None:
        return
    details = dict(event.details) if isinstance(event.details, dict) else {}
    try:
        client, ref, cluster = client_for_audit_event(event)
        node = str(details.get("proxmox_task_node") or ref.node)
        new_vmid = int(details.get("new_vmid"))
        clone_upid = str(details.get("proxmox_task_upid") or "")
    except (DurableGuestOperationError, TypeError, ValueError):
        record_event_failure(
            event,
            PublicFailure("The queued template clone has incomplete target identity.", ERROR_CODE_INCOMPLETE),
            details=details,
        )
        return
    timeout_seconds = settings.SCHEDULED_ACTION_TIMEOUT_SECONDS

    def cancelled() -> bool:
        return AuditEvent.objects.filter(pk=audit_event_id, outcome="cancelled").exists()

    try:
        clone_result = client.wait_for_task(node=node, upid=clone_upid, timeout_seconds=timeout_seconds)
        if not clone_result.success:
            raise ProxmoxAPIError(f"Clone task failed: {clone_result.exitstatus or clone_result.status or 'unknown'}")
        details["completed_stages"] = ["clone"]
        details["partial_clone_vmid"] = new_vmid
        if cancelled():
            return

        template_upid = client.post(
            f"nodes/{quote(node, safe='')}/qemu/{new_vmid}/template",
            data={},
        )
        if not isinstance(template_upid, str) or not template_upid.startswith("UPID:"):
            raise ProxmoxAPIError("Proxmox did not return a template conversion task ID.")
        details.update(
            {
                "stage": "template",
                "proxmox_task_upid": template_upid,
                "proxmox_task_node": node,
                "proxmox_endpoint": getattr(client, "endpoint", ""),
            }
        )
        event.details = details
        event.save(update_fields=["details"])
        template_result = client.wait_for_task(node=node, upid=template_upid, timeout_seconds=timeout_seconds)
        if not template_result.success:
            raise ProxmoxAPIError(
                f"Template conversion failed: {template_result.exitstatus or template_result.status or 'unknown'}"
            )
        details["completed_stages"].append("template")
    except (ProxmoxTaskTimeout, ProxmoxAPIError) as exc:
        if cancelled():
            return
        record_event_failure(
            event,
            public_failure(
                exc,
                operation="clone_guest_to_template_task",
                fallback=PROVIDER_FAILURE_MESSAGE,
                code=ERROR_CODE_PROVIDER,
            ),
            details=details,
            save=False,
        )
        clear_live_guest_caches(cluster=cluster)
        event.save(update_fields=["outcome", "details"])
        return

    if cancelled():
        return
    event.outcome = "success"
    details["stage"] = "completed"
    details["finished_at"] = timezone.now().isoformat()
    event.details = details
    clear_live_guest_caches(cluster=cluster)
    event.save(update_fields=["outcome", "details"])

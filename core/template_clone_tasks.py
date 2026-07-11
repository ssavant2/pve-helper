from __future__ import annotations

from urllib.parse import quote

from django.utils import timezone

from .models import AuditEvent
from .services.proxmox import ProxmoxAPIError, ProxmoxClient, ProxmoxTaskTimeout, clear_live_guest_caches


def clone_guest_to_template_task(
    audit_event_id: int,
    endpoint_url: str,
    node: str,
    new_vmid: int,
    clone_upid: str,
    timeout_seconds: int,
) -> None:
    event = AuditEvent.objects.filter(pk=audit_event_id).first()
    if event is None:
        return
    details = dict(event.details) if isinstance(event.details, dict) else {}
    client = ProxmoxClient(endpoint_url)

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
                "proxmox_endpoint": endpoint_url,
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
        event.outcome = "failed"
        details["error"] = str(exc)
        details["finished_at"] = timezone.now().isoformat()
        event.details = details
        clear_live_guest_caches()
        event.save(update_fields=["outcome", "details"])
        return

    if cancelled():
        return
    event.outcome = "success"
    details["stage"] = "completed"
    details["finished_at"] = timezone.now().isoformat()
    event.details = details
    clear_live_guest_caches()
    event.save(update_fields=["outcome", "details"])

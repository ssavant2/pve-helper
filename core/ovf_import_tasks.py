"""Django-Q worker for OVA/OVF imports."""

from __future__ import annotations

from django.utils import timezone

from core.models import AuditEvent, StorageMount
from core.services.proxmox import clear_live_guest_caches
from core.services.vm_register import import_ovf_package_as_vm


def import_ovf_package_task(
    audit_event_id: int,
    node: str,
    params: dict,
    source_storage_id: str,
    source_path: str,
) -> None:
    """Import all OVF disks and keep one audit event current throughout."""
    event = AuditEvent.objects.filter(pk=audit_event_id).first()
    if event is None:
        return
    details = dict(event.details) if isinstance(event.details, dict) else {}

    def progress(stage: str, index: int, total: int) -> None:
        details.update({"stage": stage, "disk_index": index, "disk_total": total})
        event.details = details
        event.save(update_fields=["details"])

    storage = StorageMount.objects.filter(storage_id=source_storage_id, enabled=True).first()
    if storage is None:
        event.outcome = "failed"
        details.update({"error": "Source storage is no longer available.", "finished_at": timezone.now().isoformat()})
        event.details = details
        event.save(update_fields=["outcome", "details"])
        return

    upids, error = import_ovf_package_as_vm(
        node,
        params,
        source_storage=storage,
        source_path=source_path,
        progress=progress,
    )
    if upids:
        details["proxmox_task_upid"] = upids[-1]
        details["proxmox_task_upids"] = upids
        details["proxmox_task_node"] = node
    details["finished_at"] = timezone.now().isoformat()
    if error:
        event.outcome = "failed"
        details["error"] = error
    else:
        event.outcome = "success"
        details["stage"] = "completed"
    event.details = details
    # A completed Recent Task must always be followed by a fresh guest list.
    clear_live_guest_caches()
    event.save(update_fields=["outcome", "details"])

    if not error:
        # Reuse the existing partial storage refresh after imports without
        # coupling the browser UI to a full storage scan.
        from core.tasks import _refresh_import_target_inventory

        _refresh_import_target_inventory(
            str(params.get("target_storage", "")),
            str(params.get("vmid", "")),
            node=node,
        )

"""Django-Q worker for OVA/OVF imports."""

from __future__ import annotations

from django.utils import timezone

from core.models import AuditEvent, StorageMount
from core.services.durable_guest_operations import (
    DurableGuestOperationError,
    client_for_audit_event,
)
from core.services.proxmox import clear_live_guest_caches
from core.services.storage_mounts import resolve_storage_mount
from core.services.vm_register import import_ovf_package_as_vm


def import_ovf_package_task(
    audit_event_id: int,
    node: str = "",
    params: dict | None = None,
    source_mount_ref: str = "",
    source_path: str = "",
    source_storage_id: str = "",
) -> None:
    """Import all OVF disks and keep one audit event current throughout."""
    event = AuditEvent.objects.filter(pk=audit_event_id).first()
    if event is None:
        return
    details = dict(event.details) if isinstance(event.details, dict) else {}
    try:
        _client, ref, cluster = client_for_audit_event(event)
    except DurableGuestOperationError as exc:
        event.outcome = "failed"
        event.details = {**details, "error": str(exc), "finished_at": timezone.now().isoformat()}
        event.save(update_fields=["outcome", "details"])
        return
    node = node or ref.node
    params = params if isinstance(params, dict) else details.get("params", {})
    source_mount_ref = (
        source_mount_ref
        or str(details.get("source_mount_ref") or "")
        or source_storage_id
        or str(details.get("source_storage_id") or "")
    )
    source_path = source_path or str(details.get("source_path") or "")
    if not node or not isinstance(params, dict):
        event.outcome = "failed"
        event.details = {
            **details,
            "error": "The queued OVF import has incomplete target data.",
            "finished_at": timezone.now().isoformat(),
        }
        event.save(update_fields=["outcome", "details"])
        return

    def progress(stage: str, index: int, total: int) -> None:
        details.update({"stage": stage, "disk_index": index, "disk_total": total})
        event.details = details
        event.save(update_fields=["details"])

    try:
        storage = resolve_storage_mount(source_mount_ref, enabled=True)
    except StorageMount.DoesNotExist:
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
        cluster=cluster,
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
    clear_live_guest_caches(cluster=cluster)
    event.save(update_fields=["outcome", "details"])

    if not error:
        # Reuse the existing partial storage refresh after imports without
        # coupling the browser UI to a full storage scan.
        from core.tasks import _refresh_import_target_inventory

        _refresh_import_target_inventory(
            str(params.get("target_storage", "")),
            str(params.get("vmid", "")),
            node=node,
            cluster=cluster,
        )

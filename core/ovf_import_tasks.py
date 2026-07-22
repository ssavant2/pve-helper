"""Django-Q worker for OVA/OVF imports."""

from __future__ import annotations

from django.utils import timezone

from core.models import AuditEvent, StorageMount
from core.services.durable_guest_operations import (
    DurableGuestOperationError,
    client_for_audit_event,
)
from core.services.proxmox import clear_live_guest_caches
from core.services.public_errors import ERROR_CODE_INCOMPLETE, ERROR_CODE_PROVIDER, PublicFailure
from core.services.storage_mounts import resolve_storage_mount
from core.services.task_failures import record_event_exception, record_event_failure
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
        record_event_exception(
            event,
            exc,
            operation="import_ovf_package_task.resolve_target",
            fallback="The queued OVF import could not be resolved to a cluster target.",
            details=details,
        )
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
        record_event_failure(
            event,
            PublicFailure("The queued OVF import has incomplete target data.", ERROR_CODE_INCOMPLETE),
            details=details,
        )
        return

    def progress(stage: str, index: int, total: int) -> None:
        details.update({"stage": stage, "disk_index": index, "disk_total": total})
        event.details = details
        event.save(update_fields=["details"])

    try:
        storage = resolve_storage_mount(source_mount_ref, enabled=True)
    except StorageMount.DoesNotExist:
        record_event_failure(
            event,
            PublicFailure("Source storage is no longer available.", ERROR_CODE_INCOMPLETE),
            details=details,
        )
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
    if error:
        # `import_ovf_package_as_vm` already returns public text.
        record_event_failure(event, PublicFailure(error, ERROR_CODE_PROVIDER), details=details, save=False)
        details = dict(event.details)
    else:
        event.outcome = "success"
        details["stage"] = "completed"
        details["finished_at"] = timezone.now().isoformat()
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

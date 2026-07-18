from __future__ import annotations

from urllib.parse import quote

from django.http import JsonResponse
from django.views.decorators.http import require_POST

from core.views import common
from core.models import ProxmoxInventory
from core.services.guests import is_template
from core.services.public_errors import public_exception_message
from core.views.guests.operation_lifecycle import _audit_guest, _guest_post_with_client
from core.views.guests.read_model_support import _require_guest


def _json_result(ok: bool, error: str = "") -> JsonResponse:
    return JsonResponse({"ok": ok, "errors": [error] if error else []})


@require_POST
def clone_guest_to_template(request, cluster_key: str, object_type: str, vmid: int):
    if not request.user.is_authenticated:
        return _json_result(False, "Authentication required.")
    if object_type != ProxmoxInventory.ObjectType.VM:
        return _json_result(False, "Only VMs can be cloned to a template.")

    detail = _require_guest(object_type, vmid, cluster_key=cluster_key)
    newid_text = request.POST.get("clone_newid", "").strip()
    clone_name = request.POST.get("clone_name", "").strip()
    storage = request.POST.get("clone_storage", "").strip()
    if not newid_text.isdigit() or int(newid_text) <= 0:
        return _json_result(False, "New VMID must be a positive whole number.")
    if not clone_name:
        return _json_result(False, "Name is required.")
    newid = int(newid_text)
    if not detail.node:
        return _json_result(False, "Could not resolve the template's node.")

    client = None
    try:
        for candidate in common.cluster_scoped_clients(detail.cluster):
            try:
                fresh_config = candidate.guest_config(node=detail.node, object_type=object_type, vmid=vmid)
                fresh_current = candidate.guest_current(node=detail.node, object_type=object_type, vmid=vmid)
            except Exception:
                continue
            client = candidate
            break
        if client is None:
            return _json_result(False, "Could not read the template from Proxmox.")
        if not is_template(fresh_config):
            return _json_result(False, "The selected guest is no longer a template.")
        if str((fresh_current or {}).get("status") or "").lower() != "stopped":
            return _json_result(False, "The template must be stopped before cloning it to a template.")
        used_vmids = {
            guest.vmid
            for guest in common.fetch_live_guest_inventory(
                use_cache=False,
                cluster=detail.cluster,
            )
            if guest.vmid is not None
        }
        if newid in used_vmids:
            return _json_result(False, f"VMID {newid} is already in use.")
        raw_storages = client.get(f"nodes/{quote(detail.node, safe='')}/storage")
        valid_storages = {
            str(item.get("storage"))
            for item in (raw_storages if isinstance(raw_storages, list) else [])
            if isinstance(item, dict)
            and item.get("storage")
            and item.get("active", 1)
            and "images" in {part.strip() for part in str(item.get("content", "")).split(",")}
        }
        if storage and storage not in valid_storages:
            return _json_result(False, f"Storage '{storage}' cannot hold VM disks on {detail.node}.")
    except Exception as exc:  # noqa: BLE001 - preflight spans provider and projection reads
        return _json_result(
            False,
            public_exception_message(
                exc,
                operation="template_clone_preflight",
                fallback="Clone preflight could not be completed.",
            ),
        )

    clone_data: dict[str, object] = {"newid": newid, "full": 1, "name": clone_name}
    if storage:
        clone_data["storage"] = storage
    response, error, client = _guest_post_with_client(detail, "clone", clone_data)
    if error or not isinstance(response, str) or not response.startswith("UPID:") or client is None:
        message = error or "Proxmox did not return a clone task ID."
        event = _audit_guest(
            request,
            detail,
            "guest.template.clone",
            {"source_vmid": detail.vmid, "new_vmid": newid, "new_name": clone_name, "storage": storage, "stage": "clone"},
            outcome="failed",
        )
        event.details = {**event.details, "error": message}
        event.save(update_fields=["details"])
        return _json_result(False, f"Clone to template failed: {message}")

    event = _audit_guest(
        request,
        detail,
        "guest.template.clone",
        {
            "source_vmid": detail.vmid,
            "new_vmid": newid,
            "new_name": clone_name,
            "storage": storage,
            "stage": "clone",
            "proxmox_task_upid": response,
            "proxmox_task_node": detail.node,
            "proxmox_endpoint": getattr(client, "endpoint", ""),
        },
        outcome="running",
    )
    task_id = common.enqueue_bulk_task(
        "core.template_clone_tasks.clone_guest_to_template_task",
        event.id,
    )
    event.details = {**event.details, "worker_task_id": task_id}
    event.save(update_fields=["details"])
    return _json_result(True)

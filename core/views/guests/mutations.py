"""Standalone guest mutation endpoints: power, snapshot, backup, bulk-nics (from _core)."""
from __future__ import annotations
from ..common import *  # noqa: F401,F403
from .. import common
from ._core import (_audit_guest,_backup_error,_delete_all_guest_snapshots,_finish_guest_running_audit,_guest_action_response,_guest_delete_with_client,_guest_nic_bridges,_guest_post_with_client,_parse_guest_target_value,_queue_guest_backup_restore,_require_guest,_restore_archive_from_key,_restore_options,_snapshot_error,_submit_guest_backup,_vm_write_disabled_redirect,_wants_task_json)


@require_POST
@app_login_required
def guest_power(request, object_type: str, vmid: int):
    def result(error_label: str = ""):
        return _guest_action_response(request, object_type, vmid, error_label, redirect_name="core:guest_summary")

    disabled = _vm_write_disabled_redirect(request, object_type, vmid, "core:guest_summary")
    if disabled:
        return result("VM/CT writes are disabled.") if _wants_task_json(request) else disabled
    detail = _require_guest(object_type, vmid)
    action = request.POST.get("action", "")
    if action not in GUEST_POWER_ACTIONS:
        return result("Unknown power action.")
    if action in VM_ONLY_POWER_ACTIONS and object_type != ProxmoxInventory.ObjectType.VM:
        return result("This action is only available for VMs.")
    subpath, params = POWER_ACTION_REQUESTS[action]
    running_event = _audit_guest(request, detail, f"guest.power.{action}", outcome="running")
    data, err, client = _guest_post_with_client(detail, subpath, params)
    if err:
        error_label = proxmox_permission_hint("VM.PowerMgmt") if "403" in err else f"Power action failed: {err}"
        _finish_guest_running_audit(running_event, detail, data, client, err=error_label)
        return result(error_label)
    _finish_guest_running_audit(running_event, detail, data, client)
    clear_live_guest_caches()
    return result()




@require_POST
@app_login_required
def guest_snapshot_create(request, object_type: str, vmid: int):
    def result(error_label: str = ""):
        return _guest_action_response(request, object_type, vmid, error_label, redirect_name="core:guest_snapshots")

    disabled = _vm_write_disabled_redirect(request, object_type, vmid, "core:guest_snapshots")
    if disabled:
        return result("VM/CT writes are disabled.") if _wants_task_json(request) else disabled
    detail = _require_guest(object_type, vmid)
    name = request.POST.get("snapname", "").strip()
    if not name:
        return result("Snapshot name is required.")
    if not SNAPSHOT_NAME_RE.match(name):
        return result(SNAPSHOT_NAME_HELP)
    data = {"snapname": name}
    description = request.POST.get("description", "").strip()
    if description:
        data["description"] = description
    # vmstate (include RAM) only exists for QEMU VMs; LXC has no such option.
    if object_type == ProxmoxInventory.ObjectType.VM and request.POST.get("vmstate") == "on":
        data["vmstate"] = 1
    running_event = _audit_guest(request, detail, "guest.snapshot.create", {"snapshot": name}, outcome="running")
    response, err, client = _guest_post_with_client(detail, "snapshot", data)
    if err:
        error_label = _snapshot_error(err)
        _finish_guest_running_audit(running_event, detail, response, client, err=error_label, audit_details={"snapshot": name})
        return result(error_label)
    _finish_guest_running_audit(running_event, detail, response, client, audit_details={"snapshot": name})
    return result()




@require_POST
@app_login_required
def guest_snapshot_delete(request, object_type: str, vmid: int, snapname: str):
    def result(error_label: str = ""):
        return _guest_action_response(request, object_type, vmid, error_label, redirect_name="core:guest_snapshots")

    disabled = _vm_write_disabled_redirect(request, object_type, vmid, "core:guest_snapshots")
    if disabled:
        return result("VM/CT writes are disabled.") if _wants_task_json(request) else disabled
    detail = _require_guest(object_type, vmid)
    running_event = _audit_guest(request, detail, "guest.snapshot.delete", {"snapshot": snapname}, outcome="running")
    response, err, client = _guest_delete_with_client(detail, f"snapshot/{quote(snapname, safe='')}")
    if err:
        error_label = _snapshot_error(err)
        _finish_guest_running_audit(running_event, detail, response, client, err=error_label, audit_details={"snapshot": snapname})
        return result(error_label)
    _finish_guest_running_audit(running_event, detail, response, client, audit_details={"snapshot": snapname})
    return result()




@require_POST
@app_login_required
def guest_snapshot_delete_all(request, object_type: str, vmid: int):
    def result(error_label: str = ""):
        return _guest_action_response(request, object_type, vmid, error_label, redirect_name="core:guest_snapshots")

    disabled = _vm_write_disabled_redirect(request, object_type, vmid, "core:guest_snapshots")
    if disabled:
        return result("VM/CT writes are disabled.") if _wants_task_json(request) else disabled
    detail = _require_guest(object_type, vmid)
    running_event = _audit_guest(request, detail, "guest.snapshot.delete_all", outcome="running")
    deleted, err = _delete_all_guest_snapshots(detail)
    if err:
        error_label = _snapshot_error(err)
        _finish_guest_running_audit(running_event, detail, None, None, err=error_label)
        return result(error_label)
    _finish_guest_running_audit(running_event, detail, None, None, audit_details={"deleted": deleted})
    return result()




@require_POST
@app_login_required
def guest_snapshot_rollback(request, object_type: str, vmid: int, snapname: str):
    def result(error_label: str = ""):
        return _guest_action_response(request, object_type, vmid, error_label, redirect_name="core:guest_snapshots")

    disabled = _vm_write_disabled_redirect(request, object_type, vmid, "core:guest_snapshots")
    if disabled:
        return result("VM/CT writes are disabled.") if _wants_task_json(request) else disabled
    detail = _require_guest(object_type, vmid)
    running_event = _audit_guest(request, detail, "guest.snapshot.rollback", {"snapshot": snapname}, outcome="running")
    response, err, client = _guest_post_with_client(detail, f"snapshot/{quote(snapname, safe='')}/rollback")
    if err:
        error_label = _snapshot_error(err)
        _finish_guest_running_audit(running_event, detail, response, client, err=error_label, audit_details={"snapshot": snapname})
        return result(error_label)
    _finish_guest_running_audit(running_event, detail, response, client, audit_details={"snapshot": snapname})
    return result()




@require_POST
@app_login_required
def guest_backup_now(request, object_type, vmid):
    def result(error_label: str = ""):
        return _guest_action_response(request, object_type, vmid, error_label, redirect_name="core:guest_backup")

    disabled = _vm_write_disabled_redirect(request, object_type, vmid, "core:guest_backup")
    if disabled:
        return result("VM/CT writes are disabled.") if _wants_task_json(request) else disabled
    detail = _require_guest(object_type, vmid)
    response, err, client, audit_details = _submit_guest_backup(request, detail)
    running_event = _audit_guest(request, detail, "guest.backup.run", audit_details, outcome="running")
    if err:
        error_label = _backup_error(err)
        _finish_guest_running_audit(running_event, detail, response, client, err=error_label, audit_details=audit_details)
        return result(error_label)
    _finish_guest_running_audit(
        running_event,
        detail,
        response,
        client,
        audit_details=audit_details,
        timeout_seconds=settings.BACKUP_TASK_TIMEOUT_SECONDS,
    )
    return result()




@require_POST
@app_login_required
def guest_backup_delete(request, object_type, vmid):
    def result(error_label: str = ""):
        return _guest_action_response(request, object_type, vmid, error_label, redirect_name="core:guest_backup")

    disabled = _vm_write_disabled_redirect(request, object_type, vmid, "core:guest_backup")
    if disabled:
        return result("VM/CT writes are disabled.") if _wants_task_json(request) else disabled
    detail = _require_guest(object_type, vmid)
    volid = request.POST.get("volid", "").strip()
    storage = request.POST.get("storage", "").strip()
    if not volid or not storage:
        return result("Missing backup reference.")
    response = None
    client = None
    err = "No Proxmox endpoint could reach this guest."
    for client in common.configured_clients():
        try:
            response = client.delete(f"nodes/{quote(detail.node, safe='')}/storage/{quote(storage, safe='')}/content/{quote(volid, safe='')}")
            err = ""
            break
        except ProxmoxAPIError as exc:
            err = str(exc)
    running_event = _audit_guest(request, detail, "guest.backup.delete", {"storage": storage, "volid": volid}, outcome="running")
    if err:
        _finish_guest_running_audit(running_event, detail, response, client, err=f"Delete backup failed: {err}")
        return result(f"Delete backup failed: {err}")
    _finish_guest_running_audit(
        running_event,
        detail,
        response,
        client,
        audit_details={"storage": storage, "volid": volid},
        timeout_seconds=settings.BACKUP_TASK_TIMEOUT_SECONDS,
    )
    return result()




@app_login_required
def guest_backup_restore(request):
    if not settings.VM_WRITE_ENABLED:
        messages.error(request, "VM/CT restore is disabled (VM_WRITE_ENABLED is off).")
        return redirect("core:vms")
    archives, nodes, storage_options, nextid = _restore_options()
    restore_error = ""
    selected_archive_key = request.POST.get("archive_key", "") if request.method == "POST" else request.GET.get("archive", "")
    source_type = (request.POST.get("source_type", "") if request.method == "POST" else request.GET.get("source_type", "")).strip()
    source_vmid_text = (
        request.POST.get("source_vmid", "") if request.method == "POST" else request.GET.get("source_vmid", "")
    ).strip()
    source_vmid = int(source_vmid_text) if source_vmid_text.isdigit() and int(source_vmid_text) > 0 else None
    if source_type not in {ProxmoxInventory.ObjectType.VM, ProxmoxInventory.ObjectType.CT}:
        source_type = ""
        source_vmid = None
    if request.method != "POST" and not selected_archive_key:
        storage_hint = request.GET.get("storage", "").strip()
        path_hint = request.GET.get("path", "").strip()
        hinted_volid = f"{storage_hint}:{path_hint}" if storage_hint and path_hint else ""
        selected_archive_key = next(
            (
                archive["key"]
                for archive in archives
                if archive["storage"] == storage_hint and archive["volid"] == hinted_volid
            ),
            "",
        )
    selected_archive = _restore_archive_from_key(selected_archive_key, archives) if selected_archive_key else None
    if selected_archive is not None and source_vmid is None:
        source_type = selected_archive["object_type"]
        source_vmid = selected_archive.get("source_vmid")
    if source_type and source_vmid:
        archives = [
            archive
            for archive in archives
            if archive["object_type"] == source_type and archive.get("source_vmid") == source_vmid
        ]
    elif selected_archive is not None:
        archives = [
            archive
            for archive in archives
            if archive["object_type"] == selected_archive["object_type"]
            and archive.get("source_vmid") == selected_archive.get("source_vmid")
        ]
    if request.method == "POST":
        error = _queue_guest_backup_restore(request, archives)
        if error:
            restore_error = error
        else:
            return redirect("core:vms")
    context = {
        **navigation_context("vms"),
        "archives": archives,
        "nodes": nodes,
        "storage_options": storage_options,
        "nextid": nextid,
        "selected_archive_key": selected_archive_key,
        "restore_error": restore_error,
        "source_type": source_type,
        "source_vmid": source_vmid or "",
        "form_values": request.POST
        if request.method == "POST"
        else {"node": nodes[0]["key"] if nodes else "", "vmid": nextid},
    }
    return render(request, "core/guest_backup_restore.html", context)




@require_POST
@app_login_required
def guest_bulk_nics(request):
    """Per-guest NIC bridges for a set of guests, for the bulk-migrate network
    preflight (which guests would land without a network on the target node)."""
    if not settings.VM_WRITE_ENABLED:
        return JsonResponse({"error": "VM/CT writes are disabled."}, status=403)
    guests: list[dict] = []
    for value in request.POST.getlist("guest"):
        object_type, vmid, node = _parse_guest_target_value(value)
        if not object_type or vmid is None:
            continue
        try:
            detail = _require_guest(object_type, vmid, node=node)
        except Http404:
            continue
        guests.append(
            {
                "target": value,
                "label": detail.name or str(vmid),
                "bridges": [nic["bridge"] for nic in _guest_nic_bridges(detail)],
            }
        )
    return JsonResponse({"guests": guests})





"""Guest bulk-action dispatcher (vms_bulk_action) + per-domain handlers (from _core)."""
from __future__ import annotations
from ..common import *  # noqa: F401,F403
from .. import common
from ._core import (MIGRATE_KINDS, SNAPSHOT_NAME_HELP, SNAPSHOT_NAME_RE, _MIGRATE_ACTIVE_STATES, _apply_migrate_net_remap, _backup_error, _delete_all_guest_snapshots, _guest_movable_disks, _snapshot_error, _split_tag_text, _submit_guest_backup, _template_linked_clone_children, _template_storage_paths, _unique_tags, _update_current_guest_config)
from .operation_lifecycle import (_MIGRATE_ASYNC, _audit_guest, _finish_guest_running_audit, _guest_destroy_with_client, _guest_post_with_client, _guest_ref_from_target_value)
from .presenters import _config_enabled
from .read_model_support import (_config_storage_ids, _guest_agent_config_enabled, _guest_pool_memberships, _linked_clone_children, _require_guest)
from core.services.public_errors import public_exception_message
from core.services.tags import TagValidationError, validate_tag


@require_POST
@app_login_required
def vms_bulk_action(request):
    wants_json = request.headers.get("X-Requested-With") == "fetch" or "application/json" in request.headers.get("Accept", "")

    def done(ok: bool = True, errors: list[str] | None = None):
        if wants_json:
            return JsonResponse({"ok": ok and not errors, "errors": errors or []})
        return None

    action = request.POST.get("bulk_action", "").strip()
    targets = request.POST.getlist("guest")
    if action not in VM_BULK_ACTIONS:
        response = done(False, ["Unknown VM/CT action."])
        if response:
            return response
        return redirect("core:vms_overview")
    if not targets:
        response = done(False, ["No VM/CT targets selected."])
        if response:
            return response
        return redirect("core:vms_overview")

    snapshot_name = request.POST.get("snapshot_name", "").strip()
    if action == "snapshot" and not snapshot_name:
        response = done(False, ["Snapshot name is required."])
        if response:
            return response
        return redirect("core:vms_overview")
    if action == "snapshot" and not SNAPSHOT_NAME_RE.match(snapshot_name):
        if wants_json:
            return done(False, [SNAPSHOT_NAME_HELP])
        messages.error(request, SNAPSHOT_NAME_HELP)
        return redirect("core:vms_overview")
    if action == "clone" and len(targets) != 1:
        response = done(False, ["Clone requires exactly one selected guest."])
        if response:
            return response
        return redirect("core:vms_overview")
    if action == "destroy" and len(targets) != 1:
        response = done(False, ["Destroy requires exactly one selected guest."])
        if response:
            return response
        return redirect("core:vms_overview")
    if action == "untemplate" and len(targets) != 1:
        response = done(False, ["Convert template to VM requires exactly one selected template."])
        if response:
            return response
        return redirect("core:vms_overview")
    # Migrate supports one or many guests for every kind: host/both migrate each
    # guest to the target node, storage moves each guest's volumes to the target
    # storage — all per-guest in the loop below (bulk skips per-guest NIC remap).
    if action == "pool" and "pool_id" not in request.POST:
        response = done(False, ["Choose a target pool or No pool."])
        if response:
            return response
        return redirect("core:vms_overview")
    if action == "tags" and request.POST.get("tags_mode", "").strip() not in {"add", "remove", "replace"}:
        response = done(False, ["Unknown tag update mode."])
        if response:
            return response
        return redirect("core:vms_overview")

    errors = []
    for target in targets:
        ref = _guest_ref_from_target_value(target)
        if ref is None:
            errors.append(f"Invalid target: {target}")
            continue
        try:
            detail = _require_guest(ref)
        except Http404:
            errors.append(f"Could not find target: {target}")
            continue

        audit_action, initial_audit_details = _bulk_action_initial_audit_details(
            request,
            action,
            detail,
            snapshot_name,
        )
        running_event = _audit_guest(request, detail, audit_action, initial_audit_details, outcome="running")
        response = None
        client = None
        if action == "snapshot":
            response, err, client = _guest_post_with_client(detail, "snapshot", {"snapname": snapshot_name})
            audit_details = {"snapshot": snapshot_name}
            error_label = _snapshot_error(err) if err else ""
        elif action == "delete_snapshots":
            deleted, err = _delete_all_guest_snapshots(detail)
            audit_details = {"deleted": deleted}
            error_label = _snapshot_error(err) if err else ""
        elif action == "clone":
            err, audit_details, response, client = _clone_guest_from_bulk_request(request, detail)
            error_label = f"Clone failed: {err}" if err else ""
        elif action == "tags":
            err, audit_details = _update_guest_tags_from_bulk_request(request, detail)
            error_label = f"Tag update failed: {err}" if err else ""
            response = None
            client = None
        elif action in {"agent_enable", "agent_disable"}:
            err, audit_details, response, client = _set_guest_agent_from_bulk_request(
                detail,
                enabled=action == "agent_enable",
            )
            error_label = f"Guest agent update failed: {err}" if err else ""
        elif action == "destroy":
            err, audit_details, response, client = _destroy_guest_from_bulk_request(request, detail)
            error_label = f"Destroy failed: {err}" if err else ""
        elif action == "template":
            if detail.object_type != ProxmoxInventory.ObjectType.VM:
                _finish_guest_running_audit(
                    running_event,
                    detail,
                    None,
                    None,
                    err="Only VMs can be converted to templates.",
                )
                errors.append("Only VMs can be converted to templates.")
                continue
            if detail.vmid in common.fetch_live_guest_lineage(cluster=detail.cluster):
                msg = (
                    f"{detail.name or detail.vmid} is a linked clone; converting it to a "
                    "template would create a fragile chained lineage. Full-clone it first."
                )
                _finish_guest_running_audit(running_event, detail, None, None, err=msg)
                errors.append(msg)
                continue
            response, err, client = _guest_post_with_client(detail, "template")
            audit_details = None
            error_label = f"Template conversion failed: {err}" if err else ""
        elif action == "untemplate":
            err, audit_details, response, client = _convert_template_back_to_vm(request, detail)
            error_label = f"Template conversion failed: {err}" if err else ""
        elif action == "pool":
            err, audit_details = _move_guest_to_pool_from_bulk_request(request, detail)
            response = None
            client = None
            error_label = f"Pool update failed: {err}" if err else ""
        elif action == "migrate":
            err, audit_details, response, client = _migrate_guest_from_bulk_request(request, detail, running_event)
            error_label = f"Migrate failed: {err}" if err else ""
        elif action == "backup":
            response, err, client, audit_details = _submit_guest_backup(request, detail)
            error_label = _backup_error(err) if err else ""
        else:
            subpath, params = POWER_ACTION_REQUESTS.get(action, (f"status/{action}", {}))
            response, err, client = _guest_post_with_client(detail, subpath, params)
            audit_details = None
            error_label = f"Power action failed: {err}" if err else ""

        if err:
            _finish_guest_running_audit(
                running_event,
                detail,
                response,
                client,
                err=error_label,
                audit_details=audit_details,
            )
            errors.append(error_label)
            continue

        _finish_guest_running_audit(
            running_event,
            detail,
            response,
            client,
            audit_details=audit_details,
            timeout_seconds=settings.BACKUP_TASK_TIMEOUT_SECONDS if action == "backup" else None,
        )
        if action == "template":
            _update_current_guest_config(detail, {"template": "1"}, [])
        if action == "untemplate":
            _update_current_guest_config(detail, {"template": "0"}, [])
        if action in GUEST_POWER_ACTIONS or action in {"template", "untemplate", "pool", "migrate", "clone", "tags", "destroy", "agent_enable", "agent_disable", "backup"}:
            clear_live_guest_caches(cluster=detail.cluster)

    response = done(not errors, errors)
    if response:
        return response

    redirect_to = request.POST.get("next", "").strip()
    if redirect_to and url_has_allowed_host_and_scheme(
        redirect_to,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(redirect_to)
    return redirect("core:vms_overview")




def _bulk_action_initial_audit_details(
    request,
    action: str,
    detail: SimpleNamespace,
    snapshot_name: str = "",
) -> tuple[str, dict]:
    if action == "snapshot":
        return "guest.snapshot.create", {"snapshot": snapshot_name}
    if action == "delete_snapshots":
        return "guest.snapshot.delete_all", {}
    if action == "clone":
        newid = request.POST.get("clone_newid", "").strip()
        clone_name = request.POST.get("clone_name", "").strip()
        storage = request.POST.get("clone_storage", "").strip()
        details = {
            "source_vmid": detail.vmid,
            "new_name": clone_name,
            "full": request.POST.get("clone_full") == "1",
            "storage": storage,
        }
        if newid.isdigit():
            details["new_vmid"] = int(newid)
        elif newid:
            details["new_vmid"] = newid
        return "guest.clone.create", details
    if action == "tags":
        return "guest.tags.updated", {
            "mode": request.POST.get("tags_mode", "").strip(),
            "tags": _split_tag_text(request.POST.get("tags_value", "")),
        }
    if action == "agent_enable":
        return "guest.agent.enable", {"agent": "enabled"}
    if action == "agent_disable":
        return "guest.agent.disable", {"agent": "disabled"}
    if action == "destroy":
        return "guest.destroy", {
            "purge": request.POST.get("destroy_purge") == "1",
            "destroy_unreferenced_disks": request.POST.get("destroy_unreferenced_disks") == "1",
        }
    if action == "template":
        return "guest.template.convert", {}
    if action == "untemplate":
        return "guest.template.revert", {}
    if action == "pool":
        return "guest.pool.updated", {"pool": request.POST.get("pool_id", "").strip()}
    if action == "migrate":
        return "guest.migrate", {
            "kind": request.POST.get("migrate_kind", "").strip(),
            "source_node": detail.node,
            "target_node": request.POST.get("migrate_target_node", "").strip(),
            "target_storage": request.POST.get("migrate_target_storage", "").strip(),
        }
    if action == "backup":
        return "guest.backup.run", {
            "storage": request.POST.get("storage", "").strip(),
            "mode": request.POST.get("mode", "snapshot").strip(),
            "compression": request.POST.get("compress", "zstd").strip(),
        }
    return f"guest.power.{action}", {}




def _clone_guest_from_bulk_request(request, detail: SimpleNamespace) -> tuple[str, dict, object | None, object | None]:
    newid = request.POST.get("clone_newid", "").strip()
    clone_name = request.POST.get("clone_name", "").strip()
    storage = request.POST.get("clone_storage", "").strip()
    full = request.POST.get("clone_full") == "1"
    if not newid.isdigit() or int(newid) <= 0:
        return "New VMID must be a positive whole number.", {}, None, None
    if not clone_name:
        return "Name is required.", {"new_vmid": int(newid)}, None, None
    if not detail.node:
        return "Could not resolve the guest's current node.", {}, None, None

    data: dict[str, object] = {"newid": newid, "full": 1 if full else 0}
    if clone_name:
        data["name" if detail.object_type == ProxmoxInventory.ObjectType.VM else "hostname"] = clone_name
    if storage and full:
        data["storage"] = storage

    response, err, client = _guest_post_with_client(detail, "clone", data)
    # The clone's disks land on the source's storage (linked / same-storage full)
    # or the chosen target storage; rescan those so the new disks reclassify at once.
    rescan_storages = list(_config_storage_ids(detail.config))
    if storage and full and storage not in rescan_storages:
        rescan_storages.append(storage)
    audit_details = {
        "source_vmid": detail.vmid,
        "new_vmid": int(newid),
        "new_name": clone_name,
        "full": full,
        "storage": storage,
        "rescan_storage_ids": rescan_storages,
    }
    return err or "", audit_details, response, client




def _move_guest_to_pool_from_bulk_request(request, detail: SimpleNamespace) -> tuple[str, dict]:
    """Move one guest between PVE pools with a rollback on a failed add."""
    target_pool = request.POST.get("pool_id", "").strip()
    client = None
    pools: list[str] = []
    memberships: list[str] = []
    for candidate in common.cluster_scoped_clients(detail.cluster):
        try:
            # Resolve both the guest and the pool list through the same endpoint.
            # Pools are cluster-local, not globally shared between configured PVE
            # endpoints.
            candidate.guest_current(node=detail.node, object_type=detail.object_type, vmid=detail.vmid)
            pools, memberships = _guest_pool_memberships(candidate, detail)
            client = candidate
            break
        except ProxmoxAPIError:
            continue
    if client is None:
        return "Could not read the current pool membership from Proxmox.", {}
    if len(memberships) > 1:
        return "This guest appears in multiple pools; resolve that inconsistent Proxmox state before moving it.", {
            "previous_pools": memberships,
            "target_pool": target_pool,
        }
    if target_pool and target_pool not in pools:
        return f"Pool '{target_pool}' no longer exists on this Proxmox endpoint.", {"target_pool": target_pool}

    current_pool = memberships[0] if memberships else ""
    audit_details = {
        "previous_pool": current_pool,
        "target_pool": target_pool,
    }
    if current_pool == target_pool:
        audit_details["noop"] = True
        return "", audit_details

    if current_pool:
        try:
            client.put(f"pools/{quote(current_pool, safe='')}", data={"vms": str(detail.vmid), "delete": 1})
        except ProxmoxAPIError as exc:
            return public_exception_message(
                exc,
                operation="guest_pool_remove",
                fallback="Proxmox could not remove the guest from its current pool.",
            ), audit_details

    if not target_pool:
        return "", audit_details

    try:
        client.put(f"pools/{quote(target_pool, safe='')}", data={"vms": str(detail.vmid)})
    except ProxmoxAPIError as exc:
        rollback_error = ""
        if current_pool:
            try:
                client.put(f"pools/{quote(current_pool, safe='')}", data={"vms": str(detail.vmid)})
            except ProxmoxAPIError as rollback_exc:
                public_exception_message(
                    rollback_exc,
                    operation="guest_pool_rollback",
                    fallback="Pool rollback failed.",
                )
                rollback_error = f" Rollback to '{current_pool}' also failed."
        error = public_exception_message(
            exc,
            operation="guest_pool_add",
            fallback="Proxmox could not add the guest to the selected pool.",
        )
        return error + rollback_error, audit_details
    return "", audit_details




def _migrate_guest_from_bulk_request(request, detail: SimpleNamespace, running_event) -> tuple[str, dict, object | None, object | None]:
    """Issue one Migrate operation (host / storage / both) for a single guest.

    host/both go through the cluster ``migrate`` endpoint (one UPID, so the same
    async poll + cancel path as clone); storage-only relocates **all** of the
    guest's volumes to the target storage via a worker task that runs the
    per-volume ``move_disk`` / ``move_volume`` operations sequentially.
    """
    kind = request.POST.get("migrate_kind", "").strip()
    target_node = request.POST.get("migrate_target_node", "").strip()
    target_storage = request.POST.get("migrate_target_storage", "").strip()
    is_vm = detail.object_type == ProxmoxInventory.ObjectType.VM
    active = str(detail.status or "").strip() in _MIGRATE_ACTIVE_STATES
    audit = {
        "kind": kind,
        "source_node": detail.node,
        "target_node": target_node,
        "target_storage": target_storage,
    }
    if kind not in MIGRATE_KINDS:
        return "Choose what to migrate (host, storage, or both).", audit, None, None
    # Relocating a template's disks (storage / both) would move the base volume
    # out from under its linked clones and orphan their backing chain.
    if kind in {"storage", "both"}:
        children = _linked_clone_children(detail)
        if children:
            labels = ", ".join(str(child) for child in children)
            return (
                "Cannot move this template's storage — linked clone(s) still depend on its "
                f"base volume: {labels}. Full-clone or delete them first.",
                {**audit, "linked_children": children},
                None,
                None,
            )
    if not detail.node:
        return "Could not resolve the guest's current node.", audit, None, None

    if kind in {"host", "both"}:
        if not target_node:
            return "Choose a target node.", audit, None, None
        if target_node == detail.node:
            return "The target node must differ from the current node.", audit, None, None
        data: dict[str, object] = {"target": target_node}
        # A running VM must migrate online (live); a running CT has no live
        # migration, so use restart migration. Stopped guests migrate offline.
        if active:
            data["online" if is_vm else "restart"] = 1
        if kind == "both":
            if not target_storage:
                return "Choose a target storage.", audit, None, None
            data["targetstorage" if is_vm else "target-storage"] = target_storage
        # Optional NIC bridge remap for bridges missing on the target. Proxmox
        # has no migrate-time network mapping, so this edits the guest config
        # (a cluster-wide, permanent change) before the migrate.
        remap_err, remapped = _apply_migrate_net_remap(request, detail)
        if remap_err:
            return remap_err, audit, None, None
        if remapped:
            audit["net_remap"] = remapped
        response, err, client = _guest_post_with_client(detail, "migrate", data)
        return err or "", audit, response, client

    # kind == "storage": relocate ALL of the guest's volumes to the target
    # storage on the same node, one move at a time (Proxmox locks the guest per
    # move), handed off to a worker task.
    if not target_storage:
        return "Choose a target storage.", audit, None, None
    disks = _guest_movable_disks(detail)
    if not disks:
        return "This guest has no movable disk/volume.", audit, None, None
    moves = [[disk["key"], target_storage] for disk in disks if disk["storage"] != target_storage]
    audit["disks"] = [disk["key"] for disk in disks]
    audit["moves"] = [move[0] for move in moves]
    if not moves:
        audit["noop"] = True
        return "", audit, None, None
    endpoint = ""
    for client in common.cluster_scoped_clients(detail.cluster):
        try:
            client.guest_current(node=detail.node, object_type=detail.object_type, vmid=detail.vmid)
            endpoint = getattr(client, "endpoint", "")
            break
        except ProxmoxAPIError:
            continue
    if not endpoint:
        return "Could not reach the guest's Proxmox endpoint.", audit, None, None
    running_event.details = {
        **(running_event.details or {}),
        "operation_payload_version": 1,
        "proxmox_endpoint": endpoint,
        "moves": moves,
        "task_timeout_seconds": settings.SCHEDULED_ACTION_TIMEOUT_SECONDS,
    }
    running_event.save(update_fields=["details"])
    common.enqueue_bulk_task(
        "core.tasks.migrate_guest_disks_task",
        running_event.id,
    )
    return "", audit, _MIGRATE_ASYNC, None




def _convert_template_back_to_vm(request, detail: SimpleNamespace) -> tuple[str, dict, object | None, object | None]:
    """Safely clear the QEMU template flag for a standalone template.

    Proxmox accepts ``template=0`` even when linked clones still reference a
    template base disk.  The API does not protect that relationship, so this
    deliberately has narrow V1 support and fails closed when it cannot prove
    the template has no children on every backing storage.
    """
    audit_details: dict[str, object] = {"operation": "template_to_vm"}
    confirmation = request.POST.get("untemplate_confirm_vmid", "").strip()
    acknowledgement = request.POST.get("untemplate_acknowledge", "").strip()
    if confirmation != str(detail.vmid):
        return "The confirmation VMID did not match.", audit_details, None, None
    if acknowledgement != "convert":
        return "Confirm that you understand this converts the template back to a VM.", audit_details, None, None
    if detail.object_type != ProxmoxInventory.ObjectType.VM:
        return "Only VM templates can be converted back to VMs.", audit_details, None, None
    if not detail.node:
        return "Could not resolve the template's current node.", audit_details, None, None

    client = None
    fresh_config: dict = {}
    current: dict = {}
    for candidate in common.cluster_scoped_clients(detail.cluster):
        try:
            fresh_config = candidate.guest_config(node=detail.node, object_type=detail.object_type, vmid=detail.vmid)
            current = candidate.guest_current(node=detail.node, object_type=detail.object_type, vmid=detail.vmid)
            client = candidate
            break
        except ProxmoxAPIError:
            continue
    if client is None:
        return "Could not read the template's current configuration from Proxmox.", audit_details, None, None

    if not is_template(fresh_config):
        return "This guest is no longer a template.", audit_details, None, client
    status = str(current.get("status") or "")
    if status != "stopped":
        return "Stop the template before converting it back to a VM.", {**audit_details, "status": status}, None, client
    if fresh_config.get("lock"):
        return (
            f"Template is locked by another Proxmox operation ({fresh_config.get('lock')}).",
            audit_details,
            None,
            client,
        )
    if _config_enabled(fresh_config, "protection"):
        return "Disable protection before converting this template back to a VM.", audit_details, None, client

    try:
        snapshots = client.get(f"nodes/{quote(detail.node, safe='')}/qemu/{detail.vmid}/snapshot")
    except ProxmoxAPIError as exc:
        return public_exception_message(
            exc,
            operation="template_snapshot_preflight",
            fallback="Could not verify template snapshots against Proxmox.",
        ), audit_details, None, client
    if not isinstance(snapshots, list):
        return "Could not verify template snapshots: unexpected Proxmox response.", audit_details, None, client
    snapshot_names = [
        str(snapshot.get("name") or "")
        for snapshot in snapshots if isinstance(snapshot, dict)
        if str(snapshot.get("name") or "") not in {"", "current"}
    ]
    if snapshot_names:
        return "Remove template snapshots before converting it back to a VM.", {**audit_details, "snapshots": snapshot_names}, None, client

    disk_references = extract_disk_references(fresh_config)
    if not disk_references:
        return "This template has no supported disk volumes to validate.", audit_details, None, client
    storage_paths, storage_error = _template_storage_paths(
        disk_references,
        cluster=detail.cluster,
        node=detail.node,
    )
    if storage_error:
        return storage_error, audit_details, None, client

    children, child_error = _template_linked_clone_children(
        client, detail.node, storage_paths, cluster=detail.cluster
    )
    if child_error:
        return child_error, audit_details, None, client
    if children:
        child_labels = ", ".join(sorted({str(child.get("vmid") or "unknown") for child in children}))
        return (
            f"Cannot convert this template back to a VM because linked clone(s) still depend on it: {child_labels}.",
            {**audit_details, "linked_children": children},
            None,
            client,
        )

    audit_details["storage_ids"] = sorted(storage_paths)
    try:
        client.set_guest_config(
            node=detail.node,
            object_type=detail.object_type,
            vmid=detail.vmid,
            updates={"template": "0"},
            delete=[],
            digest=fresh_config.get("digest"),
        )
    except ProxmoxAPIError as exc:
        return public_exception_message(
            exc,
            operation="template_conversion",
            fallback="Proxmox could not convert the template back to a VM.",
        ), audit_details, None, client
    return "", audit_details, None, client




def _destroy_guest_from_bulk_request(request, detail: SimpleNamespace) -> tuple[str, dict, object | None, object | None]:
    confirm_vmid = request.POST.get("destroy_confirm_vmid", "").strip()
    if confirm_vmid != str(detail.vmid):
        return "The confirmation VMID did not match.", {}, None, None
    if detail.status == "running":
        return "Stop the guest before destroying it.", {"status": detail.status}, None, None
    # A template whose base volume still backs linked clones must not be destroyed:
    # Proxmox refuses it anyway, but fail early with a clear message.
    children = _linked_clone_children(detail)
    if children:
        labels = ", ".join(str(child) for child in children)
        return (
            "Cannot destroy this template — linked clone(s) still depend on its base "
            f"volume: {labels}. Delete the linked clones first.",
            {"linked_children": children},
            None,
            None,
        )

    purge = request.POST.get("destroy_purge") == "1"
    destroy_unreferenced_disks = request.POST.get("destroy_unreferenced_disks") == "1"
    params = {"purge": "1" if purge else "0"}
    if detail.object_type == ProxmoxInventory.ObjectType.VM:
        params["destroy-unreferenced-disks"] = "1" if destroy_unreferenced_disks else "0"
    query = urlencode(params)
    response, err, client = _guest_destroy_with_client(detail, query)
    return (
        err or "",
        {
            "purge": purge,
            "destroy_unreferenced_disks": destroy_unreferenced_disks,
            # Rescan the freed storage so the removed disks drop out of inventory.
            "rescan_storage_ids": list(_config_storage_ids(detail.config)),
        },
        response,
        client,
    )




def _update_guest_tags_from_bulk_request(request, detail: SimpleNamespace) -> tuple[str, dict]:
    mode = request.POST.get("tags_mode", "").strip()
    requested_tags = _split_tag_text(request.POST.get("tags_value", ""))
    try:
        requested_tags = [validate_tag(tag) for tag in requested_tags]
    except TagValidationError:
        return "One or more tags use an invalid name.", {"mode": mode, "tags": requested_tags}
    if mode not in {"add", "remove", "replace"}:
        return "Unknown tag operation.", {}
    if mode in {"add", "remove"} and not requested_tags:
        return "Enter at least one tag.", {"mode": mode, "tags": requested_tags}

    client = None
    fresh: dict = {}
    for candidate in common.cluster_scoped_clients(detail.cluster):
        try:
            fresh = candidate.guest_config(node=detail.node, object_type=detail.object_type, vmid=detail.vmid)
            client = candidate
            break
        except ProxmoxAPIError:
            continue
    if client is None:
        return "Could not read the current guest config from Proxmox.", {"mode": mode, "tags": requested_tags}
    if fresh.get("lock"):
        return f"Guest is locked by another Proxmox operation ({fresh.get('lock')}).", {"mode": mode, "tags": requested_tags}

    current_tags = parse_guest_tags(fresh)
    current_lookup = {tag.lower(): tag for tag in current_tags}
    if mode == "replace":
        next_tags = _unique_tags(requested_tags)
    elif mode == "add":
        next_tags = list(current_tags)
        for tag in requested_tags:
            if tag.lower() not in current_lookup:
                next_tags.append(tag)
    else:
        remove_set = {tag.lower() for tag in requested_tags}
        next_tags = [tag for tag in current_tags if tag.lower() not in remove_set]

    audit_details = {
        "mode": mode,
        "tags": requested_tags,
        "previous_tags": current_tags,
        "new_tags": next_tags,
    }
    if current_tags == next_tags:
        audit_details["noop"] = True
        return "", audit_details

    updates: dict[str, str] = {}
    delete: list[str] = []
    if next_tags:
        updates["tags"] = ";".join(next_tags)
    else:
        delete.append("tags")

    try:
        client.set_guest_config(
            node=detail.node,
            object_type=detail.object_type,
            vmid=detail.vmid,
            updates=updates,
            delete=delete,
            digest=fresh.get("digest"),
        )
    except ProxmoxAPIError as exc:
        return public_exception_message(
            exc,
            operation="guest_tag_update",
            fallback="Proxmox could not update the guest tags.",
        ), audit_details

    _update_current_guest_config(detail, updates, delete)
    return "", audit_details




def _set_guest_agent_from_bulk_request(
    detail: SimpleNamespace,
    *,
    enabled: bool,
) -> tuple[str, dict, object | None, object | None]:
    audit_details = {"agent": "enabled" if enabled else "disabled"}
    if detail.object_type != ProxmoxInventory.ObjectType.VM:
        return "Guest agent applies to VMs only.", audit_details, None, None

    client = None
    fresh: dict = {}
    for candidate in common.cluster_scoped_clients(detail.cluster):
        try:
            fresh = candidate.guest_config(node=detail.node, object_type=detail.object_type, vmid=detail.vmid)
            client = candidate
            break
        except ProxmoxAPIError:
            continue
    if client is None:
        return "Could not read the current guest config from Proxmox.", audit_details, None, None
    if fresh.get("lock"):
        return f"Guest is locked by another Proxmox operation ({fresh.get('lock')}).", audit_details, None, None

    currently_enabled = _guest_agent_config_enabled(fresh, detail.object_type)
    audit_details["previous_agent"] = "enabled" if currently_enabled else "disabled"
    if currently_enabled == enabled:
        audit_details["noop"] = True
        return "", audit_details, None, client

    updates = {"agent": "1" if enabled else "0"}
    try:
        client.set_guest_config(
            node=detail.node,
            object_type=detail.object_type,
            vmid=detail.vmid,
            updates=updates,
            delete=[],
            digest=fresh.get("digest"),
        )
    except ProxmoxAPIError as exc:
        return public_exception_message(
            exc,
            operation="guest_agent_update",
            fallback="Proxmox could not update the guest agent setting.",
        ), audit_details, None, client

    _update_current_guest_config(detail, updates, [])
    return "", audit_details, None, client

"""Guest read tabs + dialog option endpoints (snapshots/backup + *_options) — from _core."""
from __future__ import annotations
from ..common import *  # noqa: F401,F403
from .. import common
from ._core import (_backup_job_covers,_guest_backup_archives,_guest_backup_storages,_guest_cpu_model,_guest_movable_disks,_guest_nic_bridges,_guest_snapshot_entries,_migrate_not_allowed_reason,_node_available_bridges,_node_cpu_models,_node_cpu_signature,_ordered_snapshot_entries)
from .operation_lifecycle import _guest_kind
from .read_model_support import (_config_disk_bytes,_config_storage_ids,_guest_pool_memberships,_guest_tab_context,_require_guest)
from core.services.storage_catalog import node_storage_rows


@app_login_required
def guest_snapshots(request, cluster_key: str, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid, cluster_key=cluster_key)
    entries, error = _guest_snapshot_entries(detail)
    ordered = _ordered_snapshot_entries(entries)
    context = _guest_tab_context(detail, "snapshots")
    context.update(
        {
            "snapshot_tree": ordered,
            "snapshot_count": sum(1 for item in entries if not item["is_current"]),
            "snapshot_error": error,
            "snapshot_rendered_at_ms": int(tz.now().timestamp() * 1000),
        }
    )
    if request.GET.get("snapshot_partial") == "1":
        return JsonResponse(
            {
                "html": render_to_string("core/partials/guest_snapshot_panel.html", context, request=request),
                "rendered_at_ms": context["snapshot_rendered_at_ms"],
            }
        )
    return render(request, "core/guest_snapshots.html", context)




@app_login_required
def guest_backup(request, cluster_key: str, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid, cluster_key=cluster_key)
    backups, backup_storages, error = _guest_backup_archives(detail)

    jobs = []
    clients = common.cluster_scoped_clients(detail.cluster)
    try:
        raw_jobs = clients[0].get("cluster/backup") if clients else []
        for job in raw_jobs if isinstance(raw_jobs, list) else []:
            if _backup_job_covers(job, vmid):
                jobs.append(
                    {
                        "id": job.get("id", ""),
                        "schedule": job.get("schedule", ""),
                        "storage": job.get("storage", ""),
                        "enabled": str(job.get("enabled", "1")) in ("1", "True", "true"),
                        "selection": job.get("all") and "all guests" or job.get("vmid") or job.get("pool") or "-",
                    }
                )
    except ProxmoxAPIError:
        pass

    context = _guest_tab_context(detail, "backup")
    context.update({"backups": backups, "backup_jobs": jobs, "backup_error": error, "backup_storages": backup_storages})
    if request.GET.get("backup_partial") == "1":
        return JsonResponse(
            {
                "html": render_to_string("core/partials/guest_backup_panel.html", context, request=request),
                "rendered_at_ms": int(tz.now().timestamp() * 1000),
            }
        )
    return render(request, "core/guest_backup.html", context)




@app_login_required
def guest_backup_options(request, cluster_key: str, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid, cluster_key=cluster_key)
    storages, error = _guest_backup_storages(detail)
    return JsonResponse(
        {
            "storages": storages,
            "error": error,
            "guest": {"type": detail.object_type, "vmid": detail.vmid, "node": detail.node},
        }
    )




@app_login_required
def guest_migrate_options(request, cluster_key: str, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid, cluster_key=cluster_key)
    is_vm = detail.object_type == ProxmoxInventory.ObjectType.VM
    content = "images" if is_vm else "rootdir"
    active = str(detail.status or "").strip() in _MIGRATE_ACTIVE_STATES

    nodes: list[dict] = []
    storages_by_node: dict[str, list[str]] = {}
    storage_free_by_node: dict[str, dict[str, int]] = {}
    bridges_by_node: dict[str, list[str]] = {}
    sdn_vnet_names: list[str] = []
    local_resources: list[str] = []
    for client in common.cluster_scoped_clients(detail.cluster):
        try:
            raw_nodes = client.get("nodes")
        except ProxmoxAPIError:
            continue
        if not isinstance(raw_nodes, list):
            continue
        try:
            sdn_vnets = {
                str(vnet.get("vnet"))
                for vnet in client.get("cluster/sdn/vnets")
                if isinstance(vnet, dict) and vnet.get("vnet")
            }
        except ProxmoxAPIError:
            sdn_vnets = set()
        # SDN vnets are cluster-scoped, so any of them can be assigned to a NIC on
        # any node (the per-node realized set below still drives the warning).
        sdn_vnet_names = sorted(sdn_vnets)
        # Proxmox migration preconditions give the real allowed/blocked target
        # set + reasons (missing storage/bridge, passthrough, ...). Defensive:
        # if the endpoint can't answer, fall back to "all online nodes allowed".
        allowed = None
        not_allowed: dict = {}
        try:
            pre = client.get(f"nodes/{quote(detail.node, safe='')}/{_guest_kind(detail)}/{detail.vmid}/migrate")
        except ProxmoxAPIError:
            pre = None
        if isinstance(pre, dict):
            if isinstance(pre.get("allowed_nodes"), list):
                allowed = [str(item) for item in pre["allowed_nodes"]]
            if isinstance(pre.get("not_allowed_nodes"), dict):
                not_allowed = pre["not_allowed_nodes"]
            if isinstance(pre.get("local_resources"), list):
                local_resources = [str(item) for item in pre["local_resources"]]

        cpu_model = _guest_cpu_model(detail)
        # For cpu=host, live migration is only safe between hosts exposing an
        # identical CPU; capture the source signature to compare each target.
        source_cpu_sig = _node_cpu_signature(client, detail.node) if cpu_model == "host" else None
        node_names: list[str] = []
        for node in raw_nodes:
            if not isinstance(node, dict) or not node.get("node"):
                continue
            name = str(node["node"])
            node_names.append(name)
            if name == detail.node:
                continue
            online = str(node.get("status") or "") == "online"
            entry = {
                "node": name,
                "online": online,
                "allowed": True,
                "reason": "",
                "cpu_ok": True,
                "cpu_reason": "",
                "host_cpu_match": True,
                "host_cpu_reason": "",
            }
            if not online:
                entry["allowed"] = False
                entry["reason"] = "node offline"
            elif allowed is not None and name not in allowed:
                entry["allowed"] = False
                entry["reason"] = _migrate_not_allowed_reason(not_allowed.get(name))
            # EVC-lite: a named CPU model must be runnable on the target. Proxmox's
            # own precondition does not check this. Default (unset) is portable.
            if online and cpu_model and cpu_model != "host" and cpu_model not in _node_cpu_models(client, name):
                entry["cpu_ok"] = False
                entry["cpu_reason"] = f"CPU model '{cpu_model}' is not available on {name}"
            # cpu=host: only safe to live-migrate to an identical CPU. Compare on
            # the CPU model so two identical hosts stay silent (a trivial flag/
            # microcode delta shouldn't nag), while a real mismatch (e.g. Intel →
            # AMD) is flagged.
            if online and source_cpu_sig is not None:
                target_sig = _node_cpu_signature(client, name)
                if target_sig is None or target_sig[0] != source_cpu_sig[0]:
                    entry["host_cpu_match"] = False
                    src_model = source_cpu_sig[0] or "source CPU"
                    tgt_model = (target_sig[0] if target_sig else "") or "target CPU"
                    entry["host_cpu_reason"] = f"host CPUs differ ({src_model} → {tgt_model})"
            nodes.append(entry)

        for name in node_names:
            raw_storages = node_storage_rows(detail.cluster, name, content=content)
            if not isinstance(raw_storages, list):
                continue
            ids: list[str] = []
            free: dict[str, int] = {}
            for storage in raw_storages:
                if not isinstance(storage, dict) or not storage.get("storage"):
                    continue
                contents = {item.strip() for item in str(storage.get("content", "")).split(",")}
                if content in contents and str(storage.get("active", "1")) != "0":
                    storage_id = str(storage["storage"])
                    ids.append(storage_id)
                    try:
                        free[storage_id] = int(storage.get("avail"))
                    except (TypeError, ValueError):
                        pass
            storages_by_node[name] = sorted(set(ids))
            storage_free_by_node[name] = free
            bridges_by_node[name] = _node_available_bridges(client, name, sdn_vnets)
        break

    return JsonResponse(
        {
            "object_type": object_type,
            "current_node": detail.node,
            "running": active,
            "nodes": nodes,
            "disks": _guest_movable_disks(detail),
            "guest_nics": _guest_nic_bridges(detail),
            "guest_cpu": _guest_cpu_model(detail),
            "guest_disk_bytes": _config_disk_bytes(detail.config),
            "storages_by_node": storages_by_node,
            "storage_free_by_node": storage_free_by_node,
            "bridges_by_node": bridges_by_node,
            "sdn_vnets": sdn_vnet_names,
            "local_resources": local_resources,
        }
    )




@app_login_required
def guest_clone_options(request, cluster_key: str, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid, cluster_key=cluster_key)
    nextid = ""
    storages: list[str] = []
    content = "rootdir" if object_type == ProxmoxInventory.ObjectType.CT else "images"
    for client in common.cluster_scoped_clients(detail.cluster):
        try:
            nextid = str(client.get("cluster/nextid") or "")
        except ProxmoxAPIError:
            nextid = ""
        raw_storages = node_storage_rows(detail.cluster, detail.node, content=content)
        if isinstance(raw_storages, list):
            for storage in raw_storages:
                if not isinstance(storage, dict) or not storage.get("storage"):
                    continue
                contents = {item.strip() for item in str(storage.get("content", "")).split(",")}
                if content in contents:
                    storages.append(str(storage["storage"]))
        if nextid or storages:
            break

    source_storages = _config_storage_ids(detail.config)
    default_storage = next((storage for storage in source_storages if storage in storages), "")
    if not default_storage and storages:
        default_storage = storages[0]

    used_vmids = sorted(
        {
            guest.vmid
            for guest in common.fetch_live_guest_inventory(cluster=detail.cluster)
            if guest.vmid is not None
        }
    )

    return JsonResponse(
        {
            "nextid": nextid,
            "used_vmids": used_vmids,
            "storages": [{"id": storage, "label": storage} for storage in storages],
            "default_storage": default_storage,
            "source_storages": source_storages,
            "suggested_name": f"{detail.name}-clone" if detail.name else "",
            # Linked clones are only supported from a template; a regular guest
            # must be full-cloned (Proxmox rejects linked otherwise).
            "is_template": is_template(detail.config),
        }
    )




@app_login_required
def guest_pool_options(request, cluster_key: str, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid, cluster_key=cluster_key)
    for client in common.cluster_scoped_clients(detail.cluster):
        try:
            client.guest_current(node=detail.node, object_type=detail.object_type, vmid=detail.vmid)
            pools, memberships = _guest_pool_memberships(client, detail)
            return JsonResponse(
                {
                    "pools": [{"id": pool_id, "label": pool_id} for pool_id in pools],
                    "current_pool": memberships[0] if len(memberships) == 1 else "",
                    "multiple_memberships": memberships if len(memberships) > 1 else [],
                }
            )
        except ProxmoxAPIError:
            continue
    return JsonResponse({"error": "Could not load pools from the guest's Proxmox endpoint."}, status=502)

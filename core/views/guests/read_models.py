"""Guest read models: overview, status, enrichment APIs, summary (from _core)."""
from __future__ import annotations
from ..common import *  # noqa: F401,F403
from .. import common
from .read_model_support import (_display_lock,_guest_agent_summary,_guest_cpu_label,_guest_cpu_topology,_guest_ha_summary,_guest_health,_guest_lineage,_guest_os_label,_guest_pool_label,_guest_rows,_guest_state_label,_guest_tab_context,_guest_target_value,_guest_usage,_guest_vm_details,_live_guest_has_snapshot,_resolve_guest_detail,_vms_workspace_context)


@app_login_required
def vms_list(request):
    """Central, cluster-wide VMs/CTs workspace (left list, no selection)."""
    context = _vms_workspace_context("vms")
    return render(request, "core/vms.html", context)




@app_login_required
def vms_overview(request):
    """vSphere-style, sortable overview table for all VMs and CTs."""
    context = _vms_workspace_context("vms_overview")
    return render(request, "core/vms_overview.html", context)




@app_login_required
def vms_overview_agent_info(request):
    rows, _live_available, _scan_at = _guest_rows()
    deadline = monotonic() + OVERVIEW_ENRICH_BUDGET_SECONDS
    payload = []
    for row in rows:
        if row.object_type != ProxmoxInventory.ObjectType.VM or not row.agent_enabled:
            continue
        detail = SimpleNamespace(
            cluster=row.cluster,
            cluster_key=row.cluster_key,
            object_type=row.object_type,
            vmid=row.vmid,
            name=row.name,
            node=row.node,
            status=row.status,
            config={"agent": 1},
        )
        # Cached summaries stay cheap; once the per-request budget is spent we
        # stop issuing new agent calls and serve only what is already cached.
        summary = _guest_agent_summary(detail, allow_fetch=monotonic() < deadline)
        if not summary.get("running"):
            continue
        payload.append(
            {
                "target": row.target_id,
                "guest_ref": row.guest_ref_id,
                "guest_os": summary.get("os_pretty_name") or summary.get("os_name") or "",
                "ip_label": ", ".join(summary.get("ips", [])[:3]) if summary.get("ips") else "",
                "agent": "Running",
            }
        )
    return JsonResponse({"guests": payload})




@app_login_required
def vms_overview_snapshot_info(request):
    rows, _live_available, _scan_at = _guest_rows()
    deadline = monotonic() + OVERVIEW_ENRICH_BUDGET_SECONDS
    payload = []
    for row in rows:
        has_snapshot = _live_guest_has_snapshot(row, allow_fetch=monotonic() < deadline)
        # None = probe unavailable/budget spent; keep it unknown ("-") rather
        # than reporting a misleading "No".
        payload.append(
            {
                "target": row.target_id,
                "guest_ref": row.guest_ref_id,
                "has_snapshot": bool(has_snapshot),
                "has_snapshot_label": "-" if has_snapshot is None else ("Yes" if has_snapshot else "No"),
            }
        )
    return JsonResponse({"guests": payload})




@app_login_required
def vms_status(request):
    current = list(CurrentGuestInventory.objects.all())
    refreshed_at = max(
        (guest.runtime_observed_at for guest in current if guest.runtime_observed_at),
        default=None,
    )
    guests = [
        {
            "target": _guest_target_value(guest.object_type, guest.vmid, guest.node),
            "guest_ref": guest.guest_ref().serialize() if guest.guest_ref() else "",
            "status": guest.status,
            "state_label": _guest_state_label(guest.status),
            "lock": _display_lock(guest.runtime_lock),
        }
        for guest in sorted(current, key=lambda item: (item.object_type, item.vmid, item.node))
    ]
    return JsonResponse(
        {
            "guests": guests,
            "live_available": any(guest.runtime_observed_at for guest in current),
            "refreshed_at": refreshed_at.isoformat() if refreshed_at else None,
            "cache_seconds": 0,
        }
    )




@app_login_required
def guest_summary(request, object_type: str, vmid: int):
    if object_type not in GUEST_OBJECT_TYPES:
        raise Http404("Unknown guest type")
    detail = _resolve_guest_detail(object_type, vmid)
    if not detail.found:
        raise Http404("Guest not found")

    config = detail.config
    current = detail.current
    disks, cdroms = guest_disks(config, detail.node, detail.vmid)
    nets = guest_networks(config)

    related_storages = []
    seen_storage = set()
    for disk in disks:
        if disk["storage_id"] and disk["storage_id"] not in seen_storage:
            seen_storage.add(disk["storage_id"])
            related_storages.append({"storage_id": disk["storage_id"], "url": disk["url"], "mounted": disk["mounted"]})

    related_networks = []
    seen_net = set()
    for net in nets:
        key = (net["bridge"], net["vlan"])
        if net["bridge"] and key not in seen_net:
            seen_net.add(key)
            related_networks.append({"bridge": net["bridge"], "vlan": net["vlan"]})

    context = _guest_tab_context(detail, "summary")
    guest_pool = _guest_pool_label(detail)
    guest_ha = _guest_ha_summary(detail)
    context.update(
        {
            "guest_health": _guest_health(detail),
            "guest_lineage": _guest_lineage(detail),
            "guest_os_label": _guest_os_label(config),
            "guest_agent_summary": _guest_agent_summary(detail, allow_fetch=False),
            "guest_usage": _guest_usage(current, config, detail.object_type),
            "guest_cpu_topology": _guest_cpu_topology(config, detail.object_type),
            "related_storages": related_storages,
            "related_networks": related_networks,
            "vm_details": _guest_vm_details(detail, guest_pool),
            "guest_ha": guest_ha,
            "guest_cpu_label": _guest_cpu_label(config, detail.object_type),
            "guest_memory_label": f"{config.get('memory')} MB" if config.get("memory") else "",
            "guest_disks": disks,
            "guest_cdroms": cdroms,
            "guest_nets": nets,
            "guest_notes": config.get("description") or "",
            "guest_current": current,
            "guest_config": config,
            # A hibernated (suspend-to-disk) VM is 'stopped' but carries
            # lock=suspended + a saved vmstate; Power On resumes it. The live
            # inventory may already surface it as 'hibernated'.
            "guest_is_hibernated": detail.status == "hibernated"
            or (
                detail.status == "stopped"
                and ((current or {}).get("lock") or config.get("lock")) == "suspended"
            ),
        }
    )
    return render(request, "core/guest_summary.html", context)

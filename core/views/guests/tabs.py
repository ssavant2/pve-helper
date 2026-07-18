"""Guest read-only tabs: monitor, permissions, cloud-init (+edit) — extracted from _core."""
from ..common import *  # noqa: F401,F403
from .. import common
from .operation_lifecycle import _write_result
from .presenters import _fmt_bytes, _rrd_chart
from .read_model_support import (_guest_api_get,_guest_tab_context,_require_guest)
from core.services.current_guest_inventory import refresh_current_guest_from_client, update_current_guest_config


@app_login_required
def guest_monitor(request, cluster_key: str, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid, cluster_key=cluster_key)
    timeframe = request.GET.get("timeframe", "hour")
    if timeframe not in {"hour", "day", "week", "month", "year"}:
        timeframe = "hour"
    data, error = _guest_api_get(detail, f"rrddata?timeframe={quote(timeframe)}")
    points = data if isinstance(data, list) else []
    last = points[-1] if points else {}
    maxmem = max((int(p.get("maxmem") or 0) for p in points), default=0) or 1
    cpu_last = float(last.get("cpu") or 0) * 100
    mem_last_bytes = int(last.get("mem") or 0)

    charts = []
    if points:
        charts = [
            {
                "title": "CPU",
                "current": f"{cpu_last:.1f}%",
                "legend": [],
                "chart": _rrd_chart(points, ["cpu"], to_value=lambda v: float(v or 0) * 100, fmt="pct", axis_max=100),
            },
            {
                "title": "Memory",
                "current": f"{_fmt_bytes(mem_last_bytes)} / {_fmt_bytes(maxmem)}",
                "legend": [],
                "chart": _rrd_chart(points, ["mem"], to_value=lambda v: float(v or 0), fmt="bytes", axis_max=maxmem),
            },
            {
                "title": "Network",
                "current": "",
                "legend": [{"label": "In", "cls": "s1"}, {"label": "Out", "cls": "s2"}],
                "chart": _rrd_chart(points, ["netin", "netout"], to_value=lambda v: float(v or 0), fmt="rate"),
            },
            {
                "title": "Disk IO",
                "current": "",
                "legend": [{"label": "Read", "cls": "s1"}, {"label": "Write", "cls": "s2"}],
                "chart": _rrd_chart(points, ["diskread", "diskwrite"], to_value=lambda v: float(v or 0), fmt="rate"),
            },
        ]

    context = _guest_tab_context(detail, "monitor")
    context.update(
        {
            "timeframe": timeframe,
            "timeframes": ["hour", "day", "week", "month", "year"],
            "monitor_error": error,
            "has_rrd": bool(points),
            "charts": charts,
        }
    )
    return render(request, "core/guest_monitor.html", context)




@app_login_required
def guest_permissions(request, cluster_key: str, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid, cluster_key=cluster_key)
    acl = None
    error = ""
    for client in common.cluster_scoped_clients(detail.cluster):
        try:
            acl = client.get("access/acl")
            error = ""
            break
        except ProxmoxAPIError as exc:
            error = str(exc)
    guest_path = f"/vms/{vmid}"
    entries = []
    if isinstance(acl, list):
        for entry in acl:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path", ""))
            propagate = str(entry.get("propagate", "1")) in ("1", "True", "true")
            applies = path == guest_path or (path in ("/", "/vms") and propagate)
            if not applies:
                continue
            entries.append(
                {
                    "path": path,
                    "type": entry.get("type", ""),
                    "ugid": entry.get("ugid", ""),
                    "roleid": entry.get("roleid", ""),
                    "propagate": propagate,
                    "inherited": path != guest_path,
                }
            )
    entries.sort(key=lambda item: (not item["inherited"], item["ugid"]))
    context = _guest_tab_context(detail, "permissions")
    context.update({"acl_entries": entries, "permissions_error": error, "guest_path": guest_path})
    return render(request, "core/guest_permissions.html", context)




@app_login_required
def guest_cloudinit(request, cluster_key: str, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid, cluster_key=cluster_key)
    config = detail.config
    has_ci = any(str(k).startswith("ci") or str(k).startswith("ipconfig") for k in config) or any(
        "cloudinit" in str(v) for v in config.values()
    )
    rows = []
    for key in ("ciuser", "citype", "ciupgrade", "nameserver", "searchdomain"):
        if config.get(key):
            rows.append({"label": key, "value": config[key]})
    ipconfigs = [{"label": k, "value": config[k]} for k in sorted(config) if str(k).startswith("ipconfig")]
    has_password = bool(config.get("cipassword"))
    has_sshkeys = bool(config.get("sshkeys"))
    context = _guest_tab_context(detail, "cloudinit")
    context.update(
        {
            "has_cloudinit": has_ci,
            "ci_rows": rows,
            "ipconfigs": ipconfigs,
            "has_ci_password": has_password,
            "has_ci_sshkeys": has_sshkeys,
            "ci_values": {
                "ciuser": config.get("ciuser", ""),
                "nameserver": config.get("nameserver", ""),
                "searchdomain": config.get("searchdomain", ""),
                "ipconfig0": config.get("ipconfig0", ""),
            },
        }
    )
    return render(request, "core/guest_cloudinit.html", context)




@require_POST
@app_login_required
def guest_cloudinit_edit(request, cluster_key, object_type, vmid):
    detail = _require_guest(object_type, vmid, cluster_key=cluster_key)
    updates, delete = {}, []
    for field in ("ciuser", "nameserver", "searchdomain", "ipconfig0"):
        val = request.POST.get(field, "").strip()
        if val:
            updates[field] = val
        else:
            delete.append(field)
    password = request.POST.get("cipassword", "")
    if password:
        updates["cipassword"] = password
    sshkeys = request.POST.get("sshkeys", "").strip()
    if sshkeys:
        updates["sshkeys"] = quote(sshkeys, safe="")
    node = detail.node
    err = ""
    try:
        client = common.cluster_scoped_clients(detail.cluster)[0]
        fresh = client.guest_config(node=node, object_type=object_type, vmid=vmid)
        # only delete keys that currently exist
        delete = [k for k in delete if k in fresh]
        client.set_guest_config(node=node, object_type=object_type, vmid=vmid, updates=updates, delete=delete, digest=fresh.get("digest"))
    except (ProxmoxAPIError, IndexError) as exc:
        err = str(exc)
    if not err:
        update_current_guest_config(
            object_type=object_type,
            vmid=vmid,
            node=node,
            updates={key: value for key, value in updates.items() if key != "cipassword"},
            delete=delete,
            cluster=detail.cluster,
        )
        refresh_current_guest_from_client(
            client,
            node=node,
            object_type=object_type,
            vmid=vmid,
            cluster=detail.cluster,
        )
    return _write_result(request, detail, "core:guest_cloudinit", err, "guest.cloudinit.update")

"""Guest read-only tabs: monitor, permissions, cloud-init (+edit) — extracted from _core."""
from ..common import *  # noqa: F401,F403
from .. import common
from ._core import (_fmt_bytes,_guest_api_get,_guest_tab_context,_require_guest,_rrd_chart,_write_result)


@app_login_required
def guest_monitor(request, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid)
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
def guest_permissions(request, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid)
    acl = None
    error = ""
    for client in common.configured_clients():
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
def guest_cloudinit(request, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid)
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
def guest_cloudinit_edit(request, object_type, vmid):
    detail = _require_guest(object_type, vmid)
    if not settings.VM_WRITE_ENABLED:
        messages.error(request, "Editing is disabled.")
        return redirect("core:guest_cloudinit", object_type=object_type, vmid=vmid)
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
        client = common.configured_clients()[0]
        fresh = client.guest_config(node=node, object_type=object_type, vmid=vmid)
        # only delete keys that currently exist
        delete = [k for k in delete if k in fresh]
        client.set_guest_config(node=node, object_type=object_type, vmid=vmid, updates=updates, delete=delete, digest=fresh.get("digest"))
    except (ProxmoxAPIError, IndexError) as exc:
        err = str(exc)
    return _write_result(request, detail, "core:guest_cloudinit", err, "guest.cloudinit.update")





"""Guest read-only tabs: datastores, networks, agent — extracted from _core."""
from ..common import *  # noqa: F401,F403
from .. import common
from .presenters import _config_ip_addresses, _with_network_ip_addresses
from .read_model_support import (_guest_agent_summary,_guest_api_get,_guest_tab_context,_require_guest)


@app_login_required
def guest_datastores(request, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid)
    disks, _cdroms = guest_disks(detail.config, detail.node, detail.vmid)
    mounts = {m.storage_id: m for m in StorageMount.objects.all()}
    by_storage: dict[str, dict] = {}
    for disk in disks:
        entry = by_storage.setdefault(
            disk["storage_id"],
            {
                "storage_id": disk["storage_id"],
                "mounted": disk["mounted"],
                "url": disk["url"],
                "display_name": mounts[disk["storage_id"]].display_name if disk["storage_id"] in mounts else disk["storage_id"],
                "disks": [],
            },
        )
        entry["disks"].append(disk)
    context = _guest_tab_context(detail, "datastores")
    context["datastores"] = list(by_storage.values())
    return render(request, "core/guest_datastores.html", context)




@app_login_required
def guest_networks_view(request, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid)
    agent_summary = _guest_agent_summary(detail, allow_fetch=True)
    context = _guest_tab_context(detail, "networks")
    context["nets"] = _with_network_ip_addresses(guest_networks(detail.config), _config_ip_addresses(detail.config), agent_summary)
    context["agent_ips"] = agent_summary.get("ips", [])
    return render(request, "core/guest_networks.html", context)




@app_login_required
def guest_agent_view(request, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid)
    agent_enabled = bool(detail.config.get("agent"))
    osinfo = None
    interfaces = []
    hostname = ""
    filesystems = []
    agent_error = ""
    if agent_enabled and detail.object_type == ProxmoxInventory.ObjectType.VM:
        host_data, _host_err = _guest_api_get(detail, "agent/get-host-name")
        if isinstance(host_data, dict):
            result = host_data.get("result") if isinstance(host_data.get("result"), dict) else host_data
            hostname = result.get("host-name", "") if isinstance(result, dict) else ""
        fs_data, _fs_err = _guest_api_get(detail, "agent/get-fsinfo")
        if isinstance(fs_data, dict):
            for entry in fs_data.get("result") or []:
                if not isinstance(entry, dict):
                    continue
                filesystems.append(
                    {
                        "name": entry.get("name", ""),
                        "mountpoint": entry.get("mountpoint", ""),
                        "type": entry.get("type", ""),
                        "used": entry.get("used-bytes"),
                        "total": entry.get("total-bytes"),
                    }
                )
        os_data, os_err = _guest_api_get(detail, "agent/get-osinfo")
        if isinstance(os_data, dict):
            result = os_data.get("result") if isinstance(os_data.get("result"), dict) else os_data
            osinfo = [
                {"label": "Name", "value": result.get("pretty-name") or result.get("name")},
                {"label": "Version", "value": result.get("version")},
                {"label": "Kernel", "value": result.get("kernel-release")},
                {"label": "Arch", "value": result.get("machine")},
            ]
            osinfo = [row for row in osinfo if row["value"]]
        net_data, net_err = _guest_api_get(detail, "agent/network-get-interfaces")
        if isinstance(net_data, dict):
            result = net_data.get("result")
            if isinstance(result, list):
                for iface in result:
                    if not isinstance(iface, dict):
                        continue
                    addrs = [
                        a.get("ip-address")
                        for a in iface.get("ip-addresses", []) or []
                        if isinstance(a, dict) and a.get("ip-address")
                    ]
                    interfaces.append(
                        {
                            "name": iface.get("name", ""),
                            "mac": iface.get("hardware-address", ""),
                            "addresses": addrs,
                        }
                    )
        agent_error = os_err or net_err or ""
    context = _guest_tab_context(detail, "guest_agent")
    context.update(
        {
            "agent_enabled": agent_enabled,
            "agent_osinfo": osinfo,
            "agent_hostname": hostname,
            "agent_interfaces": interfaces,
            "agent_filesystems": filesystems,
            "agent_error": agent_error,
        }
    )
    return render(request, "core/guest_agent.html", context)



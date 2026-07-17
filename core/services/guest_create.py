from __future__ import annotations

from typing import Any
from urllib.parse import quote

from core.services.proxmox import ProxmoxAPIError


VM_OSTYPES = [
    ("l26", "Linux (modern)"),
    ("l24", "Linux 2.4"),
    ("win11", "Windows 11/2022/2025"),
    ("win10", "Windows 10/2016"),
    ("win8", "Windows 8/2012"),
    ("w2k8", "Windows 2008"),
    ("wxp", "Windows XP"),
    ("solaris", "Solaris"),
    ("other", "Other"),
]


def _creation_cluster():
    """The cluster a guest is created in.

    Creation has no existing guest to infer scope from, so it resolves the sole
    enabled cluster explicitly rather than taking whichever endpoint happens to be
    first in an ambient list.
    """
    from core.services.cluster_resolver import require_sole_enabled_cluster_for_legacy_caller

    try:
        return require_sole_enabled_cluster_for_legacy_caller()
    except Exception:
        return None


def _first_client():
    from core.services.cluster_resolver import pin_cluster_write_client

    cluster = _creation_cluster()
    if cluster is None:
        return None
    try:
        _endpoint, client = pin_cluster_write_client(cluster)
    except Exception:
        return None
    return client


def _storages(client, node: str) -> list[dict]:
    try:
        data = client.get(f"nodes/{quote(node, safe='')}/storage")
    except ProxmoxAPIError:
        return []
    return data if isinstance(data, list) else []


def _storages_for(storages: list[dict], content: str) -> list[str]:
    out = []
    for storage in storages:
        contents = str(storage.get("content", "")).split(",")
        if content in contents and storage.get("storage"):
            out.append(storage["storage"])
    return out


def _content_volids(client, node: str, storages: list[str], content: str) -> list[str]:
    volids = []
    for storage in storages:
        try:
            data = client.get(f"nodes/{quote(node, safe='')}/storage/{quote(storage, safe='')}/content?content={content}")
        except ProxmoxAPIError:
            continue
        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict) and entry.get("volid"):
                    volids.append(entry["volid"])
    return sorted(volids)


def _bridges(client, node: str) -> list[str]:
    bridges: list[str] = []
    try:
        net = client.get(f"nodes/{quote(node, safe='')}/network")
        if isinstance(net, list):
            for entry in net:
                if isinstance(entry, dict) and entry.get("type") in ("bridge", "OVSBridge") and entry.get("iface"):
                    bridges.append(entry["iface"])
    except ProxmoxAPIError:
        pass
    try:
        vnets = client.get("cluster/sdn/vnets")
        if isinstance(vnets, list):
            for entry in vnets:
                if isinstance(entry, dict) and entry.get("vnet"):
                    bridges.append(entry["vnet"])
    except ProxmoxAPIError:
        pass
    # de-duplicate, keep order
    seen = set()
    result = []
    for bridge in bridges:
        if bridge not in seen:
            seen.add(bridge)
            result.append(bridge)
    return result


def create_options(object_type: str, node: str | None = None) -> dict[str, Any]:
    client = _first_client()
    if client is None:
        return {"available": False, "nodes": [], "node": ""}
    nodes = client.node_names(fallback="")
    node = node if node in nodes else (nodes[0] if nodes else "")
    if not node:
        return {"available": False, "nodes": nodes, "node": ""}

    try:
        nextid = str(client.get("cluster/nextid"))
    except ProxmoxAPIError:
        nextid = ""

    storages = _storages(client, node)
    is_vm = object_type == "vm"
    disk_storages = _storages_for(storages, "images" if is_vm else "rootdir")
    options = {
        "available": True,
        "nodes": nodes,
        "node": node,
        "nextid": nextid,
        "disk_storages": disk_storages,
        "bridges": _bridges(client, node),
    }
    if is_vm:
        options["ostypes"] = VM_OSTYPES
        options["isos"] = _content_volids(client, node, _storages_for(storages, "iso"), "iso")
    else:
        options["templates"] = _content_volids(client, node, _storages_for(storages, "vztmpl"), "vztmpl")
    return options


def _post_create(node: str, kind: str, body: dict):
    """Create a guest inside the selected cluster, without replaying the create.

    The previous fan-out retried the create on the next endpoint after any error.
    A create whose response is lost may well have succeeded, so replaying it risks
    a second guest or a confusing VMID conflict; only a request proven never to
    have left may be sent elsewhere.
    """
    from core.services.cluster_resolver import cluster_write

    cluster = _creation_cluster()
    if cluster is None:
        return None, "No Proxmox cluster is configured."
    try:
        result = cluster_write(
            cluster,
            operation="guest_create",
            call=lambda client: client.post(f"nodes/{quote(node, safe='')}/{kind}", data=body),
            error_message=str,
        )
    except Exception as exc:
        return None, str(exc)
    if not result.ok:
        return None, result.error
    return result.value, None


def create_vm(node: str, params: dict):
    body = {
        "vmid": params["vmid"],
        "name": params["name"],
        "ostype": params["ostype"],
        "cores": params["cores"],
        "sockets": params["sockets"],
        "memory": params["memory"],
        "scsihw": "virtio-scsi-single",
        "scsi0": f"{params['disk_storage']}:{params['disk_size']}",
        "boot": "order=scsi0;ide2",
    }
    if params.get("bridge"):
        net = f"virtio,bridge={params['bridge']}"
        if params.get("vlan"):
            net += f",tag={params['vlan']}"
        body["net0"] = net
    if params.get("iso"):
        body["ide2"] = f"{params['iso']},media=cdrom"
    else:
        body["ide2"] = "none,media=cdrom"
    if params.get("start"):
        body["start"] = 1
    return _post_create(node, "qemu", body)


def create_ct(node: str, params: dict):
    body = {
        "vmid": params["vmid"],
        "hostname": params["hostname"],
        "ostemplate": params["ostemplate"],
        "storage": params["disk_storage"],
        "rootfs": f"{params['disk_storage']}:{params['disk_size']}",
        "cores": params["cores"],
        "memory": params["memory"],
        "swap": params.get("swap") or 0,
    }
    if params.get("password"):
        body["password"] = params["password"]
    if params.get("ssh_keys"):
        body["ssh-public-keys"] = params["ssh_keys"]
    if params.get("bridge"):
        net = f"name=eth0,bridge={params['bridge']},ip={params.get('ip') or 'dhcp'}"
        if params.get("vlan"):
            net += f",tag={params['vlan']}"
        body["net0"] = net
    if params.get("start"):
        body["start"] = 1
    return _post_create(node, "lxc", body)

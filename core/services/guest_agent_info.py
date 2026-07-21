"""Guest-agent enrichment (OS name, hostname, IPs) shared by the read model and
the periodic worker.

The QEMU guest agent answers three cheap read-only calls — ``get-osinfo``,
``get-host-name`` and ``network-get-interfaces``. Both the single-guest Summary
card (fetched live, on demand) and the cluster-wide worker enrichment that warms
the projection need the exact same parse, so it lives here once instead of being
duplicated per caller and drifting.

Fetching fails soft: an agent that is not installed, not running, or briefly
unreachable yields an empty, non-running summary rather than an error.
"""

from __future__ import annotations

from urllib.parse import quote

from core.services.cluster_resolver import ClusterResolutionError, cluster_clients
from core.services.proxmox import ProxmoxAPIError

# The agent is a best-effort enrichment; keep the per-call ceiling low so a slow
# or wedged agent never stalls the worker sweep or a passive render.
GUEST_AGENT_INFO_TIMEOUT_SECONDS = 2

# Loopback addresses the guest reports about itself carry no useful information
# for the overview/summary IP column.
_LOOPBACK_PREFIXES = ("127.",)
_LOOPBACK_EXACT = {"::1"}


def config_agent_enabled(config: dict) -> bool:
    """Whether a VM config asks for the QEMU guest agent.

    Proxmox stores ``agent`` as ``1``, ``enabled=1`` or a comma option string; a
    missing value or ``0`` means off. Containers never carry one — the object-type
    gate is the caller's job."""
    raw = (config or {}).get("agent")
    if raw is True:
        return True
    value = str(raw or "")
    if not value or value == "0":
        return False
    return value == "1" or value.lower() == "true" or value.startswith("1,") or "enabled=1" in value


def empty_agent_info(*, enabled: bool, running: bool) -> dict:
    return {
        "enabled": enabled,
        "running": running,
        "cached": False,
        "os_name": "",
        "os_pretty_name": "",
        "os_version": "",
        "os_version_id": "",
        "architecture": "",
        "kernel_release": "",
        "kernel_version": "",
        "hostname": "",
        "ips": [],
        "interfaces": [],
    }


def _result(payload):
    """Proxmox wraps agent answers in ``{"result": ...}``; some transports unwrap
    it. Accept either shape."""
    if not isinstance(payload, dict):
        return None
    inner = payload.get("result")
    return inner if isinstance(inner, dict) else payload


def parse_osinfo(payload) -> dict:
    result = _result(payload)
    if not isinstance(result, dict):
        return {}
    name = result.get("name") or ""
    return {
        "os_name": name,
        "os_pretty_name": result.get("pretty-name") or name,
        "os_version": result.get("version") or "",
        "os_version_id": result.get("version-id") or "",
        "architecture": result.get("machine") or "",
        "kernel_release": result.get("kernel-release") or "",
        "kernel_version": result.get("kernel-version") or "",
    }


def parse_hostname(payload) -> str:
    result = _result(payload)
    if not isinstance(result, dict):
        return ""
    return str(result.get("host-name") or "")


def parse_interfaces(payload) -> tuple[list[str], list[dict]]:
    ips: list[str] = []
    interfaces: list[dict] = []
    if not isinstance(payload, dict):
        return ips, interfaces
    for iface in payload.get("result") or []:
        if not isinstance(iface, dict) or iface.get("name") == "lo":
            continue
        addresses: list[str] = []
        for addr in iface.get("ip-addresses") or []:
            ip = addr.get("ip-address") if isinstance(addr, dict) else None
            if not ip or ip in _LOOPBACK_EXACT or ip.startswith(_LOOPBACK_PREFIXES):
                continue
            addresses.append(ip)
            ips.append(ip)
        interfaces.append(
            {
                "name": iface.get("name", ""),
                "mac": iface.get("hardware-address", ""),
                "addresses": addresses,
            }
        )
    return ips, interfaces


def _agent_get(clients, *, node: str, kind: str, vmid: int, subpath: str, timeout_seconds):
    """GET one agent subpath from whichever cluster client answers first."""
    if not node:
        return None
    for client in clients:
        try:
            return client.get(
                f"nodes/{quote(node, safe='')}/{kind}/{vmid}/agent/{subpath}",
                timeout=timeout_seconds,
            )
        except ProxmoxAPIError:
            continue
    return None


def fetch_guest_agent_info(
    *,
    cluster,
    node: str,
    object_type: str,
    vmid: int,
    timeout_seconds: float = GUEST_AGENT_INFO_TIMEOUT_SECONDS,
) -> dict:
    """Read the guest agent's OS/hostname/IPs for one VM.

    Returns a summary dict shaped like :func:`empty_agent_info`. ``running`` is
    true only when the agent actually answered with usable data; callers persist
    or cache the result regardless so a non-answer is remembered as "not running"
    rather than retried on every render.
    """
    summary = empty_agent_info(enabled=True, running=False)
    kind = "qemu" if object_type == "vm" else "lxc"
    try:
        clients = cluster_clients(cluster)
    except ClusterResolutionError:
        return summary
    if not clients:
        return summary

    os_info = parse_osinfo(
        _agent_get(clients, node=node, kind=kind, vmid=vmid, subpath="get-osinfo", timeout_seconds=timeout_seconds)
    )
    hostname = parse_hostname(
        _agent_get(clients, node=node, kind=kind, vmid=vmid, subpath="get-host-name", timeout_seconds=timeout_seconds)
    )
    ips, interfaces = parse_interfaces(
        _agent_get(
            clients,
            node=node,
            kind=kind,
            vmid=vmid,
            subpath="network-get-interfaces",
            timeout_seconds=timeout_seconds,
        )
    )
    summary.update(os_info)
    summary["hostname"] = hostname
    summary["ips"] = ips[:4]
    summary["interfaces"] = interfaces
    summary["cached"] = True
    summary["running"] = bool(summary["os_pretty_name"] or summary["os_name"] or hostname or ips)
    return summary

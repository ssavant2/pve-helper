"""Pure guest config parsers and template-facing presentation helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..common import (
    CONFIG_HIDE,
    CONFIG_SECTIONS,
    CT_FEATURE_OPTIONS,
    DISK_BUS_RE,
    NET_KEY_RE,
    guest_networks,
)


def parse_net_value(value: str) -> dict:
    entry = {"model": "virtio", "mac": "", "bridge": "", "vlan": "", "firewall": False}
    for token in str(value or "").split(","):
        if "=" not in token:
            continue
        name, val = token.split("=", 1)
        if name in ("virtio", "e1000", "e1000e", "rtl8139", "vmxnet3"):
            entry["model"] = name
            entry["mac"] = val
        elif name == "bridge":
            entry["bridge"] = val
        elif name == "tag":
            entry["vlan"] = val
        elif name == "firewall":
            entry["firewall"] = val == "1"
    return entry


def config_ip_addresses(config: dict) -> list[str]:
    ips: list[str] = []
    for key, value in (config or {}).items():
        if not re.match(r"^ipconfig\d+$", key) or not isinstance(value, str):
            continue
        for token in value.split(","):
            name, sep, val = token.partition("=")
            if sep and name in {"ip", "ip6"} and val and val not in {"dhcp", "auto"}:
                ips.append(val)
    return ips


def split_kv_config(value: object) -> tuple[str, dict[str, str]]:
    head = ""
    params: dict[str, str] = {}
    for index, token in enumerate(str(value or "").split(",")):
        token = token.strip()
        if not token:
            continue
        if index == 0 and "=" not in token:
            head = token
            continue
        key, separator, raw = token.partition("=")
        if separator:
            key = key.strip()
            if key == "volume" and not head:
                head = raw.strip()
            else:
                params[key] = raw.strip()
    return head, params


def format_kv_config(head: str, params: dict[str, str], order: Iterable[str]) -> str:
    parts = [head] if head else []
    used: set[str] = set()
    for key in order:
        if key in params and params[key] != "":
            parts.append(f"{key}={params[key]}")
            used.add(key)
    for key in sorted(k for k in params if k not in used and params[k] != ""):
        parts.append(f"{key}={params[key]}")
    return ",".join(parts)


def truthy_config_value(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def set_param_bool(params: dict[str, str], key: str, enabled: bool) -> None:
    if enabled:
        params[key] = "1"
    else:
        params.pop(key, None)


def set_param_text(params: dict[str, str], key: str, value: str) -> None:
    if value:
        params[key] = value
    else:
        params.pop(key, None)


def ct_mount_summary(head: str, params: dict[str, str]) -> str:
    bits = [head or "unconfigured"]
    if params.get("mp"):
        bits.append(params["mp"])
    if params.get("size"):
        bits.append(params["size"])
    return " · ".join(bits)


def disk_size_gib_text(value: object) -> str:
    match = re.match(r"^(\d+)(?:[Gg](?:i?[Bb])?)?$", str(value or "").strip())
    return match.group(1) if match else ""


def ct_mount_rows(config: dict) -> tuple[dict, list[dict]]:
    root_head, root_params = split_kv_config(config.get("rootfs"))
    rootfs = {
        "key": "rootfs",
        "source": root_head,
        "size": root_params.get("size", ""),
        "size_gb": disk_size_gib_text(root_params.get("size")),
        "acl": truthy_config_value(root_params.get("acl")),
        "quota": truthy_config_value(root_params.get("quota")),
        "ro": truthy_config_value(root_params.get("ro")),
        "replicate": truthy_config_value(root_params.get("replicate")),
        "shared": truthy_config_value(root_params.get("shared")),
        "mountoptions": root_params.get("mountoptions", ""),
        "summary": ct_mount_summary(root_head, root_params),
    }
    mounts = []
    for key in sorted((k for k in config if re.match(r"^mp\d+$", k)), key=lambda value: int(value[2:])):
        head, params = split_kv_config(config.get(key))
        mounts.append(
            {
                "key": key,
                "source": head,
                "path": params.get("mp", ""),
                "size": params.get("size", ""),
                "size_gb": disk_size_gib_text(params.get("size")),
                "backup": truthy_config_value(params.get("backup")),
                "acl": truthy_config_value(params.get("acl")),
                "quota": truthy_config_value(params.get("quota")),
                "ro": truthy_config_value(params.get("ro")),
                "replicate": truthy_config_value(params.get("replicate")),
                "shared": truthy_config_value(params.get("shared")),
                "mountoptions": params.get("mountoptions", ""),
                "summary": ct_mount_summary(head, params),
            }
        )
    return rootfs, mounts


def ct_network_rows(config: dict) -> list[dict]:
    rows = []
    for key in sorted((k for k in config if NET_KEY_RE.match(k)), key=lambda value: int(value[3:])):
        _head, params = split_kv_config(config.get(key))
        params.setdefault("type", "veth")
        rows.append(
            {
                "key": key,
                "name": params.get("name", key.replace("net", "eth")),
                "bridge": params.get("bridge", ""),
                "firewall": truthy_config_value(params.get("firewall")),
                "gw": params.get("gw", ""),
                "gw6": params.get("gw6", ""),
                "hwaddr": params.get("hwaddr", ""),
                "ip": params.get("ip", ""),
                "ip6": params.get("ip6", ""),
                "link_down": truthy_config_value(params.get("link_down")),
                "mtu": params.get("mtu", ""),
                "rate": params.get("rate", ""),
                "tag": params.get("tag", ""),
                "trunks": params.get("trunks", ""),
                "type": params.get("type", "veth"),
                "summary": " · ".join(
                    part for part in (params.get("name"), params.get("bridge"), params.get("ip")) if part
                ),
            }
        )
    return rows


def parse_startup_options(value: object) -> dict[str, str]:
    parsed = {"order": "", "up": "", "down": ""}
    for part in str(value or "").split(","):
        key, separator, raw = part.partition("=")
        if separator and key in parsed:
            parsed[key] = raw
    return parsed


def config_enabled(config: dict, key: str, *, default: bool = False) -> bool:
    if key not in config:
        return default
    value = str(config.get(key) or "").strip().lower()
    if not value:
        return False
    return value in {"1", "true", "yes", "on"} or value.startswith("1,")


def ct_features(config: dict) -> dict[str, object]:
    _head, params = split_kv_config(config.get("features"))
    return {
        "raw": str(config.get("features", "") or ""),
        "mount": params.get("mount", ""),
        "flags": {key: truthy_config_value(params.get(key)) for key, _label in CT_FEATURE_OPTIONS},
    }


def ct_options(config: dict) -> dict[str, object]:
    startup = parse_startup_options(config.get("startup"))
    return {
        "hostname": str(config.get("hostname", "") or ""),
        "description": str(config.get("description", "") or ""),
        "onboot": config_enabled(config, "onboot"),
        "protection": config_enabled(config, "protection"),
        "nameserver": str(config.get("nameserver", "") or ""),
        "searchdomain": str(config.get("searchdomain", "") or ""),
        "arch": str(config.get("arch", "") or "amd64"),
        "ostype": str(config.get("ostype", "") or ""),
        "unprivileged": config_enabled(config, "unprivileged", default=True),
        "startup_order": startup["order"],
        "startup_up": startup["up"],
        "startup_down": startup["down"],
    }


def next_device_index(config: dict, prefix: str, extra_keys: Iterable[str] | None = None) -> int:
    used = set()
    pattern = re.compile(rf"^{prefix}(\d+)$")
    for key in list(config) + list(extra_keys or []):
        match = pattern.match(key)
        if match:
            used.add(int(match.group(1)))
    index = 0
    while index in used:
        index += 1
    return index


def row_value(value) -> dict:
    return {"value": value, "lines": []}


def row_lines(lines: list[str]) -> dict:
    return {"value": "\n".join(lines), "lines": [line for line in lines if line]}


def agent_ips_by_mac(agent_summary: dict) -> dict[str, list[str]]:
    by_mac: dict[str, list[str]] = {}
    for interface in agent_summary.get("interfaces") or []:
        if not isinstance(interface, dict):
            continue
        mac = str(interface.get("mac") or "").lower()
        addresses = [str(ip) for ip in interface.get("addresses") or [] if ip]
        if mac and addresses:
            by_mac[mac] = addresses
    return by_mac


def with_network_ip_addresses(nets: list[dict], config_ips: list[str], agent_summary: dict) -> list[dict]:
    ips_by_mac = agent_ips_by_mac(agent_summary)
    enriched = []
    for net in nets:
        addresses = ips_by_mac.get(str(net.get("mac") or "").lower(), [])
        enriched.append({**net, "ip_addresses": addresses, "ip_label": ", ".join(addresses) if addresses else "-"})
    if config_ips and len(enriched) == 1 and not enriched[0]["ip_addresses"]:
        enriched[0]["ip_addresses"] = config_ips
        enriched[0]["ip_label"] = ", ".join(config_ips)
    return enriched


def network_config_lines(net: dict) -> list[str]:
    lines = []
    if net.get("model") or net.get("mac"):
        lines.append(f"{net.get('model') or 'nic'}: {net.get('mac') or '-'}")
    if net.get("bridge"):
        lines.append(f"Bridge: {net['bridge']}")
    if net.get("vlan"):
        lines.append(f"VLAN: {net['vlan']}")
    lines.append(f"Firewall: {'on' if net.get('firewall') else 'off'}")
    if net.get("rate"):
        lines.append(f"Rate: {net['rate']}")
    if net.get("ip_addresses"):
        lines.append(f"IP: {', '.join(net['ip_addresses'])}")
    return lines


def guest_config_sections(config: dict, *, agent_summary: dict | None = None) -> list[dict]:
    shown: set[str] = set()
    sections: list[dict] = []
    for title, keys in CONFIG_SECTIONS:
        rows = [{"key": key, **row_value(config[key])} for key in keys if key in config]
        for row in rows:
            shown.add(row["key"])
        if rows:
            sections.append({"title": title, "rows": rows})

    disk_rows = [{"key": key, **row_value(config[key])} for key in sorted(config) if DISK_BUS_RE.match(key)]
    shown.update(row["key"] for row in disk_rows)
    if disk_rows:
        sections.append({"title": "Disks", "rows": disk_rows})

    config_ips = config_ip_addresses(config)
    nets = with_network_ip_addresses(guest_networks(config), config_ips, agent_summary or {})
    nets_by_label = {net["label"]: net for net in nets}
    net_rows = []
    for key in sorted(config):
        if not re.match(r"^net\d+$", key):
            continue
        net = nets_by_label.get(key)
        net_rows.append({"key": key, **(row_lines(network_config_lines(net)) if net else row_value(config[key]))})
    shown.update(row["key"] for row in net_rows)
    if net_rows:
        sections.append({"title": "Network", "rows": net_rows})

    other = []
    for key in sorted(config):
        if key in shown or key in CONFIG_HIDE:
            continue
        if key == "parent":
            other.append({"key": "Parent snapshot", **row_value(f"Snapshot: {config[key]}")})
        else:
            other.append({"key": key, **row_value(config[key])})
    if other:
        sections.append({"title": "Options", "rows": other})
    return sections


def fmt_bytes(value: float) -> str:
    number = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if number < 1024 or unit == "TiB":
            return f"{int(number)} B" if unit == "B" else f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} TiB"


def rrd_chart(points, keys, *, to_value, fmt, axis_max=None, width=340, height=90):
    series_values = []
    global_max = 0.0
    for key in keys:
        values = [to_value(point.get(key)) for point in points]
        series_values.append(values)
        for value in values:
            if value and value > global_max:
                global_max = value
    axis = float(axis_max) if axis_max else max(global_max * 1.15, 1e-9)

    series = []
    for values in series_values:
        count = len(values)
        step = width / (count - 1) if count > 1 else width
        coords = []
        for index, value in enumerate(values):
            y = height - (min(max(value, 0.0), axis) / axis) * height if axis else height
            coords.append(f"{index * step:.1f},{y:.1f}")
        line = " ".join(coords)
        area = f"0,{height} {line} {width:.1f},{height}" if coords else ""
        series.append({"line": line, "area": area})

    def axis_label(value: float) -> str:
        if fmt == "pct":
            return f"{value:.0f}%" if value >= 10 or value == 0 else f"{value:.1f}%"
        if fmt == "rate":
            return fmt_bytes(value) + "/s"
        return fmt_bytes(value)

    ticks = [
        {"y": round(height - (fraction * height), 1), "label": axis_label(axis * fraction)}
        for fraction in (1.0, 0.75, 0.5, 0.25, 0.0)
    ]
    return {"series": series, "axis_max_label": axis_label(axis), "ticks": ticks, "width": width, "height": height}


_agent_ips_by_mac = agent_ips_by_mac
_config_enabled = config_enabled
_config_ip_addresses = config_ip_addresses
_ct_features = ct_features
_ct_mount_rows = ct_mount_rows
_ct_network_rows = ct_network_rows
_ct_options = ct_options
_fmt_bytes = fmt_bytes
_format_kv_config = format_kv_config
_guest_config_sections = guest_config_sections
_next_device_index = next_device_index
_parse_net_value = parse_net_value
_parse_startup_options = parse_startup_options
_rrd_chart = rrd_chart
_set_param_bool = set_param_bool
_set_param_text = set_param_text
_split_kv_config = split_kv_config
_with_network_ip_addresses = with_network_ip_addresses

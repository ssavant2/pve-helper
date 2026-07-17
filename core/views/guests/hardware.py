"""Guest hardware + config/notes edit (extracted from _core)."""
from __future__ import annotations
from ..common import *  # noqa: F401,F403
from .. import common
from .operation_lifecycle import _audit_guest
from .presenters import (_config_enabled,_ct_features,_ct_mount_rows,_ct_network_rows,_ct_options,_format_kv_config,_next_device_index,_parse_net_value,_parse_startup_options,_set_param_bool,_set_param_text,_split_kv_config)
from .read_model_support import (_cpu_count,_is_disk_device_key,_linked_clone_disk_edit_block,_resolve_guest_detail)
from core.services.tags import TagValidationError, join_tags, parse_tags, validate_tag
from core.services.tag_catalog import load_tag_catalog
from core.services.current_guest_inventory import refresh_current_guest_from_client, update_current_guest_config


def _advanced_device_label(key: str) -> str:
    if key == "efidisk0":
        return "EFI Disk"
    if key == "tpmstate0":
        return "TPM State"
    if key == "rng0":
        return "RNG Device"
    if key == "audio0":
        return "Audio Device"
    if key.startswith("serial"):
        return "Serial Port"
    if key.startswith("usb"):
        return "USB Device"
    if key.startswith("hostpci"):
        return "PCI Device"
    if key.startswith("virtiofs"):
        return "Virtiofs Filesystem"
    return key




def _advanced_devices(config: dict) -> list[dict]:
    devices = []
    for key in sorted(config or {}):
        if ADVANCED_DEVICE_RE.match(key):
            devices.append({"key": key, "label": _advanced_device_label(key), "value": config[key]})
    return devices




def _cpu_type_options(current: str) -> tuple[tuple[str, str], ...]:
    options = list(CPU_TYPE_OPTIONS)
    if current and current not in {value for value, _label in options}:
        options.insert(1, (current, current))
    return tuple(options)




def _parse_boot_order(value: object) -> list[str]:
    text = str(value or "").strip()
    if not text.startswith("order="):
        return []
    return [item for item in text.split("=", 1)[1].split(";") if item]




def _boot_device_sort_key(key: str) -> tuple[int, int, str]:
    match = re.match(r"^([a-z]+)(\d+)$", key)
    bus_order = {"ide": 0, "sata": 1, "scsi": 2, "virtio": 3, "net": 4}
    if not match:
        return (99, 999, key)
    bus, index = match.groups()
    return (bus_order.get(bus, 90), int(index), key)




def _boot_devices(config: dict, disks: list[dict], cdroms: list[dict], nics: list[dict]) -> list[dict]:
    configured_order = _parse_boot_order(config.get("boot"))
    devices: dict[str, dict] = {}
    for disk in disks:
        size = f", size={disk['size']}" if disk.get("size") else ""
        devices[disk["label"]] = {
            "key": disk["label"],
            "label": disk["label"],
            "description": f"{disk.get('volid', '')}{size}",
        }
    for cdrom in cdroms:
        devices[cdrom["label"]] = {
            "key": cdrom["label"],
            "label": cdrom["label"],
            "description": cdrom.get("value") or "CD/DVD Drive",
        }
    for nic in nics:
        bridge = nic.get("bridge") or "not connected"
        vlan = f", VLAN {nic['vlan']}" if nic.get("vlan") else ""
        devices[nic["label"]] = {
            "key": nic["label"],
            "label": nic["label"],
            "description": f"{nic.get('model') or 'network'} on {bridge}{vlan}",
        }
    for key in configured_order:
        devices.setdefault(key, {"key": key, "label": key, "description": "Configured boot device"})

    rows: list[dict] = []
    seen: set[str] = set()
    for key in configured_order:
        if key in devices:
            rows.append({**devices[key], "enabled": True})
            seen.add(key)
    for key in sorted((key for key in devices if key not in seen), key=_boot_device_sort_key):
        rows.append({**devices[key], "enabled": False})
    return rows




def _hotplug_options(config: dict) -> list[dict]:
    raw_value = str(config.get("hotplug", HOTPLUG_DEFAULT) if "hotplug" not in config else config.get("hotplug") or "")
    enabled = {token.strip() for token in raw_value.split(",") if token.strip()}
    return [{"value": value, "label": label, "enabled": value in enabled} for value, label in HOTPLUG_OPTIONS]




@app_login_required
def guest_hardware_edit(request, object_type: str, vmid: int):
    if object_type not in GUEST_OBJECT_TYPES:
        raise Http404("Unknown guest type")

    detail = _resolve_guest_detail(object_type, vmid)
    if not detail.found:
        raise Http404("Guest not found")

    if object_type == ProxmoxInventory.ObjectType.CT:
        if request.method == "POST":
            error = _apply_ct_hardware_edit(request, detail)
            if error is None:
                return redirect("core:guest_summary", object_type=object_type, vmid=vmid)
            messages.error(request, error)

        config = detail.config
        options = create_options(object_type, detail.node)
        rootfs, mount_points = _ct_mount_rows(config)
        context = {
            **navigation_context("vms"),
            "guest": detail,
            "guest_identity": guest_identity(object_type, vmid, detail.name),
            "cores": config.get("cores", ""),
            "memory": config.get("memory", ""),
            "swap": config.get("swap", ""),
            "cpuunits": config.get("cpuunits", ""),
            "cpulimit": config.get("cpulimit", ""),
            "rootfs": rootfs,
            "mount_points": mount_points,
            "networks": _ct_network_rows(config),
            "options": options,
            "ct_options": _ct_options(config),
            "ct_features": _ct_features(config),
            "feature_options": [
                {"value": value, "label": label, "enabled": _ct_features(config)["flags"].get(value, False)}
                for value, label in CT_FEATURE_OPTIONS
            ],
            "ct_ostype_label": CT_OSTYPE_LABELS.get(str(config.get("ostype", "") or ""), str(config.get("ostype", "") or "") or "Unknown"),
            "ct_arch_label": CT_ARCH_LABELS.get(str(config.get("arch", "") or "amd64"), str(config.get("arch", "") or "amd64")),
        }
        return render(request, "core/guest_ct_hardware_edit.html", context)

    if request.method == "POST":
        error = _apply_hardware_edit(request, detail)
        if error is None:
            return redirect("core:guest_summary", object_type=object_type, vmid=vmid)
        messages.error(request, error)

    config = detail.config
    disks, cdroms = guest_disks(config, detail.node, detail.vmid)
    disks = [disk for disk in disks if _is_disk_device_key(disk["label"])]
    nics = guest_networks(config)
    options = create_options(object_type, detail.node)
    cdrom = cdroms[0] if cdroms else None
    cdrom_iso = ""
    if cdrom:
        head = str(config.get(cdrom["label"], "")).split(",")[0]
        cdrom_iso = "" if head == "none" else head

    context = {
        **navigation_context("vms"),
        "guest": detail,
        "guest_identity": guest_identity(object_type, vmid, detail.name),
        "cores": config.get("cores", ""),
        "sockets": config.get("sockets", "") or "1",
        "cpu_total": _cpu_count(config, object_type),
        "memory": config.get("memory", ""),
        "disks": disks,
        "nics": nics,
        "advanced_devices": _advanced_devices(config),
        "has_efi_disk": bool(config.get("efidisk0")),
        "has_tpm": bool(config.get("tpmstate0")),
        "has_rng": bool(config.get("rng0")),
        "has_audio": bool(config.get("audio0")),
        "cdrom": cdrom,
        "cdrom_iso": cdrom_iso,
        "options": options,
        "vm_options": _vm_settings_options(config),
        "boot_devices": _boot_devices(config, disks, cdroms, nics),
        "hotplug_options": _hotplug_options(config),
        "ostype_options": OSTYPE_LABELS.items(),
        "bios_options": (("seabios", "SeaBIOS"), ("ovmf", "OVMF (UEFI)")),
        "machine_options": (("", "Default"), ("q35", "q35"), ("pc", "i440fx / pc")),
        "scsihw_options": (
            ("", "Default"),
            ("virtio-scsi-single", "VirtIO SCSI single"),
            ("virtio-scsi-pci", "VirtIO SCSI"),
            ("lsi", "LSI 53C895A"),
            ("lsi53c810", "LSI 53C810"),
            ("megasas", "MegaRAID SAS"),
            ("pvscsi", "VMware PVSCSI"),
        ),
        "vga_options": (
            ("", "Default"),
            ("std", "Standard VGA"),
            ("virtio", "VirtIO-GPU"),
            ("virtio-gl", "VirtIO-GPU GL"),
            ("qxl", "SPICE/QXL"),
            ("qxl2", "SPICE/QXL 2 monitors"),
            ("qxl3", "SPICE/QXL 3 monitors"),
            ("qxl4", "SPICE/QXL 4 monitors"),
            ("vmware", "VMware compatible"),
            ("serial0", "Serial terminal 0"),
            ("none", "None"),
        ),
        "cpu_type_options": _cpu_type_options(str(config.get("cpu", "") or "")),
    }
    return render(request, "core/guest_hardware_edit.html", context)




def _set_checkbox_update(
    updates: dict[str, str],
    config: dict,
    key: str,
    enabled: bool,
    *,
    default: bool = False,
) -> None:
    if enabled != _config_enabled(config, key, default=default):
        updates[key] = "1" if enabled else "0"




def _set_text_update(
    updates: dict[str, str],
    delete: list[str],
    config: dict,
    key: str,
    value: str,
    *,
    allow_delete: bool = True,
) -> None:
    current = str(config.get(key, "") or "")
    if value == current:
        return
    if value or not allow_delete:
        updates[key] = value
    elif current:
        delete.append(key)




def _startup_from_post(post) -> str | None:
    parts = []
    for form_key, startup_key in (
        ("startup_order", "order"),
        ("startup_up", "up"),
        ("startup_down", "down"),
    ):
        raw = post.get(form_key, "").strip()
        if not raw:
            continue
        if not raw.isdigit():
            return None
        parts.append(f"{startup_key}={raw}")
    return ",".join(parts)




def _vm_settings_options(config: dict) -> dict[str, object]:
    startup = _parse_startup_options(config.get("startup"))
    return {
        "name": str(config.get("name", "") or ""),
        "description": str(config.get("description", "") or ""),
        "onboot": _config_enabled(config, "onboot"),
        "protection": _config_enabled(config, "protection"),
        "agent": _config_enabled(config, "agent"),
        "tablet": _config_enabled(config, "tablet", default=True),
        "acpi": _config_enabled(config, "acpi", default=True),
        "localtime": _config_enabled(config, "localtime"),
        "numa": _config_enabled(config, "numa"),
        "allow_ksm": _config_enabled(config, "allow-ksm", default=True),
        "boot": str(config.get("boot", "") or ""),
        "ostype": str(config.get("ostype", "") or "l26"),
        "bios": str(config.get("bios", "") or "seabios"),
        "vga": str(config.get("vga", "") or ""),
        "machine": str(config.get("machine", "") or ""),
        "scsihw": str(config.get("scsihw", "") or ""),
        "cpu": str(config.get("cpu", "") or ""),
        "vcpus": str(config.get("vcpus", "") or ""),
        "cpuunits": str(config.get("cpuunits", "") or ""),
        "cpulimit": str(config.get("cpulimit", "") or ""),
        "affinity": str(config.get("affinity", "") or ""),
        "balloon_enabled": str(config.get("balloon", "") or "") != "0",
        "balloon": str(config.get("balloon", "") or ""),
        "shares": str(config.get("shares", "") or ""),
        "hotplug": str(config.get("hotplug", HOTPLUG_DEFAULT) if "hotplug" not in config else config.get("hotplug") or ""),
        "startup_order": startup["order"],
        "startup_up": startup["up"],
        "startup_down": startup["down"],
    }




def _field_lists(post, *names: str) -> Iterator[tuple[str, ...]]:
    values = [post.getlist(name) for name in names]
    max_len = max((len(items) for items in values), default=0)
    for index in range(max_len):
        yield tuple((items[index].strip() if index < len(items) else "") for items in values)




def _validate_positive_int(value: str, label: str, *, allow_zero: bool = False) -> str | None:
    if not value:
        return None
    if not value.isdigit():
        return f"{label} must be a whole number."
    if int(value) < 0 or (int(value) == 0 and not allow_zero):
        return f"{label} must be a positive whole number."
    return None




def _set_optional_number_update(
    updates: dict[str, str],
    delete: list[str],
    config: dict,
    key: str,
    value: str,
    label: str,
    *,
    allow_zero: bool = False,
) -> str | None:
    error = _validate_positive_int(value, label, allow_zero=allow_zero)
    if error:
        return error
    _set_text_update(updates, delete, config, key, value)
    return None




def _set_optional_float_update(
    updates: dict[str, str],
    delete: list[str],
    config: dict,
    key: str,
    value: str,
    label: str,
) -> str | None:
    if value:
        try:
            if float(value) < 0:
                return f"{label} must be zero or higher."
        except ValueError:
            return f"{label} must be a number."
    _set_text_update(updates, delete, config, key, value)
    return None




def _apply_ct_hardware_edit(request, detail: SimpleNamespace):
    node = detail.node
    if not node:
        return "Could not resolve the container's current node."
    client = None
    fresh: dict = {}
    for candidate in common.cluster_scoped_clients():
        try:
            fresh = candidate.guest_config(node=node, object_type=detail.object_type, vmid=detail.vmid)
            client = candidate
            break
        except ProxmoxAPIError:
            continue
    if client is None:
        return "Could not read the current container config from Proxmox."
    if fresh.get("lock"):
        return f"Container is locked by another Proxmox operation ({fresh.get('lock')}); edit aborted."

    post = request.POST
    updates: dict[str, str] = {}
    delete: list[str] = []
    resizes: list[tuple[str, str]] = []

    hostname = post.get("ct_hostname", "").strip()
    if not hostname:
        return "Hostname is required."
    _set_text_update(updates, delete, fresh, "hostname", hostname, allow_delete=False)
    _set_text_update(updates, delete, fresh, "description", post.get("ct_description", "").replace("\r\n", "\n").strip())
    _set_text_update(updates, delete, fresh, "nameserver", post.get("ct_nameserver", "").strip())
    _set_text_update(updates, delete, fresh, "searchdomain", post.get("ct_searchdomain", "").strip())
    _set_checkbox_update(updates, fresh, "onboot", post.get("ct_onboot") == "on")
    _set_checkbox_update(updates, fresh, "protection", post.get("ct_protection") == "on")

    startup_value = _startup_from_post(post)
    if startup_value is None:
        return "Startup order and delays must be whole numbers."
    _set_text_update(updates, delete, fresh, "startup", startup_value)

    for form_field, key, label, allow_zero in (
        ("cores", "cores", "Cores", False),
        ("memory", "memory", "Memory", False),
        ("swap", "swap", "Swap", True),
        ("ct_cpuunits", "cpuunits", "CPU units", False),
    ):
        error = _set_optional_number_update(
            updates,
            delete,
            fresh,
            key,
            post.get(form_field, "").strip(),
            label,
            allow_zero=allow_zero,
        )
        if error:
            return error
    error = _set_optional_float_update(updates, delete, fresh, "cpulimit", post.get("ct_cpulimit", "").strip(), "CPU limit")
    if error:
        return error

    feature_parts = []
    for key, _label in CT_FEATURE_OPTIONS:
        if post.get(f"feature_{key}") == "on":
            feature_parts.append(f"{key}=1")
    mount_features = post.get("feature_mount", "").strip()
    if mount_features:
        feature_parts.append(f"mount={mount_features}")
    features_value = ",".join(feature_parts)
    _set_text_update(updates, delete, fresh, "features", features_value)

    root_head, root_params = _split_kv_config(fresh.get("rootfs"))
    if root_head:
        original = _format_kv_config(root_head, root_params, CT_MOUNT_ORDER)
        root_params_edit = dict(root_params)
        for param in ("acl", "quota", "ro", "replicate", "shared"):
            _set_param_bool(root_params_edit, param, post.get(f"rootfs_{param}") == "on")
        _set_param_text(root_params_edit, "mountoptions", post.get("rootfs_mountoptions", "").strip())
        new_root_size = post.get("rootfs_size", "").strip()
        if new_root_size:
            error = _validate_positive_int(new_root_size, "Root disk size")
            if error:
                return error
            if new_root_size != str(root_params.get("size", "")).rstrip("Gg"):
                resizes.append(("rootfs", f"{new_root_size}G"))
        updated = _format_kv_config(root_head, root_params_edit, CT_MOUNT_ORDER)
        if updated != original:
            updates["rootfs"] = updated

    for key in [k for k in fresh if re.match(r"^mp\d+$", k)]:
        if post.get(f"{key}_remove") == "on":
            delete.append(key)
            continue
        head, params = _split_kv_config(fresh.get(key))
        original = _format_kv_config(head, params, CT_MOUNT_ORDER)
        params_edit = dict(params)
        source = post.get(f"{key}_source", "").strip()
        mount_path = post.get(f"{key}_path", "").strip()
        if not source:
            return f"{key} source is required."
        if not mount_path.startswith("/"):
            return f"{key} mount path must start with /."
        for param in ("backup", "acl", "quota", "ro", "replicate", "shared"):
            _set_param_bool(params_edit, param, post.get(f"{key}_{param}") == "on")
        _set_param_text(params_edit, "mp", mount_path)
        _set_param_text(params_edit, "mountoptions", post.get(f"{key}_mountoptions", "").strip())
        new_size = post.get(f"{key}_size", "").strip()
        if new_size:
            error = _validate_positive_int(new_size, f"{key} size")
            if error:
                return error
            if new_size != str(params.get("size", "")).rstrip("Gg"):
                resizes.append((key, f"{new_size}G"))
        updated = _format_kv_config(source, params_edit, CT_MOUNT_ORDER)
        if updated != original:
            updates[key] = updated

    for storage, size, mount_path in _field_lists(post, "newmp_storage", "newmp_size", "newmp_path"):
        if not any((storage, size, mount_path)):
            continue
        if not storage:
            return "New mount point storage is required."
        error = _validate_positive_int(size, "New mount point size")
        if error:
            return error
        if not mount_path.startswith("/"):
            return "New mount point path must start with /."
        key = f"mp{_next_device_index(fresh, 'mp', updates)}"
        updates[key] = f"{storage}:{size},mp={mount_path}"

    for key in [k for k in fresh if NET_KEY_RE.match(k)]:
        if post.get(f"{key}_remove") == "on":
            delete.append(key)
            continue
        _head, params = _split_kv_config(fresh.get(key))
        params_edit = dict(params)
        name = post.get(f"{key}_name", "").strip()
        if not name:
            return f"{key} interface name is required."
        _set_param_text(params_edit, "name", name)
        _set_param_text(params_edit, "bridge", post.get(f"{key}_bridge", "").strip())
        _set_param_text(params_edit, "ip", post.get(f"{key}_ip", "").strip())
        _set_param_text(params_edit, "ip6", post.get(f"{key}_ip6", "").strip())
        _set_param_text(params_edit, "gw", post.get(f"{key}_gw", "").strip())
        _set_param_text(params_edit, "gw6", post.get(f"{key}_gw6", "").strip())
        _set_param_text(params_edit, "hwaddr", post.get(f"{key}_hwaddr", "").strip())
        _set_param_text(params_edit, "mtu", post.get(f"{key}_mtu", "").strip())
        _set_param_text(params_edit, "rate", post.get(f"{key}_rate", "").strip())
        _set_param_text(params_edit, "tag", post.get(f"{key}_tag", "").strip())
        _set_param_text(params_edit, "trunks", post.get(f"{key}_trunks", "").strip())
        params_edit["type"] = post.get(f"{key}_type", "").strip() or "veth"
        _set_param_bool(params_edit, "firewall", post.get(f"{key}_firewall") == "on")
        _set_param_bool(params_edit, "link_down", post.get(f"{key}_link_down") == "on")
        updated = _format_kv_config("", params_edit, CT_NET_ORDER)
        if updated != str(fresh.get(key, "") or ""):
            updates[key] = updated

    for name, bridge, ip, ip6, vlan, firewall in _field_lists(
        post,
        "newnet_name",
        "newnet_bridge",
        "newnet_ip",
        "newnet_ip6",
        "newnet_vlan",
        "newnet_firewall",
    ):
        if not any((name, bridge, ip, ip6, vlan, firewall)):
            continue
        net_name = name or f"eth{_next_device_index(fresh, 'net', updates)}"
        params = {"name": net_name, "type": "veth"}
        _set_param_text(params, "bridge", bridge)
        _set_param_text(params, "ip", ip or "dhcp")
        _set_param_text(params, "ip6", ip6)
        _set_param_text(params, "tag", vlan)
        _set_param_bool(params, "firewall", firewall == "on")
        updates[f"net{_next_device_index(fresh, 'net', updates)}"] = _format_kv_config("", params, CT_NET_ORDER)

    if not (updates or delete or resizes):
        return "No changes to save."
    block = _linked_clone_disk_edit_block(detail, delete, resizes)
    if block:
        return block

    try:
        if updates or delete:
            client.set_guest_config(
                node=node,
                object_type=detail.object_type,
                vmid=detail.vmid,
                updates=updates,
                delete=delete,
                digest=fresh.get("digest"),
            )
        for disk, size in resizes:
            client.put(
                f"nodes/{quote(node, safe='')}/lxc/{detail.vmid}/resize",
                data={"disk": disk, "size": size},
            )
    except ProxmoxAPIError as exc:
        if "403" in str(exc):
            return proxmox_permission_hint("a VM.Config.* privilege")
        return f"Proxmox rejected the change: {exc}"

    update_current_guest_config(
        object_type=detail.object_type,
        vmid=detail.vmid,
        node=node,
        updates=updates,
        delete=delete,
    )
    refresh_current_guest_from_client(
        client,
        node=node,
        object_type=detail.object_type,
        vmid=detail.vmid,
    )
    _audit_guest(
        request,
        detail,
        "guest.hardware.updated",
        {"updated": list(updates.keys()), "removed": delete, "resized": [d for d, _ in resizes]},
    )
    return None




def _apply_hardware_edit(request, detail: SimpleNamespace):
    node = detail.node
    if not node:
        return "Could not resolve the guest's current node."
    client = None
    fresh: dict = {}
    for candidate in common.cluster_scoped_clients():
        try:
            fresh = candidate.guest_config(node=node, object_type=detail.object_type, vmid=detail.vmid)
            client = candidate
            break
        except ProxmoxAPIError:
            continue
    if client is None:
        return "Could not read the current guest config from Proxmox."
    if fresh.get("lock"):
        return f"Guest is locked by another Proxmox operation ({fresh.get('lock')}); edit aborted."

    post = request.POST
    updates: dict[str, str] = {}
    delete: list[str] = []
    resizes: list[tuple[str, str]] = []

    new_name = post.get("vm_name", "").strip()
    if not new_name:
        return "VM name is required."
    _set_text_update(updates, delete, fresh, "name", new_name, allow_delete=False)

    new_description = post.get("vm_description", "").replace("\r\n", "\n").strip()
    _set_text_update(updates, delete, fresh, "description", new_description)

    for form_field, key, default in (
        ("vm_onboot", "onboot", False),
        ("vm_protection", "protection", False),
        ("vm_agent", "agent", False),
        ("vm_tablet", "tablet", True),
        ("vm_acpi", "acpi", True),
        ("vm_localtime", "localtime", False),
        ("vm_numa", "numa", False),
        ("vm_allow_ksm", "allow-ksm", True),
    ):
        _set_checkbox_update(updates, fresh, key, post.get(form_field) == "on", default=default)

    for form_field, key, implicit_default in (
        ("vm_boot", "boot", ""),
        ("vm_ostype", "ostype", "l26"),
        ("vm_bios", "bios", "seabios"),
        ("vm_vga", "vga", ""),
        ("vm_machine", "machine", ""),
        ("vm_scsihw", "scsihw", ""),
        ("vm_cpu", "cpu", ""),
        ("vm_affinity", "affinity", ""),
        ("vm_hotplug", "hotplug", HOTPLUG_DEFAULT),
    ):
        new_value = post.get(form_field, "").strip()
        if key not in fresh and new_value == implicit_default:
            continue
        _set_text_update(updates, delete, fresh, key, new_value)

    for form_field, key, label in (
        ("vm_vcpus", "vcpus", "VCPUs"),
        ("vm_cpuunits", "cpuunits", "CPU units"),
        ("vm_shares", "shares", "Memory shares"),
    ):
        error = _set_optional_number_update(
            updates,
            delete,
            fresh,
            key,
            post.get(form_field, "").strip(),
            label,
            allow_zero=False,
        )
        if error:
            return error

    cpulimit = post.get("vm_cpulimit", "").strip()
    if cpulimit:
        try:
            if float(cpulimit) < 0:
                return "CPU limit must be zero or higher."
        except ValueError:
            return "CPU limit must be a number."
    _set_text_update(updates, delete, fresh, "cpulimit", cpulimit)

    balloon_enabled = post.get("vm_balloon_enabled") == "on"
    balloon_value = post.get("vm_balloon", "").strip()
    if balloon_enabled:
        error = _validate_positive_int(balloon_value, "Minimum memory", allow_zero=False)
        if error:
            return error
        _set_text_update(updates, delete, fresh, "balloon", balloon_value)
    elif str(fresh.get("balloon", "") or "") != "0":
        updates["balloon"] = "0"

    startup_value = _startup_from_post(post)
    if startup_value is None:
        return "Startup order and delays must be whole numbers."
    _set_text_update(updates, delete, fresh, "startup", startup_value)

    for form_field, key in (("cores", "cores"), ("sockets", "sockets"), ("memory", "memory")):
        raw = post.get(form_field, "").strip()
        if raw and raw.isdigit() and int(raw) > 0 and raw != str(fresh.get(key, "") or ""):
            updates[key] = raw

    for key in [k for k in fresh if _is_disk_device_key(k) and "media=cdrom" not in str(fresh[k])]:
        if post.get(f"disk_{key}_remove") == "on":
            delete.append(key)
            continue
        new_size = post.get(f"disk_{key}_size", "").strip()
        if new_size and new_size.isdigit():
            resizes.append((key, f"{new_size}G"))

    for nd_storage, nd_size in _field_lists(post, "newdisk_storage", "newdisk_size"):
        if nd_storage and nd_size.isdigit():
            key = f"scsi{_next_device_index(fresh, 'scsi', updates)}"
            updates[key] = f"{nd_storage}:{nd_size}"

    for key in [k for k in fresh if NET_KEY_RE.match(k)]:
        if post.get(f"nic_{key}_remove") == "on":
            delete.append(key)
            continue
        bridge = post.get(f"nic_{key}_bridge", "").strip()
        vlan = post.get(f"nic_{key}_vlan", "").strip()
        if not bridge:
            continue
        parsed = _parse_net_value(fresh[key])
        net = f"{parsed['model']}={parsed['mac']}" if parsed["mac"] else parsed["model"]
        net += f",bridge={bridge}"
        if vlan:
            net += f",tag={vlan}"
        if parsed["firewall"]:
            net += ",firewall=1"
        if net != str(fresh[key]):
            updates[key] = net

    for new_bridge, new_vlan in _field_lists(post, "newnic_bridge", "newnic_vlan"):
        if not new_bridge:
            continue
        net = f"virtio,bridge={new_bridge}"
        if new_vlan:
            net += f",tag={new_vlan}"
        updates[f"net{_next_device_index(fresh, 'net', updates)}"] = net

    cd_key = post.get("cdrom_key", "").strip()
    if cd_key and re.match(r"^(ide|sata|scsi)\d+$", cd_key) and "media=cdrom" in str(fresh.get(cd_key, "")):
        iso = post.get("cdrom_iso", "").strip()
        value = f"{iso},media=cdrom" if iso else "none,media=cdrom"
        if value != str(fresh.get(cd_key, "") or ""):
            updates[cd_key] = value

    for key in [k for k in fresh if ADVANCED_DEVICE_RE.match(k)]:
        if post.get(f"adv_{key}_remove") == "on":
            delete.append(key)
            continue
        new_value = post.get(f"adv_{key}_value", "").strip()
        if new_value and new_value != str(fresh.get(key, "") or ""):
            updates[key] = new_value

    new_efi_storage = post.get("new_efi_storage", "").strip()
    if new_efi_storage and not fresh.get("efidisk0"):
        efi_value = f"{new_efi_storage}:0,efitype={post.get('new_efi_type', '4m') or '4m'}"
        if post.get("new_efi_pre_enrolled") == "on":
            efi_value += ",pre-enrolled-keys=1"
        updates["efidisk0"] = efi_value

    new_tpm_storage = post.get("new_tpm_storage", "").strip()
    if new_tpm_storage and not fresh.get("tpmstate0"):
        updates["tpmstate0"] = f"{new_tpm_storage}:0,version={post.get('new_tpm_version', 'v2.0') or 'v2.0'}"

    if post.get("new_rng_enable") == "on" and not fresh.get("rng0"):
        rng_source = post.get("new_rng_source", "").strip() or "/dev/urandom"
        rng_max = post.get("new_rng_max_bytes", "").strip() or "1024"
        updates["rng0"] = f"source={rng_source},max_bytes={rng_max}"

    if post.get("new_audio_enable") == "on" and not fresh.get("audio0"):
        audio_device = post.get("new_audio_device", "").strip() or "ich9-intel-hda"
        audio_driver = post.get("new_audio_driver", "").strip() or "spice"
        updates["audio0"] = f"device={audio_device},driver={audio_driver}"

    for (serial_value,) in _field_lists(post, "new_serial_value"):
        if serial_value:
            updates[f"serial{_next_device_index(fresh, 'serial', updates)}"] = serial_value

    for usb_target_type, usb_target, usb3 in _field_lists(post, "new_usb_target_type", "new_usb_target", "new_usb3"):
        if not usb_target:
            continue
        usb_key = "mapping" if usb_target_type == "mapping" else "host"
        usb_value = f"{usb_key}={usb_target}"
        if usb3 == "on":
            usb_value += ",usb3=1"
        updates[f"usb{_next_device_index(fresh, 'usb', updates)}"] = usb_value

    for pci_target_type, pci_target, pci_pcie in _field_lists(post, "new_pci_target_type", "new_pci_target", "new_pci_pcie"):
        if not pci_target:
            continue
        pci_key = "mapping" if pci_target_type == "mapping" else "host"
        pci_value = f"{pci_key}={pci_target}"
        if pci_pcie == "on":
            pci_value += ",pcie=1"
        updates[f"hostpci{_next_device_index(fresh, 'hostpci', updates)}"] = pci_value

    for virtiofs_dirid, cache, direct_io in _field_lists(
        post,
        "new_virtiofs_dirid",
        "new_virtiofs_cache",
        "new_virtiofs_direct_io",
    ):
        if not virtiofs_dirid:
            continue
        virtiofs_value = f"dirid={virtiofs_dirid}"
        if cache:
            virtiofs_value += f",cache={cache}"
        if direct_io == "on":
            virtiofs_value += ",direct-io=1"
        updates[f"virtiofs{_next_device_index(fresh, 'virtiofs', updates)}"] = virtiofs_value

    if not (updates or delete or resizes):
        return "No changes to save."
    block = _linked_clone_disk_edit_block(detail, delete, resizes)
    if block:
        return block

    try:
        if updates or delete:
            client.set_guest_config(
                node=node,
                object_type=detail.object_type,
                vmid=detail.vmid,
                updates=updates,
                delete=delete,
                digest=fresh.get("digest"),
            )
        for disk, size in resizes:
            client.put(
                f"nodes/{quote(node, safe='')}/qemu/{detail.vmid}/resize",
                data={"disk": disk, "size": size},
            )
    except ProxmoxAPIError as exc:
        if "403" in str(exc):
            return proxmox_permission_hint("a VM.Config.* privilege")
        return f"Proxmox rejected the change: {exc}"

    update_current_guest_config(
        object_type=detail.object_type,
        vmid=detail.vmid,
        node=node,
        updates=updates,
        delete=delete,
    )
    refresh_current_guest_from_client(
        client,
        node=node,
        object_type=detail.object_type,
        vmid=detail.vmid,
    )
    _audit_guest(
        request,
        detail,
        "guest.hardware.updated",
        {"updated": list(updates.keys()), "removed": delete, "resized": [d for d, _ in resizes]},
    )
    return None




@app_login_required
def guest_edit(request, object_type: str, vmid: int):
    if object_type not in GUEST_OBJECT_TYPES:
        raise Http404("Unknown guest type")

    detail = _resolve_guest_detail(object_type, vmid)
    if not detail.found:
        raise Http404("Guest not found")

    name_key = "name" if object_type == ProxmoxInventory.ObjectType.VM else "hostname"
    config = detail.config
    section = request.POST.get("section") if request.method == "POST" else request.GET.get("section")
    if section not in ("options", "hardware", "notes", "tags"):
        section = "options"

    if request.method == "POST":
        result = _apply_guest_edit(request, detail, name_key)
        if result is True:
            return redirect("core:guest_summary", object_type=object_type, vmid=vmid)
        form_error = result
        form_values = {
            "name": request.POST.get("name", ""),
            "description": request.POST.get("description", ""),
            "onboot": request.POST.get("onboot") == "on",
            "tags": request.POST.get("tags", ""),
            "cores": request.POST.get("cores", ""),
            "sockets": request.POST.get("sockets", ""),
            "memory": request.POST.get("memory", ""),
            "swap": request.POST.get("swap", ""),
            "new_tag": request.POST.get("new_tag", ""),
            "new_tag_color": request.POST.get("new_tag_color", "#3b82f6"),
        }
    else:
        form_error = ""
        form_values = {
            "name": str(config.get(name_key, "") or ""),
            "description": str(config.get("description", "") or ""),
            "onboot": str(config.get("onboot", "0")) in ("1", "True", "true"),
            "tags": " ".join(parse_guest_tags(config)),
            "cores": str(config.get("cores", "") or ""),
            "sockets": str(config.get("sockets", "") or ""),
            "memory": str(config.get("memory", "") or ""),
            "swap": str(config.get("swap", "") or ""),
            "new_tag": "",
            "new_tag_color": "#3b82f6",
        }

    current_tags = parse_tags(form_values["tags"])
    available_tags = _available_user_tags() if section == "tags" else []

    context = {
        **navigation_context("vms"),
        "guest": detail,
        "guest_identity": guest_identity(object_type, vmid, detail.name),
        "name_key_label": "Name" if object_type == ProxmoxInventory.ObjectType.VM else "Hostname",
        "section": section,
        "is_vm": object_type == ProxmoxInventory.ObjectType.VM,
        "form_values": form_values,
        "form_error": form_error,
        "current_tags": current_tags,
        "available_tags": available_tags,
    }
    return render(request, "core/guest_edit.html", context)




def _apply_guest_edit(request, detail: SimpleNamespace, name_key: str):
    node = detail.node
    if not node:
        return "Could not resolve the guest's current node."

    client = None
    fresh: dict = {}
    for candidate in common.cluster_scoped_clients():
        try:
            fresh = candidate.guest_config(node=node, object_type=detail.object_type, vmid=detail.vmid)
            client = candidate
            break
        except ProxmoxAPIError:
            continue
    if client is None:
        return "Could not read the current guest config from Proxmox."

    lock = fresh.get("lock")
    if lock:
        return f"Guest is locked by another Proxmox operation ({lock}); edit aborted."

    section = request.POST.get("section", "options")
    updates: dict[str, str] = {}
    delete: list[str] = []
    changed: list[str] = []
    registered_new_tag = False

    if section == "hardware":
        if detail.object_type == ProxmoxInventory.ObjectType.VM:
            fields = [("cores", "cores"), ("sockets", "sockets"), ("memory", "memory")]
        else:
            fields = [("cores", "cores"), ("memory", "memory"), ("swap", "swap")]
        for form_field, key in fields:
            raw = request.POST.get(form_field, "").strip()
            if raw == "":
                continue
            if not raw.isdigit() or (key != "swap" and int(raw) <= 0):
                return f"{form_field.capitalize()} must be a positive whole number."
            if raw != str(fresh.get(key, "") or ""):
                updates[key] = raw
                changed.append(key)
    if section == "notes":
        new_desc = request.POST.get("description", "").replace("\r\n", "\n").strip()
        cur_desc = str(fresh.get("description", "") or "")
        if new_desc != cur_desc:
            if new_desc:
                updates["description"] = new_desc
            else:
                delete.append("description")
            changed.append("description")

    if section == "tags":
        try:
            requested_tags = [validate_tag(tag) for tag in parse_tags(request.POST.get("tags", ""))]
            new_tag = request.POST.get("new_tag", "").strip()
            if new_tag:
                new_tag = validate_tag(new_tag)
                from core.services.tag_actions import register_tag

                _registered, registry_error = register_tag(new_tag, request.POST.get("new_tag_color", ""))
                if registry_error:
                    return f"Could not create the new tag: {registry_error}"
                if new_tag not in requested_tags:
                    requested_tags.append(new_tag)
                registered_new_tag = True
                record_audit_event(
                    request,
                    action="tag.registered",
                    object_type="cluster",
                    object_id=new_tag,
                    details={"tag": new_tag, "source": "guest-tag-editor"},
                )
            new_tags = join_tags(requested_tags)
        except TagValidationError as exc:
            return str(exc)
        cur_tags = join_tags(parse_tags(fresh))
        if new_tags != cur_tags:
            if new_tags:
                updates["tags"] = new_tags
            else:
                delete.append("tags")
            changed.append("tags")

    if section == "options":
        new_name = request.POST.get("name", "").strip()
        cur_name = str(fresh.get(name_key, "") or "")
        if new_name != cur_name:
            if new_name:
                updates[name_key] = new_name
            else:
                delete.append(name_key)
            changed.append(name_key)

        new_onboot = "1" if request.POST.get("onboot") == "on" else "0"
        cur_onboot = "1" if str(fresh.get("onboot", "0")) in ("1", "True", "true") else "0"
        if new_onboot != cur_onboot:
            updates["onboot"] = new_onboot
            changed.append("onboot")

    if not changed and not registered_new_tag:
        return "No changes to save."

    if not changed:
        return True

    try:
        client.set_guest_config(
            node=node,
            object_type=detail.object_type,
            vmid=detail.vmid,
            updates=updates,
            delete=delete,
            digest=fresh.get("digest"),
        )
    except ProxmoxAPIError as exc:
        if "403" in str(exc):
            return proxmox_permission_hint("a VM.Config.* privilege")
        return f"Proxmox rejected the change: {exc}"

    update_current_guest_config(
        object_type=detail.object_type,
        vmid=detail.vmid,
        node=node,
        updates=updates,
        delete=delete,
    )
    refresh_current_guest_from_client(
        client,
        node=node,
        object_type=detail.object_type,
        vmid=detail.vmid,
    )
    record_audit_event(
        request,
        action="guest.config.updated",
        object_type="guest",
        object_id=f"{detail.object_type}:{detail.vmid}",
        details={"fields": changed, "node": node, "vmid": detail.vmid, "target_type": detail.object_type, "name": detail.name},
        system_username="system",
    )
    return True


def _available_user_tags() -> list[str]:
    return list(load_tag_catalog().available)


@app_login_required
def guest_tag_options(request, object_type: str, vmid: int):
    if object_type not in GUEST_OBJECT_TYPES:
        raise Http404("Unknown guest type")
    detail = _resolve_guest_detail(object_type, vmid, node=request.GET.get("node", "").strip())
    if not detail.found:
        raise Http404("Guest not found")
    return JsonResponse(
        {
            "available_tags": _available_user_tags(),
            "assigned_tags": parse_guest_tags(detail.config),
        }
    )

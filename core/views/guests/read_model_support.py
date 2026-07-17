"""Current guest read model, workspace context and provider enrichments."""

from __future__ import annotations

from ..common import *  # noqa: F401,F403
from .. import common
from core.services.classification import DISK_CONFIG_KEYS
from core.services.cluster_state_identity import cluster_cache_key
from core.services.current_guest_inventory import current_inventory_state
from core.services.public_errors import public_exception_message
from core.services.tag_catalog import load_tag_catalog
from .presenters import (
    _config_ip_addresses,
    _fmt_bytes,
    _parse_net_value,
)


def _mark_linked_clones(
    rows: list[SimpleNamespace], lineage: dict[int, int] | None = None
) -> dict[int, int]:
    """Flag VM rows that are linked clones (disk backed by a template's base
    volume) and record their parent template (vmid + name when the parent row is
    present). Independent of tree ordering, so the overview can show a flat
    'linked clone of X' marker and gate 'Convert to Template' everywhere (a linked
    clone must not become a template — it would seed a deeper, fragile chain).
    Returns the lineage map so callers can reuse it without a second fetch."""
    if lineage is None:
        lineage = common.fetch_live_guest_lineage()  # {child VMID: parent VMID}
    vm_rows = {
        row.vmid: row
        for row in rows
        if row.object_type == ProxmoxInventory.ObjectType.VM and row.vmid is not None
    }
    for row in rows:
        parent = lineage.get(row.vmid) if row.object_type == ProxmoxInventory.ObjectType.VM else None
        if parent is not None and parent != row.vmid:
            row.is_linked_clone = True
            row.parent_vmid = parent
            parent_row = vm_rows.get(parent)
            row.lineage_parent_name = (parent_row.name if parent_row and parent_row.name else str(parent))
        else:
            row.is_linked_clone = False
            row.parent_vmid = None
            row.lineage_parent_name = ""
    return lineage


def _linked_clone_children(vmid: int | None) -> list[int]:
    """VMIDs of linked clones that depend on this guest's base volume (empty if
    none / API unreachable). Used to gate operations that would break the backing
    chain: destroy, storage migration, disk removal/resize on a parent template."""
    if vmid is None:
        return []
    lineage = common.fetch_live_guest_lineage()
    return sorted(clone for clone, parent in lineage.items() if parent == vmid)


def _linked_clone_disk_edit_block(detail: SimpleNamespace, delete: list[str], resizes: list) -> str | None:
    """Message blocking a disk removal/resize on a template whose base volume still
    backs linked clones (it would corrupt their backing chain), or None if safe."""
    disk_deletes = [key for key in delete if DISK_CONFIG_KEYS.match(key)]
    if not (disk_deletes or resizes):
        return None
    children = _linked_clone_children(detail.vmid)
    if not children:
        return None
    labels = ", ".join(str(child) for child in children)
    verb = "remove" if disk_deletes else "resize"
    return (
        f"Cannot {verb} this template's disk — linked clone(s) still depend on its base "
        f"volume: {labels}. Full-clone or delete them first."
    )


def _apply_workspace_lineage(rows: list[SimpleNamespace]) -> list[SimpleNamespace]:
    """Order the Inventory list as a linked-clone tree: children indented under
    their parent template (read-only). VMs on qcow2 file storage only; everything
    else stays a flat root. Returns a tree-ordered list; the same row objects get
    ``parent_vmid`` / ``lineage_parent_name`` / ``depth`` (capped at 2) /
    ``deeper_chain`` set for the template to render."""
    for row in rows:
        row.depth = 0
        row.deeper_chain = False
    lineage = _mark_linked_clones(rows)  # sets is_linked_clone / parent_vmid / name
    if not lineage:
        return rows
    vm_rows = {
        row.vmid: row
        for row in rows
        if row.object_type == ProxmoxInventory.ObjectType.VM and row.vmid is not None
    }
    children: dict[int, list[SimpleNamespace]] = {}
    for row in rows:
        # Only nest under a parent that is actually present in this view.
        if row.parent_vmid is not None and row.parent_vmid in vm_rows:
            children.setdefault(row.parent_vmid, []).append(row)
    if not children:
        return rows

    ordered: list[SimpleNamespace] = []
    visited: set[int] = set()

    def emit(row: SimpleNamespace, depth: int) -> None:
        if row.vmid is not None:
            if row.vmid in visited:
                return
            visited.add(row.vmid)
        row.depth = min(depth, 2)
        row.deeper_chain = depth > 2
        ordered.append(row)
        for child in children.get(row.vmid, []) if row.vmid is not None else []:
            emit(child, depth + 1)

    for row in rows:
        is_vm = row.object_type == ProxmoxInventory.ObjectType.VM and row.vmid is not None
        if is_vm and row.parent_vmid is not None:
            continue  # emitted under its parent's subtree
        emit(row, 0)
    return ordered


def _vms_workspace_context(active_nav: str) -> dict:
    tag_catalog = load_tag_catalog()
    rows, live_available, scan_at = _guest_rows(current_guests=tag_catalog.guests)
    available_user_tags = _decorate_guest_tag_chips(rows, catalog=tag_catalog)
    # The workspace Inventory list renders the full lineage tree; the overview
    # stays flat but still flags linked clones so the toolbar can gate 'Convert
    # to Template'. Both share the same cached lineage fetch.
    if active_nav == "vms":
        guest_list = _apply_workspace_lineage(rows)
    else:
        _mark_linked_clones(rows)
        guest_list = rows
    return {
        **navigation_context(active_nav),
        "guests": rows,
        "guest_list": guest_list,
        "guest_count": len(rows),
        "running_count": sum(1 for row in rows if row.status == "running"),
        "live_available": live_available,
        "runtime_inventory_stale": _runtime_inventory_is_stale(scan_at, rows=rows),
        "inventory_scan_at": scan_at,
        "live_inventory_cache_seconds": LIVE_GUEST_INVENTORY_CACHE_SECONDS,
        "live_status_cache_seconds": LIVE_GUEST_STATUS_CACHE_SECONDS,
        "active_object_type": "",
        "active_vmid": None,
        "available_user_tags": available_user_tags,
    }


def _decorate_guest_tag_chips(rows, *, catalog=None) -> list[str]:
    catalog = catalog or load_tag_catalog()
    for row in rows:
        row.tag_chips = catalog.chips(row.tags)
    return list(catalog.available)


def _guest_rows(*, current_guests=None):
    """Build every guest row from the non-blocking current-state projection."""
    current = list(current_guests if current_guests is not None else CurrentGuestInventory.objects.all())
    rows = [
        _build_guest_row(
            object_type=guest.object_type,
            vmid=guest.vmid,
            name=guest.name,
            status=guest.status,
            node=guest.node,
            current_obj=guest,
        )
        for guest in current
    ]

    rows.sort(key=lambda row: ((row.name or "").casefold(), row.type_sort, row.vmid or 0, row.node))
    _decorate_guests_with_scheduled_actions(rows)
    state = current_inventory_state()
    latest_runtime_at = max(
        (guest.runtime_observed_at for guest in current if guest.runtime_observed_at),
        default=None,
    )
    return rows, latest_runtime_at is not None, latest_runtime_at


def _runtime_inventory_is_stale(refreshed_at, *, rows=None) -> bool:
    if refreshed_at is None:
        return True
    if rows is not None and any(getattr(row, "runtime_observed_at", None) is None for row in rows):
        return True
    max_age = timedelta(
        minutes=max(1, settings.CURRENT_GUEST_REFRESH_INTERVAL_MINUTES) * 2,
        seconds=30,
    )
    return refreshed_at < tz.now() - max_age


def _current_obj_for_live_guest(
    current_by_key: dict[tuple[str, str, int], CurrentGuestInventory],
    current_by_identity: dict[tuple[str, int], list[CurrentGuestInventory]],
    node: str,
    object_type: str,
    vmid: int,
) -> CurrentGuestInventory | None:
    exact = current_by_key.get((node or "", object_type, vmid))
    if exact is not None:
        return exact
    legacy_matches = current_by_identity.get((object_type, vmid), [])
    if len(legacy_matches) == 1:
        return legacy_matches[0]
    return None


def _guest_target_value(object_type: str, vmid: int | str | None, node: str = "") -> str:
    base = f"{object_type}:{vmid}"
    return f"{base}@{node}" if node else base


def _display_lock(value: object) -> str:
    """A guest's config lock for the 'needs a look' badge — but ``suspended`` is
    hibernate (an expected state shown by the moon icon), not a problem, so drop it."""
    lock = str(value or "").strip()
    return "" if lock == "suspended" else lock


def _build_guest_row(*, object_type, vmid, name, status, node, current_obj, live_guest=None) -> SimpleNamespace:
    config = current_obj.config if current_obj is not None and isinstance(current_obj.config, dict) else {}
    template = object_type == ProxmoxInventory.ObjectType.VM and (
        bool(getattr(live_guest, "is_template", False)) or is_template(config)
    )
    if template:
        type_label, type_filter, type_sort = "Template", "template", 0
    elif object_type == ProxmoxInventory.ObjectType.CT:
        type_label, type_filter, type_sort = "CT", "ct", 2
    else:
        type_label, type_filter, type_sort = "VM", "vm", 1
    runtime = live_guest or current_obj
    cpu = _float_or_zero(getattr(runtime, "cpu", getattr(runtime, "cpu_usage", 0.0)))
    mem = _int_or_zero(getattr(runtime, "mem", getattr(runtime, "memory_used_bytes", 0)))
    maxmem = _int_or_zero(getattr(runtime, "maxmem", getattr(runtime, "memory_max_bytes", 0))) or _config_mem_bytes(config)
    used_disk = _int_or_zero(getattr(runtime, "disk", getattr(runtime, "disk_used_bytes", 0)))
    provisioned_disk = _int_or_zero(getattr(runtime, "maxdisk", getattr(runtime, "disk_max_bytes", 0))) or _config_disk_bytes(config)
    uptime = _int_or_zero(getattr(runtime, "uptime", getattr(runtime, "uptime_seconds", 0)))
    cpus = _int_or_zero(config.get("vcpus")) or _cpu_count(config, object_type)
    macs = _config_mac_addresses(config)
    ips = _config_ip_addresses(config)
    storage_ids = _config_storage_ids(config)
    identity = guest_identity(object_type, vmid, name or "")
    return SimpleNamespace(
        cluster=getattr(current_obj, "cluster", None),
        cluster_key=getattr(getattr(current_obj, "cluster", None), "key", ""),
        object_type=object_type,
        vmid=vmid,
        name=name or "",
        config=config,
        guest_identity=identity,
        status=status or "",
        state_label=_guest_state_label(status),
        node=node or "",
        lock=_display_lock(getattr(runtime, "lock", getattr(runtime, "runtime_lock", "")) or config.get("lock")),
        is_template=template,
        type_label=type_label,
        type_filter=type_filter,
        type_sort=type_sort,
        target_id=_guest_target_value(object_type, vmid, node),
        tags=parse_guest_tags(config),
        in_current_inventory=current_obj is not None,
        detail_url=reverse("core:guest_summary", args=[object_type, vmid]) if vmid is not None else "",
        provisioned_bytes=provisioned_disk,
        provisioned_label=provisioned_disk and _fmt_bytes(provisioned_disk) or "-",
        used_bytes=used_disk,
        used_label=used_disk and _fmt_bytes(used_disk) or "-",
        cpu_value=round(cpu * 100, 2),
        cpu_label=f"{round(cpu * 100, 1)}%" if cpu else "0%",
        mem_bytes=mem,
        mem_label=mem and _fmt_bytes(mem) or "-",
        active_mem_bytes=mem,
        active_mem_label=mem and _fmt_bytes(mem) or "-",
        memory_size_bytes=maxmem,
        memory_size_label=maxmem and _fmt_bytes(maxmem) or "-",
        guest_os_label=_guest_os_label(config),
        agent_enabled=_guest_agent_config_enabled(config, object_type),
        agent_label=_guest_agent_config_label(config, object_type),
        uptime_seconds=uptime,
        runtime_observed_at=getattr(current_obj, "runtime_observed_at", None),
        uptime_label=_format_uptime(uptime) if uptime else "-",
        cpus=cpus,
        cpu_count_label=str(cpus) if cpus else "-",
        nic_count=len(macs),
        nic_count_label=str(len(macs)),
        disk_count=_config_disk_count(config),
        disk_count_label=str(_config_disk_count(config)),
        ip_addresses=ips,
        ip_label=", ".join(ips[:3]) if ips else "-",
        mac_addresses=macs,
        mac_label=", ".join(macs[:3]) if macs else "-",
        storage_label=", ".join(storage_ids) if storage_ids else "-",
        tags_label=", ".join(parse_guest_tags(config)) if parse_guest_tags(config) else "-",
        # The scan/config payload has no snapshot data, so presence is unknown
        # until the live probe (vms_overview_snapshot_info) answers. Render "-"
        # rather than a misleading "No".
        has_snapshot=None,
        has_snapshot_label="-",
        # Lineage flags; populated by _mark_linked_clones / _apply_workspace_lineage.
        is_linked_clone=False,
        parent_vmid=None,
        lineage_parent_name="",
        depth=0,
        deeper_chain=False,
    )


def _guest_state_label(status: str) -> str:
    if status == "running":
        return "Powered On"
    if status == "stopped":
        return "Powered Off"
    if status == "paused":
        return "Suspended"
    if status == "hibernated":
        return "Hibernated"
    return (status or "-").title()


def _config_disk_count(config: dict) -> int:
    return len(
        [
            key
            for key, value in (config or {}).items()
            if _is_disk_device_key(key) and isinstance(value, str) and "media=cdrom" not in value
        ]
    )


def _live_guest_has_snapshot(row: SimpleNamespace, *, allow_fetch: bool = True) -> bool | None:
    """True/False if the live snapshot list could be read (cached ~30 s per
    guest), or None when unavailable so the caller can fall back to the scan
    value. When ``allow_fetch`` is False only the cache is consulted — used to
    keep serving cached probes after a request's live-call budget is spent."""
    cluster = getattr(row, "cluster", None)
    if not row.node or row.vmid is None or not cluster:
        return None
    cache_key = cluster_cache_key(
        "guest-snapshot-present:v2", cluster, row.node, row.object_type, row.vmid
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return bool(cached)
    if not allow_fetch:
        return None
    kind = "qemu" if row.object_type == ProxmoxInventory.ObjectType.VM else "lxc"
    path = f"nodes/{quote(row.node, safe='')}/{kind}/{row.vmid}/snapshot"
    for client in common.cluster_scoped_clients():
        try:
            data = client.get(path, timeout=2)
        except ProxmoxAPIError:
            continue
        if not isinstance(data, list):
            return None
        result = any(
            isinstance(snapshot, dict) and str(snapshot.get("name") or "") not in {"", "current"}
            for snapshot in data
        )
        cache.set(cache_key, result, LIVE_GUEST_INVENTORY_CACHE_SECONDS)
        return result
    return None


def _config_disk_bytes(config: dict) -> int:
    total = 0
    for key, value in (config or {}).items():
        if not _is_disk_device_key(key) or not isinstance(value, str) or "media=cdrom" in value:
            continue
        total += _disk_config_size_bytes(value)
    return total


def _config_storage_ids(config: dict) -> list[str]:
    storage_ids: list[str] = []
    seen: set[str] = set()
    for volid in extract_disk_references(config or {}):
        storage_id, sep, _volume = volid.partition(":")
        if sep and storage_id and storage_id not in seen:
            seen.add(storage_id)
            storage_ids.append(storage_id)
    return storage_ids


def _config_mac_addresses(config: dict) -> list[str]:
    macs: list[str] = []
    for key, value in (config or {}).items():
        if not NET_KEY_RE.match(key) or not isinstance(value, str):
            continue
        parsed = _parse_net_value(value)
        if parsed.get("mac"):
            macs.append(parsed["mac"])
    return macs


def _guest_agent_config_label(config: dict, object_type: str) -> str:
    if not _guest_agent_config_enabled(config, object_type):
        return "Disabled" if object_type == ProxmoxInventory.ObjectType.VM else "-"
    return "Enabled"


def _guest_agent_config_enabled(config: dict, object_type: str) -> bool:
    if object_type != ProxmoxInventory.ObjectType.VM:
        return False
    raw_value = (config or {}).get("agent")
    if raw_value is True:
        return True
    value = str((config or {}).get("agent") or "")
    if not value or value == "0":
        return False
    return value == "1" or value.lower() == "true" or value.startswith("1,") or "enabled=1" in value


def _is_disk_device_key(key: str) -> bool:
    return bool(re.match(r"^(ide|sata|scsi|virtio)\d+$", str(key or "")))


def _disk_config_size_bytes(value: str) -> int:
    for token in str(value or "").split(","):
        if token.startswith("size="):
            return _size_text_to_bytes(token.split("=", 1)[1])
    return 0


def _size_text_to_bytes(value: str) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    match = re.match(r"^([0-9]+(?:\.[0-9]+)?)([KMGTPE]?)(i?B?)?$", text, re.IGNORECASE)
    if not match:
        return 0
    number = float(match.group(1))
    unit = match.group(2).upper()
    factor = {
        "": 1,
        "K": 1024,
        "M": 1024**2,
        "G": 1024**3,
        "T": 1024**4,
        "P": 1024**5,
        "E": 1024**6,
    }.get(unit, 1)
    return int(number * factor)


def _guest_health(detail: SimpleNamespace) -> dict:
    """Read-only health signals for a guest. Currently a stale/active config lock,
    with a copy/paste unlock command for the specific guest (clearing a lock via
    our API token is root@pam-only, so we point at the node CLI instead)."""
    lock = str((detail.current or {}).get("lock") or (detail.config or {}).get("lock") or "").strip()
    issues: list[dict] = []
    # A 'suspended' lock is hibernate (expected state), not a health problem.
    if lock and lock != "suspended":
        unlock_cmd = "qm" if detail.object_type == ProxmoxInventory.ObjectType.VM else "pct"
        issues.append(
            {
                "kind": "lock",
                "title": f"Locked by “{lock}”",
                "detail": (
                    "A Proxmox operation holds a config lock on this guest. If no matching "
                    "task is still running (check Recent Tasks), the lock is stale."
                ),
                "lock": lock,
                "node": detail.node,
                "command": f"{unlock_cmd} unlock {detail.vmid}",
            }
        )
    return {"ok": not issues, "issues": issues}


def _guest_lineage(detail: SimpleNamespace) -> dict:
    """Linked-clone lineage for the detail page: this VM's parent template and/or
    its own linked children (read-only). VM/qcow2/file-storage only."""
    empty = {"parent": None, "children": []}
    if detail.object_type != ProxmoxInventory.ObjectType.VM or detail.vmid is None:
        return empty
    lineage = common.fetch_live_guest_lineage()
    if not lineage:
        return empty
    names = {
        guest.vmid: guest.name
        for guest in CurrentGuestInventory.objects.filter(object_type=ProxmoxInventory.ObjectType.VM)
        if guest.object_type == ProxmoxInventory.ObjectType.VM
    }
    parent = None
    parent_vmid = lineage.get(detail.vmid)
    if parent_vmid:
        parent = {"vmid": parent_vmid, "name": names.get(parent_vmid) or str(parent_vmid)}
    children = [
        {"vmid": child, "name": names.get(child) or str(child)}
        for child, pvmid in sorted(lineage.items())
        if pvmid == detail.vmid
    ]
    return {"parent": parent, "children": children}


def _require_guest(object_type: str, vmid: int, *, node: str = "") -> SimpleNamespace:
    if object_type not in GUEST_OBJECT_TYPES:
        raise Http404("Unknown guest type")
    detail = _resolve_guest_detail(object_type, vmid, node=node)
    if not detail.found:
        raise Http404("Guest not found")
    return detail


def _resolve_guest_detail(object_type: str, vmid: int, *, node: str = "") -> SimpleNamespace:
    """Resolve read-only guest context without provider I/O in the request."""
    current_query = CurrentGuestInventory.objects.filter(object_type=object_type, vmid=vmid)
    if node:
        current_query = current_query.filter(node=node)
    obj = current_query.first()
    if obj is None:
        return SimpleNamespace(
            cluster=None,
            cluster_key="",
            object_type=object_type,
            vmid=vmid,
            name="",
            node=node,
            status="",
            config={},
            current={},
            live_ok=False,
            ambiguous=False,
            ambiguous_nodes=[],
            found=False,
        )

    config = obj.config if isinstance(obj.config, dict) else {}
    current = {
        "status": obj.status,
        "lock": obj.runtime_lock,
        "cpu": obj.cpu_usage,
        "mem": obj.memory_used_bytes,
        "maxmem": obj.memory_max_bytes,
        "disk": obj.disk_used_bytes,
        "maxdisk": obj.disk_max_bytes,
        "uptime": obj.uptime_seconds,
    }

    return SimpleNamespace(
        cluster=obj.cluster,
        cluster_key=obj.cluster.key if obj.cluster_id else "",
        object_type=object_type,
        vmid=vmid,
        name=obj.name or "",
        node=obj.node or "",
        status=obj.status or "",
        config=config,
        current=current,
        live_ok=bool(obj.runtime_observed_at),
        ambiguous=False,
        ambiguous_nodes=[],
        found=True,
    )


def _guest_tab_context(detail: SimpleNamespace, active_tab: str) -> dict:
    is_tmpl = detail.object_type == ProxmoxInventory.ObjectType.VM and is_template(detail.config)
    if is_tmpl:
        type_label = "Template"
    elif detail.object_type == ProxmoxInventory.ObjectType.CT:
        type_label = "CT"
    else:
        type_label = "VM"
    target = f"{detail.object_type}:{detail.vmid}"
    active_target = _guest_target_value(detail.object_type, detail.vmid, detail.node)
    tag_catalog = load_tag_catalog()
    rows, live_available, scan_at = _guest_rows(current_guests=tag_catalog.guests)
    available_user_tags = _decorate_guest_tag_chips(rows, catalog=tag_catalog)
    # The sidebar list on every detail/Summary page is the same workspace tree,
    # so it must render the lineage indentation too (not a flat list).
    guest_list = _apply_workspace_lineage(rows)
    tag_row = SimpleNamespace(tags=parse_guest_tags(detail.config))
    _decorate_guest_tag_chips([tag_row], catalog=tag_catalog)
    return {
        **navigation_context("vms"),
        "guest": detail,
        "guest_identity": guest_identity(detail.object_type, detail.vmid, detail.name),
        "guest_is_template": is_tmpl,
        "guest_type_label": type_label,
        "guest_tags": parse_guest_tags(detail.config),
        "guest_tag_chips": tag_row.tag_chips,
        "guest_tabs": _guest_tabs(detail, active_tab),
        "active_guest_tab": active_tab,
        "guest_list": guest_list,
        "live_available": live_available,
        "runtime_inventory_stale": _runtime_inventory_is_stale(scan_at, rows=rows),
        "inventory_scan_at": scan_at,
        "active_object_type": detail.object_type,
        "active_vmid": detail.vmid,
        "active_guest_target_id": active_target,
        "available_user_tags": available_user_tags,
        "schedule_action_url": f"{reverse('core:scheduled_task_create')}?{urlencode({'target': target})}",
        "scheduled_actions_url": f"{reverse('core:scheduled_tasks')}?{urlencode({'target': target})}",
    }


def _guest_tabs(detail: SimpleNamespace, active_tab: str) -> list[dict]:
    args = [detail.object_type, detail.vmid]
    tabs = [
        {"key": "summary", "label": "Summary", "url": reverse("core:guest_summary", args=args)},
        {"key": "console", "label": "Console", "url": reverse("core:guest_console", args=args)},
        {"key": "monitor", "label": "Monitor", "url": reverse("core:guest_monitor", args=args)},
        {"key": "configure", "label": "Configure", "url": reverse("core:guest_configure", args=args)},
        {"key": "permissions", "label": "Permissions", "url": reverse("core:guest_permissions", args=args)},
        {"key": "datastores", "label": "Datastores", "url": reverse("core:guest_datastores", args=args)},
        {"key": "networks", "label": "Networks", "url": reverse("core:guest_networks", args=args)},
        {"key": "snapshots", "label": "Snapshots", "url": reverse("core:guest_snapshots", args=args)},
        {"key": "guest_agent", "label": "Guest Agent", "url": reverse("core:guest_agent", args=args)},
        {"key": "cloudinit", "label": "Cloud-Init", "url": reverse("core:guest_cloudinit", args=args)},
        {"key": "backup", "label": "Backup", "url": reverse("core:guest_backup", args=args)},
        {"key": "replication", "label": "Replication", "url": reverse("core:guest_replication", args=args)},
        {"key": "firewall", "label": "Firewall", "url": reverse("core:guest_firewall", args=args)},
    ]
    for tab in tabs:
        tab["enabled"] = True
        tab["active"] = tab["key"] == active_tab
    return tabs


def _guest_api_get(detail: SimpleNamespace, subpath: str, *, timeout_seconds: float | None = None):
    """GET a guest-scoped Proxmox path (e.g. 'snapshot', 'rrddata?...',
    'agent/get-osinfo'); returns (data, error_message)."""
    kind = "qemu" if detail.object_type == ProxmoxInventory.ObjectType.VM else "lxc"
    if not detail.node:
        return None, "The guest's node could not be resolved."
    err = "No Proxmox endpoint could reach this guest."
    for client in common.cluster_scoped_clients():
        try:
            return client.get(
                f"nodes/{quote(detail.node, safe='')}/{kind}/{detail.vmid}/{subpath}",
                timeout=timeout_seconds,
            ), None
        except ProxmoxAPIError as exc:
            err = public_exception_message(
                exc,
                operation="guest_api_read",
                fallback="Proxmox guest data is temporarily unavailable.",
            )
    return None, err


def _guest_os_label(config: dict) -> str:
    ostype = str(config.get("ostype") or "")
    return OSTYPE_LABELS.get(ostype, ostype or "Unknown")


def _guest_agent_summary(detail: SimpleNamespace, *, allow_fetch: bool = True) -> dict:
    """Best-effort guest-agent OS name + IPs for the Summary Guest OS card.
    Only queries when the agent is enabled; degrades silently otherwise."""
    config = detail.config
    enabled = _guest_agent_config_enabled(config, detail.object_type)
    if detail.object_type != ProxmoxInventory.ObjectType.VM or not enabled or detail.status != "running":
        return _empty_guest_agent_summary(enabled=enabled, running=False)

    cluster = getattr(detail, "cluster", None)
    if not cluster:
        return _empty_guest_agent_summary(enabled=enabled, running=False)
    cache_key = cluster_cache_key(
        "guest-agent-summary:v2", cluster, detail.node, detail.object_type, detail.vmid
    )
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return cached
    if not allow_fetch:
        return _empty_guest_agent_summary(enabled=enabled, running=False)

    os_name = ""
    os_pretty_name = ""
    os_version = ""
    os_version_id = ""
    architecture = ""
    kernel_release = ""
    kernel_version = ""
    hostname = ""
    ips: list[str] = []
    interfaces: list[dict] = []
    os_data, _err = _guest_api_get(detail, "agent/get-osinfo", timeout_seconds=GUEST_AGENT_API_TIMEOUT_SECONDS)
    if isinstance(os_data, dict):
        result = os_data.get("result") if isinstance(os_data.get("result"), dict) else os_data
        if isinstance(result, dict):
            os_name = result.get("name") or ""
            os_pretty_name = result.get("pretty-name") or os_name
            os_version = result.get("version") or ""
            os_version_id = result.get("version-id") or ""
            architecture = result.get("machine") or ""
            kernel_release = result.get("kernel-release") or ""
            kernel_version = result.get("kernel-version") or ""
    host_data, _err = _guest_api_get(detail, "agent/get-host-name", timeout_seconds=GUEST_AGENT_API_TIMEOUT_SECONDS)
    if isinstance(host_data, dict):
        result = host_data.get("result") if isinstance(host_data.get("result"), dict) else host_data
        if isinstance(result, dict):
            hostname = result.get("host-name", "")
    net_data, _err = _guest_api_get(detail, "agent/network-get-interfaces", timeout_seconds=GUEST_AGENT_API_TIMEOUT_SECONDS)
    if isinstance(net_data, dict):
        for iface in net_data.get("result") or []:
            if not isinstance(iface, dict) or iface.get("name") == "lo":
                continue
            addresses = []
            for addr in iface.get("ip-addresses") or []:
                ip = addr.get("ip-address") if isinstance(addr, dict) else None
                if ip and not ip.startswith("127.") and ip != "::1":
                    addresses.append(ip)
                    ips.append(ip)
            interfaces.append(
                {
                    "name": iface.get("name", ""),
                    "mac": iface.get("hardware-address", ""),
                    "addresses": addresses,
                }
            )
    summary = {
        "enabled": True,
        "running": bool(os_pretty_name or os_name or hostname or ips),
        "cached": True,
        "os_name": os_name,
        "os_pretty_name": os_pretty_name,
        "os_version": os_version,
        "os_version_id": os_version_id,
        "architecture": architecture,
        "kernel_release": kernel_release,
        "kernel_version": kernel_version,
        "hostname": hostname,
        "ips": ips[:4],
        "interfaces": interfaces,
    }
    cache.set(cache_key, summary, LIVE_GUEST_INVENTORY_CACHE_SECONDS)
    return summary


def _empty_guest_agent_summary(*, enabled: bool, running: bool) -> dict:
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


def _guest_pool_label(detail: SimpleNamespace) -> str:
    if not detail.live_ok or not detail.node:
        return ""
    for client in common.cluster_scoped_clients():
        if not hasattr(client, "get"):
            continue
        try:
            client.guest_current(node=detail.node, object_type=detail.object_type, vmid=detail.vmid)
            _pools, memberships = _guest_pool_memberships(client, detail)
            return memberships[0] if len(memberships) == 1 else ""
        except ProxmoxAPIError:
            continue
    return ""


def _guest_ha_summary(detail: SimpleNamespace) -> dict:
    """Return a small, read-only HA view for the Summary card.

    HA resources are cluster scoped, but a pve-helper installation can point at
    several unrelated endpoints. Resolve the guest against an endpoint first,
    then only read that endpoint's cluster data. This is deliberately a read
    model; Module 5 owns HA writes and placement rules.
    """
    unavailable = {
        "available": False,
        "managed": False,
        "label": "HA / cluster unavailable",
        "badge_class": "warning",
        "message": "Live HA status could not be read for this guest.",
        "cluster_name": "",
        "cluster_nodes": 0,
        "quorate": False,
        "desired_state": "",
        "max_restart": "",
        "max_relocate": "",
        "placement": "",
    }
    if not detail.live_ok or not detail.node:
        return unavailable

    cluster = getattr(detail, "cluster", None)
    if not cluster:
        return unavailable
    cache_key = cluster_cache_key(
        "guest-ha-summary:v2", cluster, detail.node, detail.object_type, detail.vmid
    )
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return cached

    for client in common.cluster_scoped_clients():
        if not hasattr(client, "get"):
            continue
        try:
            # Confirms this endpoint owns the guest before querying its cluster.
            client.guest_current(node=detail.node, object_type=detail.object_type, vmid=detail.vmid)
            cluster_status = client.get("cluster/status", timeout=2)
            resources = client.get("cluster/ha/resources", timeout=2)
        except (AttributeError, ProxmoxAPIError):
            continue

        cluster_status = cluster_status if isinstance(cluster_status, list) else []
        resources = resources if isinstance(resources, list) else []
        cluster = next(
            (entry for entry in cluster_status if isinstance(entry, dict) and entry.get("type") == "cluster"),
            None,
        )
        if not cluster:
            result = {
                **unavailable,
                "message": "This endpoint is not reporting a Proxmox cluster.",
            }
        else:
            cluster_nodes = _int_or_zero(cluster.get("nodes"))
            quorate = bool(cluster.get("quorate"))
            cluster_name = str(cluster.get("name") or "Proxmox cluster")
            common_fields = {
                "cluster_name": cluster_name,
                "cluster_nodes": cluster_nodes,
                "quorate": quorate,
            }
            if cluster_nodes < 2:
                result = {
                    **unavailable,
                    **common_fields,
                    "message": "A multi-node cluster is required before HA can protect this guest.",
                }
            elif not quorate:
                result = {
                    **unavailable,
                    **common_fields,
                    "message": "The cluster is not quorate, so HA is unavailable.",
                }
            else:
                resource_id = f"{'ct' if detail.object_type == ProxmoxInventory.ObjectType.CT else 'vm'}:{detail.vmid}"
                resource = next(
                    (
                        entry
                        for entry in resources
                        if isinstance(entry, dict) and str(entry.get("sid") or entry.get("id") or "") == resource_id
                    ),
                    None,
                )
                if resource is None:
                    result = {
                        **common_fields,
                        "available": True,
                        "managed": False,
                        "label": "Not managed",
                        "badge_class": "",
                        "message": "This guest is not configured as a Proxmox HA resource.",
                        "desired_state": "",
                        "max_restart": "",
                        "max_relocate": "",
                        "placement": "",
                    }
                else:
                    result = {
                        **common_fields,
                        "available": True,
                        "managed": True,
                        "label": "Managed",
                        "badge_class": "completed",
                        "message": "",
                        "desired_state": str(resource.get("state") or "started").capitalize(),
                        "max_restart": resource.get("max_restart") or resource.get("max-restart") or "Default",
                        "max_relocate": resource.get("max_relocate") or resource.get("max-relocate") or "Default",
                        "placement": resource.get("group") or resource.get("rule") or "Default placement",
                    }
        cache.set(cache_key, result, LIVE_GUEST_INVENTORY_CACHE_SECONDS)
        return result

    return unavailable


def _guest_vm_details(detail: SimpleNamespace, pool_label: str = "") -> list[dict]:
    config = detail.config
    current = detail.current
    rows: list[dict] = []

    def add(label, value, key: str = ""):
        if value not in (None, "", "-"):
            rows.append({"label": label, "value": value, "key": key})

    add("Guest OS", _guest_os_label(config))
    add("Node", detail.node or "-")
    add("Pool", pool_label or "No pool", "pool")
    if config.get("bios"):
        add("Firmware", "UEFI (OVMF)" if config.get("bios") == "ovmf" else "SeaBIOS")
    if config.get("machine"):
        add("Machine", config.get("machine"))
    if config.get("boot"):
        add("Boot order", str(config.get("boot")).replace("order=", ""))
    if detail.object_type == ProxmoxInventory.ObjectType.VM:
        add("Guest agent", _guest_agent_config_label(config, detail.object_type), "agent")
    uptime = current.get("uptime") if isinstance(current, dict) else None
    if uptime:
        add("Uptime", _format_uptime(int(uptime)))
    return rows


def _guest_cpu_label(config: dict, object_type: str) -> str:
    cores = _int_or_zero(config.get("cores"))
    if object_type == ProxmoxInventory.ObjectType.VM:
        sockets = _int_or_zero(config.get("sockets")) or 1
        total = sockets * cores
        if not total:
            return ""
        return f"{total} vCPU ({sockets} socket x {cores} cores)" if sockets > 1 else f"{total} vCPU"
    return f"{cores} cores" if cores else ""


def _config_mem_bytes(config: dict) -> int:
    return _int_or_zero(config.get("memory")) * 1024 * 1024


def _cpu_count(config: dict, object_type: str) -> int:
    cores = _int_or_zero(config.get("cores"))
    if object_type == ProxmoxInventory.ObjectType.VM:
        return (_int_or_zero(config.get("sockets")) or 1) * cores
    return cores


def _guest_cpu_topology(config: dict, object_type: str) -> dict | None:
    cores = _int_or_zero(config.get("cores"))
    if not cores:
        return None
    if object_type == ProxmoxInventory.ObjectType.CT:
        return {
            "vcpus": cores,
            "cores": cores,
            "sockets": 1,
            "threads": 1,
            "numa": False,
        }
    sockets = _int_or_zero(config.get("sockets")) or 1
    return {
        "vcpus": sockets * cores,
        "cores": cores,
        "sockets": sockets,
        "threads": 1,
        "numa": str(config.get("numa", "0")) in ("1", "true", "True"),
    }


def _guest_usage(current: dict, config: dict, object_type: str) -> dict:
    """Always returns usage figures. When the guest is stopped the used values
    are 0 and the totals come from the config, so the Usage card never vanishes."""
    running = isinstance(current, dict) and current.get("status") == "running"
    if running:
        cpu = float(current.get("cpu") or 0)
        cpus = _int_or_zero(current.get("cpus")) or _cpu_count(config, object_type)
        mem = _int_or_zero(current.get("mem"))
        maxmem = _int_or_zero(current.get("maxmem")) or _config_mem_bytes(config)
        maxdisk = _int_or_zero(current.get("maxdisk"))
    else:
        cpu = 0.0
        cpus = _cpu_count(config, object_type)
        mem = 0
        maxmem = _config_mem_bytes(config)
        maxdisk = 0
    return {
        "running": running,
        "cpu_pct": round(cpu * 100, 1),
        "cpus": cpus,
        "mem": mem,
        "maxmem": maxmem,
        "mem_pct": round(mem / maxmem * 100, 1) if maxmem else 0,
        "maxdisk": maxdisk,
    }


def _float_or_zero(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _format_uptime(seconds: int) -> str:
    if seconds <= 0:
        return "-"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return " ".join(parts) or "<1m"


def _guest_pool_memberships(client, detail: SimpleNamespace) -> tuple[list[str], list[str]]:
    raw_pools = client.get("pools")
    if not isinstance(raw_pools, list):
        raise ProxmoxAPIError("Unexpected Proxmox pool list response.")
    pool_ids = sorted(
        {
            str(pool.get("poolid") or "").strip()
            for pool in raw_pools
            if isinstance(pool, dict) and str(pool.get("poolid") or "").strip()
        },
        key=str.casefold,
    )
    memberships: list[str] = []
    for pool_id in pool_ids:
        pool = client.get(f"pools/{quote(pool_id, safe='')}")
        members = pool.get("members") if isinstance(pool, dict) else None
        if not isinstance(members, list):
            raise ProxmoxAPIError(f"Unexpected member response for pool '{pool_id}'.")
        if any(_pool_member_matches_guest(member, detail) for member in members):
            memberships.append(pool_id)
    return pool_ids, memberships


def _pool_member_matches_guest(member: object, detail: SimpleNamespace) -> bool:
    if not isinstance(member, dict):
        return False
    try:
        vmid = int(member.get("vmid"))
    except (TypeError, ValueError):
        return False
    if vmid != detail.vmid:
        return False
    member_type = str(member.get("type") or "").strip().lower()
    expected = "qemu" if detail.object_type == ProxmoxInventory.ObjectType.VM else "lxc"
    return not member_type or member_type == expected

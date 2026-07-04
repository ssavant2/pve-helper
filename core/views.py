from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta, timezone as dt_timezone
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Iterable, Iterator
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponse, HttpResponseForbidden, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone as tz
from django.utils.dateparse import parse_datetime
from django.utils.http import content_disposition_header, url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django_q.tasks import async_task

from .models import (
    AuditEvent,
    FileInventory,
    ProxmoxInventory,
    ScanRun,
    ScheduledAction,
    ScheduledActionRun,
    StorageMount,
    StorageSpaceSnapshot,
    TrashItem,
)
from .services.classification import extract_disk_references
from .services.file_actions import FileActionRisk, file_action_risk
from .services.audit_retention_schedule import audit_retention_schedule_state, update_audit_retention_schedule
from .services.filesystem import storage_space_info
from .services.guests import (
    guest_identity,
    guest_identity_from_inventory,
    guest_identity_from_scheduled_action,
    is_template,
    parse_guest_tags,
)
from .services.guest_storage import DISK_BUS_RE, guest_disks, guest_networks
from .services.guest_create import create_ct, create_options, create_vm
from .services.partial_scan import refresh_storage_directory
from .services.permissions import storage_permissions as get_permissions
from .services.proxmox import (
    LIVE_GUEST_INVENTORY_CACHE_SECONDS,
    LIVE_GUEST_STATUS_CACHE_SECONDS,
    ProxmoxAPIError,
    ProxmoxTaskTimeout,
    clear_live_guest_caches,
    configured_clients,
    fetch_live_guest_inventory,
    fetch_live_guest_status,
)
from .services.recent_tasks import recent_task_page, serialize_task_page
from .services.scan_schedule import scan_schedule_state, update_scan_schedule
from .services.scheduled_actions import ScheduledActionQueueError, queue_manual_scheduled_action_run
from .services.scheduled_recurrence import RecurrenceError, next_run_after
from .services.storage_actions import (
    INFLATE_PREALLOCATION_FULL,
    INFLATE_PREALLOCATION_METADATA,
    INFLATE_PREALLOCATION_MODES,
    MIN_INFLATE_ALLOCATED_PERCENT,
    StorageActionError,
    adopt_discovered_trash_items,
    cleanup_empty_app_trash_directories,
    create_storage_directory,
    full_inflate_already_recorded,
    is_nfs_silly_rename_path,
    validate_inflate_storage_file,
    move_storage_file,
    move_file_to_trash,
    purge_trash_item as purge_trash_item_action,
    rename_storage_file,
    restore_trash_item,
    upload_to_storage,
    upload_folder_to_storage,
)
from .services.storage_details import storage_details
from .services.storage_visibility import ignored_relative_paths_for_storage, is_ignored_storage_path
from .services.trash_schedule import trash_purge_schedule_state, update_trash_purge_schedule


SPACE_CHART_DAYS = 7
SPACE_CHART_BUCKET_HOURS = 12
SPACE_CHART_MAX_POINTS = 14
FILE_BROWSER_BATCH_SIZE = 200
AUDIT_PAGE_SIZE = 200
SCHEDULED_ACTION_WEEKDAYS = [
    ("0", "Monday"),
    ("1", "Tuesday"),
    ("2", "Wednesday"),
    ("3", "Thursday"),
    ("4", "Friday"),
    ("5", "Saturday"),
    ("6", "Sunday"),
]
SCHEDULED_ACTION_ORDINALS = [
    ("first", "First"),
    ("second", "Second"),
    ("third", "Third"),
    ("fourth", "Fourth"),
    ("fifth", "Fifth"),
    ("last", "Last"),
]
SCHEDULED_ACTION_MONTHS = [
    ("1", "Jan"),
    ("2", "Feb"),
    ("3", "Mar"),
    ("4", "Apr"),
    ("5", "May"),
    ("6", "Jun"),
    ("7", "Jul"),
    ("8", "Aug"),
    ("9", "Sep"),
    ("10", "Oct"),
    ("11", "Nov"),
    ("12", "Dec"),
]
SCHEDULED_ACTION_DEFAULT_MONTHS = [value for value, _label in SCHEDULED_ACTION_MONTHS]
SCHEDULED_ACTION_RECURRENCE_ONCE = "once"


def app_login_required(view_func):
    if not settings.APP_REQUIRE_LOGIN:
        return view_func
    return login_required(view_func)


def navigation_context(active: str, **extra: str) -> dict[str, str]:
    return {"active_nav": active, **extra}


def _storage_tab_context(storage: StorageMount, latest_scan, active_tab: str) -> dict:
    return {
        **navigation_context("storage_browser", active_storage_id=storage.storage_id),
        "storage": storage,
        "latest_scan": latest_scan,
        "active_scan": _active_scan(),
        "active_storage_tab": active_tab,
    }


@app_login_required
def dashboard(request):
    latest_scan = ScanRun.objects.order_by("-created_at").first()
    result_scan = _latest_result_scan()
    storages = list(StorageMount.objects.filter(enabled=True).order_by("display_name"))
    _decorate_storages_with_scan_state(storages, result_scan)
    classification_counts = _current_classification_counts(storages)
    context = {
        **navigation_context("dashboard"),
        "latest_scan": latest_scan,
        "result_scan": result_scan,
        "storage_count": StorageMount.objects.count(),
        "scan_count": ScanRun.objects.count(),
        "audit_count": AuditEvent.objects.count(),
        "classification_counts": classification_counts,
        "storage_gate_rows": _storage_gate_rows(storages, result_scan),
        "scan_schedule": scan_schedule_state(),
        "trash_purge_schedule": _trash_purge_schedule_state(),
        "active_scan": _active_scan(),
    }
    return render(request, "core/dashboard.html", context)


@app_login_required
def datastores(request):
    result_scan = _latest_result_scan()
    storages = list(StorageMount.objects.order_by("display_name"))
    _decorate_storages_with_scan_state(storages, result_scan)

    context = {
        **navigation_context("datastores"),
        "latest_scan": result_scan,
        "storages": storages,
    }
    return render(request, "core/datastores.html", context)


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
    payload = []
    for row in rows:
        if row.object_type != ProxmoxInventory.ObjectType.VM or not row.agent_enabled:
            continue
        detail = SimpleNamespace(
            object_type=row.object_type,
            vmid=row.vmid,
            name=row.name,
            node=row.node,
            status=row.status,
            config={"agent": 1},
        )
        summary = _guest_agent_summary(detail, allow_fetch=True)
        if not summary.get("running"):
            continue
        payload.append(
            {
                "target": row.target_id,
                "guest_os": summary.get("os_pretty_name") or summary.get("os_name") or "",
                "ip_label": ", ".join(summary.get("ips", [])[:3]) if summary.get("ips") else "",
                "agent": "Running",
            }
        )
    return JsonResponse({"guests": payload})


@app_login_required
def vms_overview_snapshot_info(request):
    rows, _live_available, _scan_at = _guest_rows()
    payload = []
    for row in rows:
        has_snapshot = _live_guest_has_snapshot(row)
        if has_snapshot is None:
            has_snapshot = row.has_snapshot
        payload.append(
            {
                "target": row.target_id,
                "has_snapshot": bool(has_snapshot),
                "has_snapshot_label": "Yes" if has_snapshot else "No",
            }
        )
    return JsonResponse({"guests": payload})


@app_login_required
def vms_status(request):
    statuses = fetch_live_guest_status()
    guests = [
        {
            "target": _guest_target_value(object_type, vmid, node),
            "status": status,
            "state_label": _guest_state_label(status),
            "health_label": "Normal" if status else "Unknown",
        }
        for (node, object_type, vmid), status in sorted(statuses.items(), key=lambda item: (item[0][1], item[0][2], item[0][0]))
    ]
    return JsonResponse(
        {
            "guests": guests,
            "live_available": bool(statuses),
            "cache_seconds": LIVE_GUEST_STATUS_CACHE_SECONDS,
        }
    )


@app_login_required
def guest_agent_summary_api(request, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid)
    summary = _guest_agent_summary(detail, allow_fetch=True)
    rows = []
    if summary.get("os_name"):
        rows.append({"label": "OS name", "value": summary["os_name"]})
    if summary.get("os_version"):
        version = str(summary["os_version"])
        if summary.get("os_version_id"):
            version = f"{version} ({summary['os_version_id']})"
        rows.append({"label": "Version", "value": version})
    if summary.get("architecture"):
        rows.append({"label": "Architecture", "value": summary["architecture"]})
    if summary.get("kernel_release"):
        rows.append({"label": "Kernel", "value": summary["kernel_release"]})
    if summary.get("hostname"):
        rows.append({"label": "DNS name", "value": summary["hostname"]})
    if summary.get("ips"):
        rows.append({"label": "IP addresses", "value": "\n".join(summary["ips"])})

    return JsonResponse(
        {
            "enabled": summary.get("enabled", False),
            "running": summary.get("running", False),
            "guest_status": detail.status,
            "os_label": summary.get("os_pretty_name") or summary.get("os_name") or _guest_os_label(detail.config),
            "rows": rows,
            "status_label": "Running" if summary.get("running") else "Not running",
        }
    )


def _vms_workspace_context(active_nav: str) -> dict:
    rows, live_available, scan_at = _guest_rows()
    return {
        **navigation_context(active_nav),
        "guests": rows,
        "guest_list": rows,
        "guest_count": len(rows),
        "running_count": sum(1 for row in rows if row.status == "running"),
        "live_available": live_available,
        "inventory_scan_at": scan_at,
        "live_inventory_cache_seconds": LIVE_GUEST_INVENTORY_CACHE_SECONDS,
        "live_status_cache_seconds": LIVE_GUEST_STATUS_CACHE_SECONDS,
        "guest_write_enabled": settings.VM_WRITE_ENABLED,
        "active_object_type": "",
        "active_vmid": None,
    }


def _guest_rows():
    """Cluster-wide guest rows: live membership/status/name joined with the
    latest scan for template flag and tags. Falls back to scan if the API is
    unreachable. Returns (rows, live_available, scan_timestamp)."""
    live_guests = fetch_live_guest_inventory()
    latest_scan = _latest_proxmox_inventory_scan()

    scan_by_key: dict[tuple[str, str, int], ProxmoxInventory] = {}
    scan_by_legacy_key: dict[tuple[str, int], list[ProxmoxInventory]] = {}
    if latest_scan:
        for obj in ProxmoxInventory.objects.filter(
            scan_run=latest_scan,
            object_type__in=[ProxmoxInventory.ObjectType.VM, ProxmoxInventory.ObjectType.CT],
            vmid__isnull=False,
        ):
            scan_by_key[(obj.node or "", obj.object_type, obj.vmid)] = obj
            scan_by_legacy_key.setdefault((obj.object_type, obj.vmid), []).append(obj)

    live_available = bool(live_guests)
    rows: list[SimpleNamespace] = []
    if live_available:
        live_status = fetch_live_guest_status()
        for guest in live_guests:
            status = _live_status_for(live_status, guest.node, guest.object_type, guest.vmid, guest.status)
            rows.append(
                _build_guest_row(
                    object_type=guest.object_type,
                    vmid=guest.vmid,
                    name=guest.name,
                    status=status,
                    node=guest.node,
                    scan_obj=_scan_obj_for_live_guest(
                        scan_by_key,
                        scan_by_legacy_key,
                        guest.node,
                        guest.object_type,
                        guest.vmid,
                    ),
                    live_guest=guest,
                )
            )
    else:
        for (_node, object_type, vmid), scan_obj in scan_by_key.items():
            rows.append(
                _build_guest_row(
                    object_type=object_type,
                    vmid=vmid,
                    name=scan_obj.name,
                    status=scan_obj.status,
                    node=scan_obj.node,
                    scan_obj=scan_obj,
                    live_guest=None,
                )
            )

    rows.sort(key=lambda row: ((row.name or "").casefold(), row.type_sort, row.vmid or 0, row.node))
    _decorate_guests_with_scheduled_actions(rows)
    return rows, live_available, _scan_timestamp(latest_scan)


def _scan_obj_for_live_guest(
    scan_by_key: dict[tuple[str, str, int], ProxmoxInventory],
    scan_by_legacy_key: dict[tuple[str, int], list[ProxmoxInventory]],
    node: str,
    object_type: str,
    vmid: int,
) -> ProxmoxInventory | None:
    exact = scan_by_key.get((node or "", object_type, vmid))
    if exact is not None:
        return exact
    legacy_matches = scan_by_legacy_key.get((object_type, vmid), [])
    if len(legacy_matches) == 1:
        return legacy_matches[0]
    return None


def _live_status_for(statuses: dict, node: str, object_type: str, vmid: int, default: str = "") -> str:
    return statuses.get((node or "", object_type, vmid), statuses.get((object_type, vmid), default))


def _guest_target_value(object_type: str, vmid: int | str | None, node: str = "") -> str:
    base = f"{object_type}:{vmid}"
    return f"{base}@{node}" if node else base


def _build_guest_row(*, object_type, vmid, name, status, node, scan_obj, live_guest=None) -> SimpleNamespace:
    config = scan_obj.config if scan_obj is not None and isinstance(scan_obj.config, dict) else {}
    template = object_type == ProxmoxInventory.ObjectType.VM and is_template(config)
    if template:
        type_label, type_filter, type_sort = "Template", "template", 0
    elif object_type == ProxmoxInventory.ObjectType.CT:
        type_label, type_filter, type_sort = "CT", "ct", 2
    else:
        type_label, type_filter, type_sort = "VM", "vm", 1
    cpu = _float_or_zero(getattr(live_guest, "cpu", 0.0))
    mem = _int_or_zero(getattr(live_guest, "mem", 0))
    maxmem = _int_or_zero(getattr(live_guest, "maxmem", 0)) or _config_mem_bytes(config)
    used_disk = _int_or_zero(getattr(live_guest, "disk", 0))
    provisioned_disk = _int_or_zero(getattr(live_guest, "maxdisk", 0)) or _config_disk_bytes(config)
    uptime = _int_or_zero(getattr(live_guest, "uptime", 0))
    cpus = _int_or_zero(config.get("vcpus")) or _cpu_count(config, object_type)
    macs = _config_mac_addresses(config)
    ips = _config_ip_addresses(config)
    storage_ids = _config_storage_ids(config)
    identity = guest_identity(object_type, vmid, name or "")
    return SimpleNamespace(
        object_type=object_type,
        vmid=vmid,
        name=name or "",
        guest_identity=identity,
        status=status or "",
        state_label=_guest_state_label(status),
        health_label="Normal" if status else "Unknown",
        node=node or "",
        is_template=template,
        type_label=type_label,
        type_filter=type_filter,
        type_sort=type_sort,
        target_id=_guest_target_value(object_type, vmid, node),
        tags=parse_guest_tags(config),
        in_scan=scan_obj is not None,
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
        has_snapshot=_config_has_snapshots(config),
        has_snapshot_label="Yes" if _config_has_snapshots(config) else "No",
    )


def _guest_state_label(status: str) -> str:
    if status == "running":
        return "Powered On"
    if status == "stopped":
        return "Powered Off"
    return (status or "-").title()


def _config_disk_count(config: dict) -> int:
    return len(
        [
            key
            for key, value in (config or {}).items()
            if _is_disk_device_key(key) and isinstance(value, str) and "media=cdrom" not in value
        ]
    )


def _config_has_snapshots(config: dict) -> bool:
    snapshots = (config or {}).get("snapshots")
    if isinstance(snapshots, dict):
        return bool(snapshots)
    if isinstance(snapshots, list):
        return any(snapshot for snapshot in snapshots if snapshot)
    return False


def _live_guest_has_snapshot(row: SimpleNamespace) -> bool | None:
    if not row.node or row.vmid is None:
        return None
    kind = "qemu" if row.object_type == ProxmoxInventory.ObjectType.VM else "lxc"
    path = f"nodes/{quote(row.node, safe='')}/{kind}/{row.vmid}/snapshot"
    for client in configured_clients():
        try:
            data = client.get(path, timeout=2)
        except ProxmoxAPIError:
            continue
        if not isinstance(data, list):
            return None
        return any(
            isinstance(snapshot, dict) and str(snapshot.get("name") or "") not in {"", "current"}
            for snapshot in data
        )
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


def _config_ip_addresses(config: dict) -> list[str]:
    ips: list[str] = []
    for key, value in (config or {}).items():
        if not re.match(r"^ipconfig\d+$", key) or not isinstance(value, str):
            continue
        for token in value.split(","):
            name, sep, val = token.partition("=")
            if sep and name in {"ip", "ip6"} and val and val not in {"dhcp", "auto"}:
                ips.append(val)
    return ips


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


def _is_editable_disk_key(key: str) -> bool:
    return _is_disk_device_key(key)


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


GUEST_OBJECT_TYPES = {"vm": ProxmoxInventory.ObjectType.VM, "ct": ProxmoxInventory.ObjectType.CT}


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
    context.update(
        {
            "guest_os_label": _guest_os_label(config),
            "guest_agent_summary": _guest_agent_summary(detail, allow_fetch=False),
            "guest_usage": _guest_usage(current, config, detail.object_type),
            "guest_cpu_topology": _guest_cpu_topology(config, detail.object_type),
            "related_storages": related_storages,
            "related_networks": related_networks,
            "vm_details": _guest_vm_details(detail),
            "guest_cpu_label": _guest_cpu_label(config, detail.object_type),
            "guest_memory_label": f"{config.get('memory')} MB" if config.get("memory") else "",
            "guest_disks": disks,
            "guest_cdroms": cdroms,
            "guest_nets": nets,
            "guest_notes": config.get("description") or "",
            "guest_current": current,
            "guest_config": config,
        }
    )
    return render(request, "core/guest_summary.html", context)


GUEST_EDIT_FIELDS = ("name", "description", "onboot")
NET_KEY_RE = re.compile(r"^net\d+$")
ADVANCED_DEVICE_RE = re.compile(r"^(efidisk0|tpmstate0|rng0|audio0|serial\d+|usb\d+|hostpci\d+|virtiofs\d+)$")
HOTPLUG_DEFAULT = "disk,network,usb"
HOTPLUG_OPTIONS = (
    ("disk", "Disk"),
    ("network", "Network"),
    ("usb", "USB"),
    ("memory", "Memory"),
    ("cpu", "CPU"),
)
CPU_TYPE_OPTIONS = (
    ("", "Default"),
    ("host", "host"),
    ("max", "max"),
    ("x86-64-v2-AES", "x86-64-v2-AES"),
    ("x86-64-v3", "x86-64-v3"),
    ("x86-64-v4", "x86-64-v4"),
    ("kvm64", "kvm64"),
    ("qemu64", "qemu64"),
)


def _parse_net_value(value: str) -> dict:
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


def _next_device_index(config: dict, prefix: str, extra_keys: Iterable[str] | None = None) -> int:
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
    if object_type != ProxmoxInventory.ObjectType.VM:
        # CT hardware model differs; use the simple field editor for now.
        return redirect(f"{reverse('core:guest_edit', args=[object_type, vmid])}?section=hardware")
    if not settings.VM_WRITE_ENABLED:
        messages.error(request, "VM editing is disabled (VM_WRITE_ENABLED is off).")
        return redirect("core:guest_summary", object_type=object_type, vmid=vmid)

    detail = _resolve_guest_detail(object_type, vmid)
    if not detail.found:
        raise Http404("Guest not found")

    if request.method == "POST":
        error = _apply_hardware_edit(request, detail)
        if error is None:
            return redirect("core:guest_summary", object_type=object_type, vmid=vmid)
        messages.error(request, error)

    config = detail.config
    disks, cdroms = guest_disks(config, detail.node, detail.vmid)
    disks = [disk for disk in disks if _is_editable_disk_key(disk["label"])]
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


def _config_enabled(config: dict, key: str, *, default: bool = False) -> bool:
    if key not in config:
        return default
    value = str(config.get(key) or "").strip().lower()
    if not value:
        return False
    return value in {"1", "true", "yes", "on"} or value.startswith("1,")


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


def _parse_startup_options(value: object) -> dict[str, str]:
    parsed = {"order": "", "up": "", "down": ""}
    for part in str(value or "").split(","):
        key, separator, raw = part.partition("=")
        if separator and key in parsed:
            parsed[key] = raw
    return parsed


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


def _apply_hardware_edit(request, detail: SimpleNamespace):
    node = detail.node
    if not node:
        return "Could not resolve the guest's current node."
    client = None
    fresh: dict = {}
    for candidate in configured_clients():
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

    for key in [k for k in fresh if _is_editable_disk_key(k) and "media=cdrom" not in str(fresh[k])]:
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
    if cd_key:
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
            return "Proxmox denied the change (403) - the token lacks a required VM.Config.* privilege."
        return f"Proxmox rejected the change: {exc}"

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
    if not settings.VM_WRITE_ENABLED:
        messages.error(request, "VM/CT editing is disabled (VM_WRITE_ENABLED is off).")
        return redirect("core:guest_summary", object_type=object_type, vmid=vmid)

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
        messages.error(request, result)
        form_values = {
            "name": request.POST.get("name", ""),
            "description": request.POST.get("description", ""),
            "onboot": request.POST.get("onboot") == "on",
            "tags": request.POST.get("tags", ""),
            "cores": request.POST.get("cores", ""),
            "sockets": request.POST.get("sockets", ""),
            "memory": request.POST.get("memory", ""),
            "swap": request.POST.get("swap", ""),
        }
    else:
        form_values = {
            "name": str(config.get(name_key, "") or ""),
            "description": str(config.get("description", "") or ""),
            "onboot": str(config.get("onboot", "0")) in ("1", "True", "true"),
            "tags": " ".join(parse_guest_tags(config)),
            "cores": str(config.get("cores", "") or ""),
            "sockets": str(config.get("sockets", "") or ""),
            "memory": str(config.get("memory", "") or ""),
            "swap": str(config.get("swap", "") or ""),
        }

    context = {
        **navigation_context("vms"),
        "guest": detail,
        "guest_identity": guest_identity(object_type, vmid, detail.name),
        "name_key_label": "Name" if object_type == ProxmoxInventory.ObjectType.VM else "Hostname",
        "section": section,
        "is_vm": object_type == ProxmoxInventory.ObjectType.VM,
        "form_values": form_values,
    }
    return render(request, "core/guest_edit.html", context)


def _apply_guest_edit(request, detail: SimpleNamespace, name_key: str):
    node = detail.node
    if not node:
        return "Could not resolve the guest's current node."

    client = None
    fresh: dict = {}
    for candidate in configured_clients():
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
        new_tags = ";".join(t for t in re.split(r"[;,\s]+", request.POST.get("tags", "").strip()) if t)
        cur_tags = ";".join(t for t in re.split(r"[;,\s]+", str(fresh.get("tags", "") or "").strip()) if t)
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

    if not changed:
        return "No changes to save."

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
            return "Proxmox denied the change (403) - the API token lacks the required VM.Config.* privilege."
        return f"Proxmox rejected the change: {exc}"

    AuditEvent.objects.create(
        user=request.user if request.user.is_authenticated else None,
        username=request.user.get_username() if request.user.is_authenticated else "system",
        action="guest.config.updated",
        object_type="guest",
        object_id=f"{detail.object_type}:{detail.vmid}",
        outcome="success",
        details={"fields": changed, "node": node, "vmid": detail.vmid, "target_type": detail.object_type, "name": detail.name},
    )
    return True


CONFIG_SECTIONS = [
    ("General", ["name", "hostname", "ostype", "arch", "bios", "machine", "boot", "onboot", "startup", "agent", "tablet", "protection", "hotplug"]),
    ("Processors", ["cores", "sockets", "vcpus", "cpu", "numa", "cpuunits", "cpulimit", "affinity"]),
    ("Memory", ["memory", "balloon", "shares", "swap"]),
]
CONFIG_HIDE = {"digest", "description", "tags", "meta", "smbios1", "vmgenid"}


def _row_value(value) -> dict:
    return {"value": value, "lines": []}


def _row_lines(lines: list[str]) -> dict:
    return {"value": "\n".join(lines), "lines": [line for line in lines if line]}


def _agent_ips_by_mac(agent_summary: dict) -> dict[str, list[str]]:
    by_mac: dict[str, list[str]] = {}
    for interface in agent_summary.get("interfaces") or []:
        if not isinstance(interface, dict):
            continue
        mac = str(interface.get("mac") or "").lower()
        addresses = [str(ip) for ip in interface.get("addresses") or [] if ip]
        if mac and addresses:
            by_mac[mac] = addresses
    return by_mac


def _with_network_ip_addresses(nets: list[dict], config_ips: list[str], agent_summary: dict) -> list[dict]:
    ips_by_mac = _agent_ips_by_mac(agent_summary)
    enriched = []
    for net in nets:
        addresses = ips_by_mac.get(str(net.get("mac") or "").lower(), [])
        enriched.append({**net, "ip_addresses": addresses, "ip_label": ", ".join(addresses) if addresses else "-"})
    if config_ips and len(enriched) == 1 and not enriched[0]["ip_addresses"]:
        enriched[0]["ip_addresses"] = config_ips
        enriched[0]["ip_label"] = ", ".join(config_ips)
    return enriched


def _network_config_lines(net: dict) -> list[str]:
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


def _guest_config_sections(config: dict, *, agent_summary: dict | None = None) -> list[dict]:
    shown: set[str] = set()
    sections: list[dict] = []
    for title, keys in CONFIG_SECTIONS:
        rows = [{"key": key, **_row_value(config[key])} for key in keys if key in config]
        for row in rows:
            shown.add(row["key"])
        if rows:
            sections.append({"title": title, "rows": rows})

    disk_rows = [{"key": key, **_row_value(config[key])} for key in sorted(config) if DISK_BUS_RE.match(key)]
    shown.update(row["key"] for row in disk_rows)
    if disk_rows:
        sections.append({"title": "Disks", "rows": disk_rows})

    config_ips = _config_ip_addresses(config)
    nets = _with_network_ip_addresses(guest_networks(config), config_ips, agent_summary or {})
    nets_by_label = {net["label"]: net for net in nets}
    net_rows = []
    for key in sorted(config):
        if not re.match(r"^net\d+$", key):
            continue
        net = nets_by_label.get(key)
        net_rows.append({"key": key, **(_row_lines(_network_config_lines(net)) if net else _row_value(config[key]))})
    shown.update(row["key"] for row in net_rows)
    if net_rows:
        sections.append({"title": "Network", "rows": net_rows})

    other = []
    for key in sorted(config):
        if key in shown or key in CONFIG_HIDE:
            continue
        if key == "parent":
            other.append({"key": "Parent snapshot", **_row_value(f"Snapshot: {config[key]}")})
        else:
            other.append({"key": key, **_row_value(config[key])})
    if other:
        sections.append({"title": "Options", "rows": other})
    return sections


def _fmt_bytes(value: float) -> str:
    number = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if number < 1024 or unit == "TiB":
            return f"{int(number)} B" if unit == "B" else f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} TiB"


def _rrd_chart(points, keys, *, to_value, fmt, axis_max=None, width=340, height=90):
    """Build an auto-scaled SVG chart (line + area) for one or more rrddata
    series. fmt is 'pct' | 'bytes' | 'rate'."""
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

    def _axis_label(value: float) -> str:
        if fmt == "pct":
            return f"{value:.0f}%" if value >= 10 or value == 0 else f"{value:.1f}%"
        if fmt == "rate":
            return _fmt_bytes(value) + "/s"
        return _fmt_bytes(value)

    ticks = []
    for fraction in (1.0, 0.75, 0.5, 0.25, 0.0):
        ticks.append(
            {
                "y": round(height - (fraction * height), 1),
                "label": _axis_label(axis * fraction),
            }
        )
    return {"series": series, "axis_max_label": _axis_label(axis), "ticks": ticks, "width": width, "height": height}


@app_login_required
def guest_configure(request, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid)
    agent_summary = _guest_agent_summary(detail, allow_fetch=True)
    actions = list(
        ScheduledAction.objects.filter(target_type=object_type, target_vmid=vmid).order_by("-enabled", "next_run_at", "name")
    )
    for action in actions:
        action.display_schedule = _scheduled_action_schedule_label(action)
        action.display_status_class = _scheduled_action_status_class(action.last_status)
    context = _guest_tab_context(detail, "configure")
    context["config_sections"] = _guest_config_sections(detail.config, agent_summary=agent_summary)
    context["scheduled_actions"] = actions
    context["scheduled_task_create_url"] = (
        f"{reverse('core:scheduled_task_create')}?{urlencode({'target': f'{object_type}:{vmid}'})}"
    )
    return render(request, "core/guest_configure.html", context)


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
def guest_snapshots(request, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid)
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
    return render(request, "core/guest_snapshots.html", context)


def _guest_snapshot_entries(detail: SimpleNamespace) -> tuple[list[dict], str]:
    data, error = _guest_api_get(detail, "snapshot")
    entries = []
    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            snaptime = entry.get("snaptime")
            entries.append(
                {
                    "name": entry.get("name", ""),
                    "description": entry.get("description", ""),
                    "parent": entry.get("parent", "") or "",
                    "snaptime": datetime.fromtimestamp(int(snaptime), dt_timezone.utc) if snaptime else None,
                    "vmstate": bool(entry.get("vmstate")),
                    "is_current": entry.get("name") == "current",
                }
            )
    return entries, error or ""


def _ordered_snapshot_entries(entries: list[dict]) -> list[dict]:
    # Build the snapshot tree from the parent links and flatten it depth-first.
    by_name = {item["name"]: item for item in entries}
    children: dict[str, list] = {}
    roots = []
    for item in entries:
        parent = item["parent"]
        if parent and parent in by_name:
            children.setdefault(parent, []).append(item)
        else:
            roots.append(item)

    def _sort_key(item):
        if item["is_current"]:
            return datetime.max.replace(tzinfo=dt_timezone.utc)
        return item["snaptime"] or datetime.min.replace(tzinfo=dt_timezone.utc)

    ordered = []

    def _walk(node, depth):
        ordered.append({**node, "depth": depth, "indent": depth * 22})
        for child in sorted(children.get(node["name"], []), key=_sort_key):
            _walk(child, depth + 1)

    for root in sorted(roots, key=_sort_key):
        _walk(root, 0)
    return ordered


def _delete_all_guest_snapshots(detail: SimpleNamespace) -> tuple[int, str]:
    entries, error = _guest_snapshot_entries(detail)
    if error:
        return 0, error
    snapshots = [snap for snap in _ordered_snapshot_entries(entries) if not snap["is_current"] and snap.get("name")]
    deleted = 0
    for snap in reversed(snapshots):
        _data, err = _guest_delete_wait_task(detail, f"snapshot/{quote(snap['name'], safe='')}")
        if err:
            return deleted, err
        deleted += 1
    return deleted, ""


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
            "timeframes": ["hour", "day", "week", "month"],
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
    for client in configured_clients():
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


@app_login_required
def guest_backup(request, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid)
    backups = []
    backup_storages = []
    error = ""
    node = detail.node
    if node:
        client = configured_clients()[0] if configured_clients() else None
        if client:
            try:
                storages = client.get(f"nodes/{quote(node, safe='')}/storage")
            except ProxmoxAPIError as exc:
                storages, error = [], str(exc)
            for storage in storages if isinstance(storages, list) else []:
                if "backup" not in str(storage.get("content", "")).split(","):
                    continue
                sid = storage.get("storage")
                backup_storages.append(sid)
                try:
                    content = client.get(
                        f"nodes/{quote(node, safe='')}/storage/{quote(sid, safe='')}/content?content=backup&vmid={vmid}"
                    )
                except ProxmoxAPIError:
                    continue
                for entry in content if isinstance(content, list) else []:
                    backups.append(
                        {
                            "volid": entry.get("volid", ""),
                            "size": entry.get("size"),
                            "ctime": datetime.fromtimestamp(int(entry["ctime"]), dt_timezone.utc) if entry.get("ctime") else None,
                            "notes": entry.get("notes", ""),
                            "storage": sid,
                        }
                    )
    backups.sort(key=lambda item: item["ctime"] or datetime.min.replace(tzinfo=dt_timezone.utc), reverse=True)

    jobs = []
    try:
        raw_jobs = configured_clients()[0].get("cluster/backup") if configured_clients() else []
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
    return render(request, "core/guest_backup.html", context)


def _backup_job_covers(job: dict, vmid: int) -> bool:
    if str(job.get("all", "0")) in ("1", "True", "true"):
        return True
    vmids = str(job.get("vmid", ""))
    return str(vmid) in [v.strip() for v in vmids.split(",") if v.strip()]


@app_login_required
def guest_replication(request, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid)
    jobs = []
    error = ""
    try:
        raw = configured_clients()[0].get("cluster/replication") if configured_clients() else []
        for job in raw if isinstance(raw, list) else []:
            if str(job.get("guest", "")) == str(vmid) or str(job.get("id", "")).startswith(f"{vmid}-"):
                jobs.append(
                    {
                        "id": job.get("id", ""),
                        "target": job.get("target", ""),
                        "schedule": job.get("schedule", ""),
                        "rate": job.get("rate", ""),
                        "disabled": str(job.get("disable", "0")) in ("1", "True", "true"),
                        "comment": job.get("comment", ""),
                    }
                )
    except ProxmoxAPIError as exc:
        error = str(exc)
    target_nodes = []
    if configured_clients():
        target_nodes = [n for n in configured_clients()[0].node_names(fallback="") if n != detail.node]
    context = _guest_tab_context(detail, "replication")
    context.update({"replication_jobs": jobs, "replication_error": error, "target_nodes": target_nodes})
    return render(request, "core/guest_replication.html", context)


@app_login_required
def guest_firewall(request, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid)
    opts, opts_err = _guest_api_get(detail, "firewall/options")
    rules, rules_err = _guest_api_get(detail, "firewall/rules")
    option_rows = []
    if isinstance(opts, dict):
        for key in ("enable", "dhcp", "macfilter", "ndp", "ipfilter", "policy_in", "policy_out", "log_level_in", "log_level_out"):
            if key in opts:
                option_rows.append({"label": key, "value": opts[key]})
    rule_list = []
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            rule_list.append(
                {
                    "pos": rule.get("pos"),
                    "type": rule.get("type", ""),
                    "action": rule.get("action", ""),
                    "enable": str(rule.get("enable", "1")) in ("1", "True", "true"),
                    "source": rule.get("source", ""),
                    "dest": rule.get("dest", ""),
                    "proto": rule.get("proto", ""),
                    "dport": rule.get("dport", ""),
                    "comment": rule.get("comment", ""),
                }
            )
    opts_dict = opts if isinstance(opts, dict) else {}
    context = _guest_tab_context(detail, "firewall")
    context.update(
        {
            "fw_enabled": bool(opts_dict.get("enable")),
            "fw_options": option_rows,
            "fw_policy_in": opts_dict.get("policy_in", "DROP"),
            "fw_policy_out": opts_dict.get("policy_out", "ACCEPT"),
            "fw_rules": rule_list,
            "fw_error": opts_err or rules_err or "",
        }
    )
    return render(request, "core/guest_firewall.html", context)


def _require_guest(object_type: str, vmid: int, *, node: str = "") -> SimpleNamespace:
    if object_type not in GUEST_OBJECT_TYPES:
        raise Http404("Unknown guest type")
    detail = _resolve_guest_detail(object_type, vmid, node=node)
    if not detail.found:
        raise Http404("Guest not found")
    return detail


def _guest_kind(detail: SimpleNamespace) -> str:
    return "qemu" if detail.object_type == ProxmoxInventory.ObjectType.VM else "lxc"


def _guest_post_with_client(detail: SimpleNamespace, subpath: str, data: dict | None = None):
    if not detail.node:
        return None, "The guest's node could not be resolved.", None
    err = "No Proxmox endpoint could reach this guest."
    for client in configured_clients():
        try:
            return client.post(
                f"nodes/{quote(detail.node, safe='')}/{_guest_kind(detail)}/{detail.vmid}/{subpath}",
                data=data or {},
            ), None, client
        except ProxmoxAPIError as exc:
            err = str(exc)
    return None, err, None


def _guest_post(detail: SimpleNamespace, subpath: str, data: dict | None = None):
    response, err, _client = _guest_post_with_client(detail, subpath, data)
    return response, err


def _guest_delete_with_client(detail: SimpleNamespace, subpath: str):
    if not detail.node:
        return None, "The guest's node could not be resolved.", None
    err = "No Proxmox endpoint could reach this guest."
    for client in configured_clients():
        try:
            return client.delete(
                f"nodes/{quote(detail.node, safe='')}/{_guest_kind(detail)}/{detail.vmid}/{subpath}"
            ), None, client
        except ProxmoxAPIError as exc:
            err = str(exc)
    return None, err, None


def _guest_delete(detail: SimpleNamespace, subpath: str):
    response, err, _client = _guest_delete_with_client(detail, subpath)
    return response, err


SNAPSHOT_TASK_WAIT_SECONDS = 60


def _guest_post_wait_task(detail: SimpleNamespace, subpath: str, data: dict | None = None, *, timeout_seconds: int = SNAPSHOT_TASK_WAIT_SECONDS):
    if not detail.node:
        return None, "The guest's node could not be resolved."
    err = "No Proxmox endpoint could reach this guest."
    for client in configured_clients():
        try:
            response = client.post(
                f"nodes/{quote(detail.node, safe='')}/{_guest_kind(detail)}/{detail.vmid}/{subpath}",
                data=data or {},
            )
            return response, _wait_for_proxmox_task_if_returned(client, detail.node, response, timeout_seconds=timeout_seconds)
        except ProxmoxAPIError as exc:
            err = str(exc)
    return None, err


def _guest_delete_wait_task(detail: SimpleNamespace, subpath: str, *, timeout_seconds: int = SNAPSHOT_TASK_WAIT_SECONDS):
    if not detail.node:
        return None, "The guest's node could not be resolved."
    err = "No Proxmox endpoint could reach this guest."
    for client in configured_clients():
        try:
            response = client.delete(
                f"nodes/{quote(detail.node, safe='')}/{_guest_kind(detail)}/{detail.vmid}/{subpath}"
            )
            return response, _wait_for_proxmox_task_if_returned(client, detail.node, response, timeout_seconds=timeout_seconds)
        except ProxmoxAPIError as exc:
            err = str(exc)
    return None, err


def _wait_for_proxmox_task_if_returned(client, node: str, response, *, timeout_seconds: int) -> str:
    if not (isinstance(response, str) and response.startswith("UPID:")):
        return ""
    if not hasattr(client, "wait_for_task"):
        return ""
    try:
        result = client.wait_for_task(node=node, upid=response, timeout_seconds=timeout_seconds)
    except ProxmoxTaskTimeout as exc:
        return str(exc)
    if not result.success:
        return f"Proxmox task exitstatus: {result.exitstatus or result.status or 'unknown'}"
    return ""


def _guest_destroy_with_client(detail: SimpleNamespace, query: str):
    if not detail.node:
        return None, "The guest's node could not be resolved.", None
    err = "No Proxmox endpoint could reach this guest."
    path = f"nodes/{quote(detail.node, safe='')}/{_guest_kind(detail)}/{detail.vmid}"
    if query:
        path = f"{path}?{query}"
    for client in configured_clients():
        try:
            return client.delete(path), None, client
        except ProxmoxAPIError as exc:
            err = str(exc)
    return None, err, None


def _guest_destroy(detail: SimpleNamespace, query: str):
    response, err, _client = _guest_destroy_with_client(detail, query)
    return response, err


def _guest_put(detail: SimpleNamespace, subpath: str, data: dict | None = None):
    if not detail.node:
        return None, "The guest's node could not be resolved."
    err = "No Proxmox endpoint could reach this guest."
    for client in configured_clients():
        try:
            return client.put(
                f"nodes/{quote(detail.node, safe='')}/{_guest_kind(detail)}/{detail.vmid}/{subpath}",
                data=data or {},
            ), None
        except ProxmoxAPIError as exc:
            err = str(exc)
    return None, err


def _write_result(request, detail, redirect_name, tab_key, err, ok_msg, audit_action, audit_details=None):
    if err:
        if "403" in err:
            messages.error(request, "Proxmox denied the change (403): the token lacks the required privilege.")
        else:
            messages.error(request, f"Failed: {err}")
    else:
        _audit_guest(request, detail, audit_action, audit_details)
    return redirect(redirect_name, object_type=detail.object_type, vmid=detail.vmid)


@require_POST
@app_login_required
def guest_firewall_options(request, object_type, vmid):
    detail = _require_guest(object_type, vmid)
    data = {"enable": "1" if request.POST.get("enable") == "on" else "0"}
    for key in ("policy_in", "policy_out"):
        val = request.POST.get(key, "").strip()
        if val:
            data[key] = val
    _d, err = _guest_put(detail, "firewall/options", data)
    return _write_result(request, detail, "core:guest_firewall", "firewall", err, "Firewall options updated.", "guest.firewall.options")


@require_POST
@app_login_required
def guest_firewall_rule_add(request, object_type, vmid):
    detail = _require_guest(object_type, vmid)
    data = {
        "type": request.POST.get("type", "in"),
        "action": request.POST.get("action", "ACCEPT"),
        "enable": "1",
    }
    for key in ("source", "dest", "proto", "dport", "sport", "comment", "macro"):
        val = request.POST.get(key, "").strip()
        if val:
            data[key] = val
    _d, err = _guest_post(detail, "firewall/rules", data)
    return _write_result(request, detail, "core:guest_firewall", "firewall", err, "Firewall rule added.", "guest.firewall.rule_add")


@require_POST
@app_login_required
def guest_firewall_rule_delete(request, object_type, vmid, pos):
    detail = _require_guest(object_type, vmid)
    _d, err = _guest_delete(detail, f"firewall/rules/{pos}")
    return _write_result(request, detail, "core:guest_firewall", "firewall", err, "Firewall rule deleted.", "guest.firewall.rule_delete", {"pos": pos})


@require_POST
@app_login_required
def guest_firewall_rule_toggle(request, object_type, vmid, pos):
    detail = _require_guest(object_type, vmid)
    enable = "1" if request.POST.get("enable") == "1" else "0"
    _d, err = _guest_put(detail, f"firewall/rules/{pos}", {"enable": enable})
    return _write_result(request, detail, "core:guest_firewall", "firewall", err, "Firewall rule updated.", "guest.firewall.rule_toggle", {"pos": pos, "enable": enable})


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
        client = configured_clients()[0]
        fresh = client.guest_config(node=node, object_type=object_type, vmid=vmid)
        # only delete keys that currently exist
        delete = [k for k in delete if k in fresh]
        client.set_guest_config(node=node, object_type=object_type, vmid=vmid, updates=updates, delete=delete, digest=fresh.get("digest"))
    except (ProxmoxAPIError, IndexError) as exc:
        err = str(exc)
    return _write_result(request, detail, "core:guest_cloudinit", "cloudinit", err, "Cloud-Init updated.", "guest.cloudinit.update")


@require_POST
@app_login_required
def guest_backup_now(request, object_type, vmid):
    detail = _require_guest(object_type, vmid)
    storage = request.POST.get("storage", "").strip()
    if not storage:
        messages.error(request, "Select a backup storage.")
        return redirect("core:guest_backup", object_type=object_type, vmid=vmid)
    body = {
        "vmid": vmid,
        "storage": storage,
        "mode": request.POST.get("mode", "snapshot"),
        "compress": request.POST.get("compress", "zstd"),
        "remove": "0",
    }
    err = ""
    for client in configured_clients():
        try:
            client.post(f"nodes/{quote(detail.node, safe='')}/vzdump", data=body)
            err = ""
            break
        except ProxmoxAPIError as exc:
            err = str(exc)
    return _write_result(request, detail, "core:guest_backup", "backup", err, "Backup started.", "guest.backup.run", {"storage": storage})


@require_POST
@app_login_required
def guest_backup_delete(request, object_type, vmid):
    detail = _require_guest(object_type, vmid)
    volid = request.POST.get("volid", "").strip()
    storage = request.POST.get("storage", "").strip()
    if not volid or not storage:
        messages.error(request, "Missing backup reference.")
        return redirect("core:guest_backup", object_type=object_type, vmid=vmid)
    err = ""
    for client in configured_clients():
        try:
            client.delete(f"nodes/{quote(detail.node, safe='')}/storage/{quote(storage, safe='')}/content/{quote(volid, safe='')}")
            err = ""
            break
        except ProxmoxAPIError as exc:
            err = str(exc)
    return _write_result(request, detail, "core:guest_backup", "backup", err, "Backup archive deleted.", "guest.backup.delete", {"volid": volid})


@require_POST
@app_login_required
def guest_replication_create(request, object_type, vmid):
    detail = _require_guest(object_type, vmid)
    target = request.POST.get("target", "").strip()
    if not target:
        messages.error(request, "Select a target node.")
        return redirect("core:guest_replication", object_type=object_type, vmid=vmid)
    body = {"id": f"{vmid}-0", "type": "local", "target": target}
    schedule = request.POST.get("schedule", "").strip()
    if schedule:
        body["schedule"] = schedule
    err = ""
    for client in configured_clients():
        try:
            client.post("cluster/replication", data=body)
            err = ""
            break
        except ProxmoxAPIError as exc:
            err = str(exc)
    return _write_result(request, detail, "core:guest_replication", "replication", err, "Replication job created.", "guest.replication.create", {"target": target})


@require_POST
@app_login_required
def guest_replication_delete(request, object_type, vmid):
    detail = _require_guest(object_type, vmid)
    job_id = request.POST.get("job_id", "").strip()
    if not job_id:
        messages.error(request, "Missing job id.")
        return redirect("core:guest_replication", object_type=object_type, vmid=vmid)
    err = ""
    for client in configured_clients():
        try:
            client.delete(f"cluster/replication/{quote(job_id, safe='')}")
            err = ""
            break
        except ProxmoxAPIError as exc:
            err = str(exc)
    return _write_result(request, detail, "core:guest_replication", "replication", err, "Replication job deleted.", "guest.replication.delete", {"job_id": job_id})


def _audit_guest(request, detail: SimpleNamespace, action: str, details: dict | None = None, *, outcome: str = "success") -> AuditEvent:
    return AuditEvent.objects.create(
        user=request.user if request.user.is_authenticated else None,
        username=request.user.get_username() if request.user.is_authenticated else "system",
        action=action,
        object_type="guest",
        object_id=f"{detail.object_type}:{detail.vmid}",
        outcome=outcome,
        details={"node": detail.node, "vmid": detail.vmid, "target_type": detail.object_type, "name": detail.name, **(details or {})},
    )


def _audit_guest_task_or_success(
    request,
    detail: SimpleNamespace,
    audit_action: str,
    response,
    client,
    audit_details: dict | None = None,
    *,
    timeout_seconds: int | None = None,
) -> AuditEvent:
    details = dict(audit_details or {})
    if isinstance(response, str) and response.startswith("UPID:") and client is not None:
        details.update(
            {
                "proxmox_task_upid": response,
                "proxmox_task_node": detail.node,
                "proxmox_endpoint": getattr(client, "endpoint", ""),
            }
        )
        event = _audit_guest(request, detail, audit_action, details, outcome="running")
        task_id = async_task(
            "core.tasks.poll_guest_audit_task",
            event.id,
            getattr(client, "endpoint", ""),
            detail.node,
            response,
            timeout_seconds or settings.SCHEDULED_ACTION_TIMEOUT_SECONDS,
        )
        event.details = {**event.details, "poll_task_id": task_id}
        event.save(update_fields=["details"])
        return event
    return _audit_guest(request, detail, audit_action, details)


GUEST_POWER_ACTIONS = {"start", "shutdown", "reboot", "stop", "reset"}
VM_BULK_ACTIONS = {*GUEST_POWER_ACTIONS, "snapshot", "delete_snapshots", "template", "clone", "tags", "destroy"}


@require_POST
@app_login_required
def guest_power(request, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid)
    action = request.POST.get("action", "")
    if action not in GUEST_POWER_ACTIONS:
        messages.error(request, "Unknown power action.")
        return redirect("core:guest_summary", object_type=object_type, vmid=vmid)
    data, err, client = _guest_post_with_client(detail, f"status/{action}")
    if err:
        if "403" in err:
            messages.error(request, "Proxmox denied the power action (403) - the token needs VM.PowerMgmt.")
        else:
            messages.error(request, f"Power action failed: {err}")
    else:
        _audit_guest_task_or_success(request, detail, f"guest.power.{action}", data, client)
        clear_live_guest_caches()
    return redirect("core:guest_summary", object_type=object_type, vmid=vmid)


@require_POST
@app_login_required
def vms_bulk_action(request):
    if not settings.VM_WRITE_ENABLED:
        return redirect("core:vms_overview")

    action = request.POST.get("bulk_action", "").strip()
    targets = request.POST.getlist("guest")
    if action not in VM_BULK_ACTIONS:
        return redirect("core:vms_overview")
    if not targets:
        return redirect("core:vms_overview")

    snapshot_name = request.POST.get("snapshot_name", "").strip()
    if action == "snapshot" and not snapshot_name:
        return redirect("core:vms_overview")
    if action == "clone" and len(targets) != 1:
        return redirect("core:vms_overview")
    if action == "destroy" and len(targets) != 1:
        return redirect("core:vms_overview")
    if action == "tags" and request.POST.get("tags_mode", "").strip() not in {"add", "remove", "replace"}:
        return redirect("core:vms_overview")

    for target in targets:
        object_type, vmid, target_node = _parse_guest_target_value(target)
        if not object_type or vmid is None:
            continue
        try:
            detail = _require_guest(object_type, vmid, node=target_node)
        except Http404:
            continue

        response = None
        client = None
        if action == "snapshot":
            _data, err = _guest_post_wait_task(detail, "snapshot", {"snapname": snapshot_name})
            audit_action = "guest.snapshot.create"
            audit_details = {"snapshot": snapshot_name}
            error_label = _snapshot_error(err) if err else ""
        elif action == "delete_snapshots":
            deleted, err = _delete_all_guest_snapshots(detail)
            audit_action = "guest.snapshot.delete_all"
            audit_details = {"deleted": deleted}
            error_label = _snapshot_error(err) if err else ""
        elif action == "clone":
            err, audit_details, response, client = _clone_guest_from_bulk_request(request, detail)
            audit_action = "guest.clone.create"
            error_label = f"Clone failed: {err}" if err else ""
        elif action == "tags":
            err, audit_details = _update_guest_tags_from_bulk_request(request, detail)
            audit_action = "guest.tags.updated"
            error_label = f"Tag update failed: {err}" if err else ""
            response = None
            client = None
        elif action == "destroy":
            err, audit_details, response, client = _destroy_guest_from_bulk_request(request, detail)
            audit_action = "guest.destroy"
            error_label = f"Destroy failed: {err}" if err else ""
        elif action == "template":
            if object_type != ProxmoxInventory.ObjectType.VM:
                _audit_guest(
                    request,
                    detail,
                    "guest.template.convert",
                    {"error": "Only VMs can be converted to templates."},
                    outcome="failed",
                )
                continue
            response, err, client = _guest_post_with_client(detail, "template")
            audit_action = "guest.template.convert"
            audit_details = None
            error_label = f"Template conversion failed: {err}" if err else ""
        else:
            response, err, client = _guest_post_with_client(detail, f"status/{action}")
            audit_action = f"guest.power.{action}"
            audit_details = None
            error_label = f"Power action failed: {err}" if err else ""

        if err:
            failure_details = dict(audit_details or {})
            failure_details["error"] = error_label
            _audit_guest(request, detail, audit_action, failure_details, outcome="failed")
            continue

        _audit_guest_task_or_success(request, detail, audit_action, response, client, audit_details)
        if action == "template":
            _update_latest_guest_scan_config(detail, {"template": "1"}, [])
        if action == "destroy":
            _delete_latest_guest_scan_object(detail)
        if action in GUEST_POWER_ACTIONS or action in {"template", "clone", "tags", "destroy"}:
            clear_live_guest_caches()

    redirect_to = request.POST.get("next", "").strip()
    if redirect_to and url_has_allowed_host_and_scheme(
        redirect_to,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(redirect_to)
    return redirect("core:vms_overview")


def _parse_guest_target_value(value: str) -> tuple[str | None, int | None, str]:
    target_text, _node_separator, node = str(value or "").partition("@")
    object_type, separator, vmid_text = target_text.partition(":")
    if separator != ":" or object_type not in GUEST_OBJECT_TYPES:
        return None, None, ""
    try:
        return object_type, int(vmid_text), node
    except ValueError:
        return None, None, ""


def _clone_guest_from_bulk_request(request, detail: SimpleNamespace) -> tuple[str, dict, object | None, object | None]:
    newid = request.POST.get("clone_newid", "").strip()
    clone_name = request.POST.get("clone_name", "").strip()
    storage = request.POST.get("clone_storage", "").strip()
    full = request.POST.get("clone_full") == "1"
    if not newid.isdigit() or int(newid) <= 0:
        return "New VMID must be a positive whole number.", {}, None, None
    if not clone_name:
        return "Name is required.", {"new_vmid": int(newid)}, None, None
    if not detail.node:
        return "Could not resolve the guest's current node.", {}, None, None

    data: dict[str, object] = {"newid": newid, "full": 1 if full else 0}
    if clone_name:
        data["name" if detail.object_type == ProxmoxInventory.ObjectType.VM else "hostname"] = clone_name
    if storage and full:
        data["storage"] = storage

    response, err, client = _guest_post_with_client(detail, "clone", data)
    audit_details = {
        "source_vmid": detail.vmid,
        "new_vmid": int(newid),
        "new_name": clone_name,
        "full": full,
        "storage": storage,
    }
    return err or "", audit_details, response, client


def _destroy_guest_from_bulk_request(request, detail: SimpleNamespace) -> tuple[str, dict, object | None, object | None]:
    confirm_vmid = request.POST.get("destroy_confirm_vmid", "").strip()
    if confirm_vmid != str(detail.vmid):
        return "The confirmation VMID did not match.", {}, None, None
    if detail.status == "running":
        return "Stop the guest before destroying it.", {"status": detail.status}, None, None

    purge = request.POST.get("destroy_purge") == "1"
    destroy_unreferenced_disks = request.POST.get("destroy_unreferenced_disks") == "1"
    params = {"purge": "1" if purge else "0"}
    if detail.object_type == ProxmoxInventory.ObjectType.VM:
        params["destroy-unreferenced-disks"] = "1" if destroy_unreferenced_disks else "0"
    query = urlencode(params)
    response, err, client = _guest_destroy_with_client(detail, query)
    return err or "", {"purge": purge, "destroy_unreferenced_disks": destroy_unreferenced_disks}, response, client


def _update_guest_tags_from_bulk_request(request, detail: SimpleNamespace) -> tuple[str, dict]:
    mode = request.POST.get("tags_mode", "").strip()
    requested_tags = _split_tag_text(request.POST.get("tags_value", ""))
    if mode not in {"add", "remove", "replace"}:
        return "Unknown tag operation.", {}
    if mode in {"add", "remove"} and not requested_tags:
        return "Enter at least one tag.", {"mode": mode, "tags": requested_tags}

    client = None
    fresh: dict = {}
    for candidate in configured_clients():
        try:
            fresh = candidate.guest_config(node=detail.node, object_type=detail.object_type, vmid=detail.vmid)
            client = candidate
            break
        except ProxmoxAPIError:
            continue
    if client is None:
        return "Could not read the current guest config from Proxmox.", {"mode": mode, "tags": requested_tags}
    if fresh.get("lock"):
        return f"Guest is locked by another Proxmox operation ({fresh.get('lock')}).", {"mode": mode, "tags": requested_tags}

    current_tags = parse_guest_tags(fresh)
    current_lookup = {tag.lower(): tag for tag in current_tags}
    if mode == "replace":
        next_tags = _unique_tags(requested_tags)
    elif mode == "add":
        next_tags = list(current_tags)
        for tag in requested_tags:
            if tag.lower() not in current_lookup:
                next_tags.append(tag)
    else:
        remove_set = {tag.lower() for tag in requested_tags}
        next_tags = [tag for tag in current_tags if tag.lower() not in remove_set]

    audit_details = {
        "mode": mode,
        "tags": requested_tags,
        "previous_tags": current_tags,
        "new_tags": next_tags,
    }
    if current_tags == next_tags:
        audit_details["noop"] = True
        return "", audit_details

    updates: dict[str, str] = {}
    delete: list[str] = []
    if next_tags:
        updates["tags"] = ";".join(next_tags)
    else:
        delete.append("tags")

    try:
        client.set_guest_config(
            node=detail.node,
            object_type=detail.object_type,
            vmid=detail.vmid,
            updates=updates,
            delete=delete,
            digest=fresh.get("digest"),
        )
    except ProxmoxAPIError as exc:
        return str(exc), audit_details

    _update_latest_guest_scan_config(detail, updates, delete)
    return "", audit_details


def _split_tag_text(value: str) -> list[str]:
    return _unique_tags(t for t in re.split(r"[;,\s]+", str(value or "").strip()) if t)


def _unique_tags(tags) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        tag = str(tag).strip()
        key = tag.lower()
        if not tag or key in seen:
            continue
        seen.add(key)
        result.append(tag)
    return result


def _update_latest_guest_scan_config(detail: SimpleNamespace, updates: dict[str, str], delete: list[str]) -> None:
    latest_scan = _latest_proxmox_inventory_scan()
    if not latest_scan:
        return
    obj = ProxmoxInventory.objects.filter(
        scan_run=latest_scan,
        object_type=detail.object_type,
        vmid=detail.vmid,
    ).first()
    if not obj or not isinstance(obj.config, dict):
        return
    config = dict(obj.config)
    config.update(updates)
    for key in delete:
        config.pop(key, None)
    obj.config = config
    obj.save(update_fields=["config"])


def _delete_latest_guest_scan_object(detail: SimpleNamespace) -> None:
    latest_scan = _latest_proxmox_inventory_scan()
    if not latest_scan:
        return
    ProxmoxInventory.objects.filter(
        scan_run=latest_scan,
        object_type=detail.object_type,
        vmid=detail.vmid,
    ).delete()


@app_login_required
def guest_clone_options(request, object_type: str, vmid: int):
    if not settings.VM_WRITE_ENABLED:
        return JsonResponse({"error": "VM/CT writes are disabled."}, status=403)
    detail = _require_guest(object_type, vmid)
    nextid = ""
    storages: list[str] = []
    content = "rootdir" if object_type == ProxmoxInventory.ObjectType.CT else "images"
    for client in configured_clients():
        try:
            nextid = str(client.get("cluster/nextid") or "")
        except ProxmoxAPIError:
            nextid = ""
        try:
            raw_storages = client.get(f"nodes/{quote(detail.node, safe='')}/storage")
        except ProxmoxAPIError:
            raw_storages = []
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

    return JsonResponse(
        {
            "nextid": nextid,
            "storages": [{"id": storage, "label": storage} for storage in storages],
            "default_storage": default_storage,
            "source_storages": source_storages,
            "suggested_name": f"{detail.name}-clone" if detail.name else "",
        }
    )


@require_POST
@app_login_required
def guest_snapshot_create(request, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid)
    name = request.POST.get("snapname", "").strip()
    if not name:
        messages.error(request, "Snapshot name is required.")
        return redirect("core:guest_snapshots", object_type=object_type, vmid=vmid)
    data = {"snapname": name}
    description = request.POST.get("description", "").strip()
    if description:
        data["description"] = description
    if request.POST.get("vmstate") == "on":
        data["vmstate"] = 1
    _data, err = _guest_post_wait_task(detail, "snapshot", data)
    if err:
        messages.error(request, _snapshot_error(err))
    else:
        _audit_guest(request, detail, "guest.snapshot.create", {"snapshot": name})
    return redirect("core:guest_snapshots", object_type=object_type, vmid=vmid)


@require_POST
@app_login_required
def guest_snapshot_delete(request, object_type: str, vmid: int, snapname: str):
    detail = _require_guest(object_type, vmid)
    _data, err = _guest_delete_wait_task(detail, f"snapshot/{quote(snapname, safe='')}")
    if err:
        messages.error(request, _snapshot_error(err))
    else:
        _audit_guest(request, detail, "guest.snapshot.delete", {"snapshot": snapname})
    return redirect("core:guest_snapshots", object_type=object_type, vmid=vmid)


@require_POST
@app_login_required
def guest_snapshot_delete_all(request, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid)
    if not settings.VM_WRITE_ENABLED:
        messages.error(request, "Snapshot changes are disabled (VM_WRITE_ENABLED is off).")
        return redirect("core:guest_snapshots", object_type=object_type, vmid=vmid)
    deleted, err = _delete_all_guest_snapshots(detail)
    if err:
        messages.error(request, _snapshot_error(err))
    else:
        _audit_guest(request, detail, "guest.snapshot.delete_all", {"deleted": deleted})
    return redirect("core:guest_snapshots", object_type=object_type, vmid=vmid)


@require_POST
@app_login_required
def guest_snapshot_rollback(request, object_type: str, vmid: int, snapname: str):
    detail = _require_guest(object_type, vmid)
    _data, err = _guest_post(detail, f"snapshot/{quote(snapname, safe='')}/rollback")
    if err:
        messages.error(request, _snapshot_error(err))
    else:
        _audit_guest(request, detail, "guest.snapshot.rollback", {"snapshot": snapname})
    return redirect("core:guest_snapshots", object_type=object_type, vmid=vmid)


def _snapshot_error(err: str) -> str:
    if "403" in err:
        return "Proxmox denied the snapshot operation (403) - the token needs VM.Snapshot (and VM.Snapshot.Rollback for rollback)."
    return f"Snapshot operation failed: {err}"


@app_login_required
def guest_create(request, object_type: str):
    if object_type not in GUEST_OBJECT_TYPES:
        raise Http404("Unknown guest type")
    if not settings.VM_WRITE_ENABLED:
        messages.error(request, "VM/CT creation is disabled (VM_WRITE_ENABLED is off).")
        return redirect("core:vms")

    is_vm = object_type == ProxmoxInventory.ObjectType.VM
    node_param = request.POST.get("node") if request.method == "POST" else request.GET.get("node")
    options = create_options(object_type, node_param)
    if not options.get("available"):
        messages.error(request, "Could not load creation options from Proxmox (no reachable node).")
        return redirect("core:vms")

    if request.method == "POST":
        error = _create_guest(request, object_type, options)
        if error is None:
            return redirect("core:vms")
        messages.error(request, error)
        form_values = request.POST
    else:
        form_values = {
            "vmid": options.get("nextid", ""),
            "cores": "1",
            "sockets": "1",
            "memory": "2048" if is_vm else "512",
            "disk_size": "32" if is_vm else "8",
            "swap": "512",
            "ip": "dhcp",
        }

    context = {
        **navigation_context("vms"),
        "object_type": object_type,
        "is_vm": is_vm,
        "options": options,
        "form_values": form_values,
    }
    return render(request, "core/guest_create.html", context)


def _create_guest(request, object_type: str, options: dict):
    post = request.POST
    node = post.get("node", "").strip() or options.get("node", "")
    vmid = post.get("vmid", "").strip()
    if not vmid.isdigit():
        return "VMID must be a whole number."
    disk_storage = post.get("disk_storage", "").strip()
    if not disk_storage:
        return "Select a storage for the disk."
    bridge = post.get("bridge", "").strip()

    common = {
        "vmid": vmid,
        "cores": post.get("cores", "1").strip() or "1",
        "memory": post.get("memory", "512").strip() or "512",
        "disk_storage": disk_storage,
        "disk_size": post.get("disk_size", "8").strip() or "8",
        "bridge": bridge,
        "vlan": post.get("vlan", "").strip(),
        "start": post.get("start") == "on",
    }

    if object_type == ProxmoxInventory.ObjectType.VM:
        name = post.get("name", "").strip()
        if not name:
            return "Name is required."
        params = {
            **common,
            "name": name,
            "ostype": post.get("ostype", "l26").strip() or "l26",
            "sockets": post.get("sockets", "1").strip() or "1",
            "iso": post.get("iso", "").strip(),
        }
        if not bridge:
            params["bridge"] = ""
        _data, err = create_vm(node, params)
    else:
        hostname = post.get("hostname", "").strip()
        if not hostname:
            return "Hostname is required."
        ostemplate = post.get("ostemplate", "").strip()
        if not ostemplate:
            return "Select an OS template."
        password = post.get("password", "")
        ssh_keys = post.get("ssh_keys", "").strip()
        if not password and not ssh_keys:
            return "Set a root password or an SSH public key for the container."
        params = {
            **common,
            "hostname": hostname,
            "ostemplate": ostemplate,
            "swap": post.get("swap", "0").strip() or "0",
            "password": password,
            "ssh_keys": ssh_keys,
            "ip": post.get("ip", "dhcp").strip() or "dhcp",
        }
        _data, err = create_ct(node, params)

    if err:
        if "403" in err:
            return "Proxmox denied creation (403) - the token needs VM.Allocate + Datastore.AllocateSpace (+ SDN.Use for the NIC)."
        return f"Creation failed: {err}"

    AuditEvent.objects.create(
        user=request.user if request.user.is_authenticated else None,
        username=request.user.get_username() if request.user.is_authenticated else "system",
        action="guest.create",
        object_type="guest",
        object_id=f"{object_type}:{vmid}",
        outcome="success",
        details={"node": node, "vmid": vmid, "target_type": object_type, "name": post.get("name") or post.get("hostname") or ""},
    )
    return None


@app_login_required
def storage_api_inventory(request, node: str, storage: str):
    """Read-only Proxmox API view of a storage's full content, for storages
    pve-helper does not mount (local-lvm, ZFS, ...). No write controls."""
    highlight_vmid = _int_or_zero(request.GET.get("vmid")) or None

    content: list = []
    status: dict = {}
    error = ""
    found = False
    for client in configured_clients():
        node_q = quote(node, safe="")
        storage_q = quote(storage, safe="")
        try:
            data = client.get(f"nodes/{node_q}/storage/{storage_q}/content")
        except ProxmoxAPIError as exc:
            error = str(exc)
            continue
        content = data if isinstance(data, list) else []
        found = True
        try:
            status_data = client.get(f"nodes/{node_q}/storage/{storage_q}/status")
            status = status_data if isinstance(status_data, dict) else {}
        except ProxmoxAPIError:
            status = {}
        break

    volumes = []
    for entry in content:
        if not isinstance(entry, dict):
            continue
        entry_vmid = entry.get("vmid")
        volumes.append(
            {
                "volid": entry.get("volid", ""),
                "content": entry.get("content", ""),
                "format": entry.get("format", ""),
                "size": entry.get("size"),
                "used": entry.get("used"),
                "vmid": entry_vmid,
                "highlight": highlight_vmid is not None and str(entry_vmid) == str(highlight_vmid),
            }
        )
    volumes.sort(key=lambda item: (str(item["vmid"] or ""), item["volid"]))

    context = {
        **navigation_context("vms"),
        "node": node,
        "storage": storage,
        "volumes": volumes,
        "status": status,
        "highlight_vmid": highlight_vmid,
        "found": found,
        "error": error,
    }
    return render(request, "core/storage_api_inventory.html", context)


def _resolve_guest_detail(object_type: str, vmid: int, *, node: str = "") -> SimpleNamespace:
    """Resolve a guest to its current node + live status/config.

    Membership/node come from the live cluster inventory; if the API is
    unreachable, fall back to the latest scan. Never silently pick one guest
    when the same type+VMID is ambiguous across multiple nodes.
    """
    requested_node = node
    matches = [
        g
        for g in fetch_live_guest_inventory()
        if g.object_type == object_type and g.vmid == vmid and (not requested_node or g.node == requested_node)
    ]
    nodes = {g.node for g in matches if g.node}
    ambiguous = not requested_node and len(nodes) > 1
    node = next(iter(matches)).node if matches else requested_node
    name = matches[0].name if matches else ""
    status = matches[0].status if matches else ""

    config: dict = {}
    current: dict = {}
    live_ok = False
    if node and not ambiguous:
        for client in configured_clients():
            try:
                current = client.guest_current(node=node, object_type=object_type, vmid=vmid)
                config = client.guest_config(node=node, object_type=object_type, vmid=vmid)
                live_ok = True
                break
            except ProxmoxAPIError:
                continue

    if ambiguous:
        return SimpleNamespace(
            object_type=object_type,
            vmid=vmid,
            name=name or "",
            node="",
            status=status or "",
            config={},
            current={},
            live_ok=False,
            ambiguous=True,
            ambiguous_nodes=sorted(nodes),
            found=False,
        )

    if not config:
        scan = _latest_proxmox_inventory_scan()
        if scan:
            scan_query = ProxmoxInventory.objects.filter(scan_run=scan, object_type=object_type, vmid=vmid)
            if node:
                scan_query = scan_query.filter(node=node)
            obj = scan_query.first()
            if obj:
                config = obj.config if isinstance(obj.config, dict) else {}
                node = node or obj.node
                name = name or obj.name
                status = status or obj.status

    return SimpleNamespace(
        object_type=object_type,
        vmid=vmid,
        name=name or "",
        node=node or "",
        status=str(current.get("status") or status or ""),
        config=config,
        current=current,
        live_ok=live_ok,
        ambiguous=ambiguous,
        ambiguous_nodes=sorted(nodes) if ambiguous else [],
        found=bool(node or config),
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
    guest_list, live_available, scan_at = _guest_rows()
    return {
        **navigation_context("vms"),
        "guest": detail,
        "guest_identity": guest_identity(detail.object_type, detail.vmid, detail.name),
        "guest_is_template": is_tmpl,
        "guest_type_label": type_label,
        "guest_tags": parse_guest_tags(detail.config),
        "guest_tabs": _guest_tabs(detail, active_tab),
        "active_guest_tab": active_tab,
        "guest_write_enabled": settings.VM_WRITE_ENABLED,
        "guest_list": guest_list,
        "live_available": live_available,
        "inventory_scan_at": scan_at,
        "active_object_type": detail.object_type,
        "active_vmid": detail.vmid,
        "active_guest_target_id": active_target,
        "schedule_action_url": f"{reverse('core:scheduled_task_create')}?{urlencode({'target': target})}",
        "scheduled_actions_url": f"{reverse('core:scheduled_tasks')}?{urlencode({'target': target})}",
    }


def _guest_tabs(detail: SimpleNamespace, active_tab: str) -> list[dict]:
    args = [detail.object_type, detail.vmid]
    tabs = [
        {"key": "summary", "label": "Summary", "url": reverse("core:guest_summary", args=args)},
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


GUEST_AGENT_API_TIMEOUT_SECONDS = 2


def _guest_api_get(detail: SimpleNamespace, subpath: str, *, timeout_seconds: float | None = None):
    """GET a guest-scoped Proxmox path (e.g. 'snapshot', 'rrddata?...',
    'agent/get-osinfo'); returns (data, error_message)."""
    kind = "qemu" if detail.object_type == ProxmoxInventory.ObjectType.VM else "lxc"
    if not detail.node:
        return None, "The guest's node could not be resolved."
    err = "No Proxmox endpoint could reach this guest."
    for client in configured_clients():
        try:
            return client.get(
                f"nodes/{quote(detail.node, safe='')}/{kind}/{detail.vmid}/{subpath}",
                timeout=timeout_seconds,
            ), None
        except ProxmoxAPIError as exc:
            err = str(exc)
    return None, err


OSTYPE_LABELS = {
    "l24": "Linux 2.4 Kernel",
    "l26": "Linux (modern)",
    "w2k": "Windows 2000",
    "w2k3": "Windows Server 2003",
    "w2k8": "Windows Server 2008",
    "wvista": "Windows Vista",
    "win7": "Windows 7",
    "win8": "Windows 8/2012",
    "win10": "Windows 10/2016",
    "win11": "Windows 11/2022",
    "wxp": "Windows XP",
    "solaris": "Solaris",
    "other": "Other",
}


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

    cache_key = f"pve-helper:guest-agent-summary:v1:{detail.node}:{detail.object_type}:{detail.vmid}"
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


def _guest_vm_details(detail: SimpleNamespace) -> list[dict]:
    config = detail.config
    current = detail.current
    rows: list[dict] = []

    def add(label, value):
        if value not in (None, "", "-"):
            rows.append({"label": label, "value": value})

    add("Guest OS", _guest_os_label(config))
    add("Node", detail.node or "-")
    if config.get("bios"):
        add("Firmware", "UEFI (OVMF)" if config.get("bios") == "ovmf" else "SeaBIOS")
    if config.get("machine"):
        add("Machine", config.get("machine"))
    if config.get("boot"):
        add("Boot order", str(config.get("boot")).replace("order=", ""))
    if config.get("agent"):
        add("Guest agent", "Enabled")
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
    if object_type != ProxmoxInventory.ObjectType.VM:
        return None
    cores = _int_or_zero(config.get("cores"))
    if not cores:
        return None
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


def _int_or_zero(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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


@app_login_required
def scheduled_tasks(request):
    target_filter = request.GET.get("target", "")
    target_type, target_vmid = _parse_scheduled_target(target_filter)
    target_filter_value = f"{target_type}:{target_vmid}" if target_type and target_vmid else ""

    actions_query = ScheduledAction.objects.select_related("created_by")
    if target_filter_value:
        actions_query = actions_query.filter(target_type=target_type, target_vmid=target_vmid)

    actions = list(actions_query.order_by("-enabled", "next_run_at", "name"))
    latest_runs = _latest_scheduled_runs(target_type=target_type, target_vmid=target_vmid)

    for action in actions:
        action.display_target = _scheduled_action_target_label(action)
        action.guest_identity = guest_identity_from_scheduled_action(action)
        action.display_schedule = _scheduled_action_schedule_label(action)
        action.display_status_class = _scheduled_action_status_class(action.last_status)
        action.display_creator = action.created_by.get_username() if action.created_by else "system"

    scheduled_runs_url = reverse("core:scheduled_task_runs")
    if target_filter_value:
        scheduled_runs_url = f"{scheduled_runs_url}?{urlencode({'target': target_filter_value})}"

    context = {
        **navigation_context("scheduled_tasks"),
        "scheduled_actions": actions,
        "latest_runs": latest_runs,
        "scheduled_actions_enabled": settings.SCHEDULED_ACTIONS_ENABLED,
        "schedule_timezone": settings.TIME_ZONE,
        "run_retention_days": settings.SCHEDULED_ACTION_RUN_RETENTION_DAYS,
        "target_filter": target_filter_value,
        "target_filter_label": _scheduled_target_label(target_type, target_vmid) if target_filter_value else "",
        "scheduled_task_create_query": urlencode({"target": target_filter_value}) if target_filter_value else "",
        "scheduled_runs_url": scheduled_runs_url,
    }
    return render(request, "core/scheduled_tasks.html", context)


@app_login_required
def scheduled_task_runs(request):
    target_filter = request.GET.get("target", "")
    target_type, target_vmid = _parse_scheduled_target(target_filter)
    return JsonResponse(
        {
            "runs": [
                _serialize_scheduled_run(run)
                for run in _latest_scheduled_runs(target_type=target_type, target_vmid=target_vmid, limit=10)
            ]
        }
    )


@app_login_required
def scheduled_task_create(request):
    action = ScheduledAction()
    if request.method == "POST":
        errors = _apply_scheduled_action_form(action, request.POST, request.user)
        if not errors:
            _audit_scheduled_action_definition(request, "scheduled_action.created", action)
            return redirect("core:scheduled_tasks")
    else:
        target_type, target_vmid = _parse_scheduled_target(request.GET.get("target", ""))
        if target_type and target_vmid:
            action.target_type = target_type
            action.target_vmid = target_vmid
            _apply_target_snapshot(action)
        errors = []

    context = _scheduled_action_form_context(
        action,
        form_values=_scheduled_action_form_values(action, request.POST if request.method == "POST" else None),
        errors=errors,
        mode="create",
    )
    return render(request, "core/scheduled_task_form.html", context, status=400 if errors else 200)


@app_login_required
def scheduled_task_edit(request, action_id: int):
    action = get_object_or_404(ScheduledAction, pk=action_id)
    if request.method == "POST":
        errors = _apply_scheduled_action_form(action, request.POST, request.user)
        if not errors:
            _audit_scheduled_action_definition(request, "scheduled_action.updated", action)
            return redirect("core:scheduled_tasks")
    else:
        errors = []

    context = _scheduled_action_form_context(
        action,
        form_values=_scheduled_action_form_values(action, request.POST if request.method == "POST" else None),
        errors=errors,
        mode="edit",
    )
    return render(request, "core/scheduled_task_form.html", context, status=400 if errors else 200)


@require_POST
@app_login_required
def scheduled_task_toggle(request, action_id: int):
    action = get_object_or_404(ScheduledAction, pk=action_id)
    enabled = request.POST.get("enabled") == "1"
    if enabled:
        try:
            _refresh_scheduled_action_next_run(action)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("core:scheduled_tasks")
    action.enabled = enabled
    action.save(update_fields=["enabled", "next_run_at", "updated_at"])
    _audit_scheduled_action_definition(
        request,
        "scheduled_action.enabled" if enabled else "scheduled_action.disabled",
        action,
    )
    return redirect("core:scheduled_tasks")


@require_POST
@app_login_required
def scheduled_task_delete(request, action_id: int):
    action = get_object_or_404(ScheduledAction, pk=action_id)
    _audit_scheduled_action_definition(request, "scheduled_action.deleted", action)
    action.delete()
    return redirect("core:scheduled_tasks")


@require_POST
@app_login_required
def scheduled_task_run_now(request, action_id: int):
    action = get_object_or_404(ScheduledAction, pk=action_id)
    try:
        queue_manual_scheduled_action_run(action, triggered_by=request.user)
    except ScheduledActionQueueError as exc:
        messages.error(request, str(exc))
    return redirect("core:scheduled_tasks")


def _decorate_storages_with_scan_state(storages: list[StorageMount], result_scan: ScanRun | None) -> None:
    for storage in storages:
        storage_result_scan = _latest_storage_result_scan(storage)
        storage.latest_counts = _classification_counts(
            FileInventory.objects.filter(scan_run=storage_result_scan, storage=storage)
            if storage_result_scan
            else FileInventory.objects.none()
        )
        storage.latest_file_count = sum(storage.latest_counts.values())
        storage.latest_gate_status = (result_scan.storage_gate_status or {}).get(storage.storage_id, {}) if result_scan else {}
        storage.latest_scan = storage_result_scan
        storage.latest_scan_at = _scan_timestamp(storage_result_scan)
        storage.space_info = storage_space_info(storage.path)
        storage.storage_actions_enabled = settings.STORAGE_WRITE_ENABLED and storage.space_info.can_write
        storage.details = storage_details(storage, storage_result_scan, storage.space_info)


@app_login_required
def storage_browser(request, storage_id: str):
    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    _decorate_storage_with_space_info(storage)
    latest_scan = _latest_storage_result_scan(storage)
    current_path = _normalize_browser_path(request.GET.get("path", ""))
    parent_path = _parent_path(current_path)
    file_query = request.GET.get("q", "").strip()[:200]
    file_offset = max(0, _int_request_param(request, "file_offset", 0))
    file_partial = request.GET.get("file_partial") == "1"
    entries = []
    current_entry = None
    folder_tree = []

    if latest_scan:
        ignored_paths = ignored_relative_paths_for_storage(storage)
        if current_path:
            if is_ignored_storage_path(current_path, ignored_paths):
                raise Http404("Directory not found in latest scan.")
            current_entry = FileInventory.objects.filter(
                scan_run=latest_scan,
                storage=storage,
                path=current_path,
                entry_type=FileInventory.EntryType.DIRECTORY,
            ).first()
            if current_entry is None:
                raise Http404("Directory not found in latest scan.")

        candidates = FileInventory.objects.filter(scan_run=latest_scan, storage=storage)
        if current_path:
            candidates = candidates.filter(path__startswith=f"{current_path}/")

        prefix = f"{current_path}/" if current_path else ""
        for entry in candidates:
            if is_ignored_storage_path(entry.path, ignored_paths):
                continue
            remainder = entry.path[len(prefix) :] if prefix else entry.path
            if not remainder or "/" in remainder:
                continue
            entry.name = remainder
            _decorate_browser_entry(entry)
            entries.append(entry)
        folder_tree = _browser_folder_tree(latest_scan, storage, current_path, ignored_paths=ignored_paths)

    entries.sort(key=lambda item: (item.entry_type != FileInventory.EntryType.DIRECTORY, item.name.lower()))
    if file_query:
        query = file_query.lower()
        entries = [
            entry
            for entry in entries
            if query in " ".join(
                [
                    entry.name.lower(),
                    entry.path.lower(),
                    (entry.content_category or "").lower(),
                    (entry.classification or "").lower(),
                    getattr(entry, "classification_label", "").lower(),
                    getattr(entry, "category_label", "").lower(),
                ]
            )
        ]

    file_total = len(entries)
    entries = entries[file_offset:file_offset + FILE_BROWSER_BATCH_SIZE]
    file_next_offset = file_offset + FILE_BROWSER_BATCH_SIZE
    file_has_next = file_next_offset < file_total
    file_next_url = (
        _storage_browser_url(
            storage,
            current_path,
            q=file_query,
            file_offset=file_next_offset,
        )
        if file_has_next
        else ""
    )

    context = {
        **_storage_tab_context(storage, latest_scan, "files"),
        "current_path": current_path,
        "parent_path": parent_path,
        "breadcrumbs": _browser_breadcrumbs(current_path),
        "folder_tree": folder_tree,
        "entries": entries,
        "current_entry": current_entry,
        "file_query": file_query,
        "file_offset": file_offset,
        "file_batch_size": FILE_BROWSER_BATCH_SIZE,
        "file_total": file_total,
        "file_start": min(file_offset + 1, file_total),
        "file_end": min(file_offset + len(entries), file_total),
        "file_has_next": file_has_next,
        "file_next_url": file_next_url,
        "include_parent_row": current_path and file_offset == 0,
    }
    if file_partial:
        return JsonResponse(
            {
                "rows_html": render_to_string(
                    "core/partials/storage_file_rows.html",
                    {**context, "include_parent_row": False},
                    request=request,
                ),
                "has_next": file_has_next,
                "next_url": file_next_url,
                "total": file_total,
                "end": context["file_end"],
            }
        )
    return render(request, "core/storage_browser.html", context)


@app_login_required
def download_storage_file(request, storage_id: str):
    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    latest_scan = _latest_storage_result_scan(storage)
    if latest_scan is None:
        raise Http404("No storage inventory has been scanned yet.")

    requested_path = _normalize_browser_path(request.GET.get("path", ""))
    if not requested_path:
        raise Http404("No file path requested.")

    entry = get_object_or_404(
        FileInventory,
        scan_run=latest_scan,
        storage=storage,
        path=requested_path,
        entry_type=FileInventory.EntryType.FILE,
    )
    absolute_path = _resolve_storage_file(storage, entry.path)

    AuditEvent.objects.create(
        user=request.user if request.user.is_authenticated else None,
        username=request.user.get_username() if request.user.is_authenticated else "",
        source_ip=_client_ip(request),
        action="file.downloaded",
        object_type="file",
        object_id=f"{storage.storage_id}:{entry.path}",
        outcome="success",
        details={
            "storage_id": storage.storage_id,
            "storage_name": storage.display_name,
            "path": entry.path,
            "size_bytes": entry.size_bytes,
            "scan_run": latest_scan.id,
        },
    )

    return _download_response(request, storage, entry.path, absolute_path)


@require_POST
@app_login_required
def create_storage_folder(request, storage_id: str):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    current_path = _normalize_browser_path(request.POST.get("path", ""))
    redirect_to = _safe_next_url(request) or _storage_browser_url(storage, current_path)
    latest_scan = _latest_storage_result_scan(storage)
    if current_path:
        _storage_directory_or_404(storage, latest_scan, current_path)

    try:
        result = create_storage_directory(
            storage=storage,
            directory_path=current_path,
            folder_name=request.POST.get("folder_name", ""),
        )
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_to)

    _audit_file_action(
        request,
        action="file.folder_created",
        storage=storage,
        path=str(result["path"]),
        details={"directory_path": result["directory_path"]},
    )
    _refresh_latest_storage_directory(storage, str(result["directory_path"]))
    return redirect(redirect_to)


@require_POST
@app_login_required
def upload_storage_file(request, storage_id: str):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    current_path = _normalize_browser_path(request.POST.get("path", ""))
    redirect_to = _safe_next_url(request) or _storage_browser_url(storage, current_path)
    uploaded_file = request.FILES.get("file")
    if uploaded_file is None:
        return _upload_error_response(request, redirect_to, "No upload file selected.")

    latest_scan = _latest_storage_result_scan(storage)
    if current_path:
        _storage_directory_or_404(storage, latest_scan, current_path)

    try:
        result = upload_to_storage(
            storage=storage,
            directory_path=current_path,
            uploaded_file=uploaded_file,
        )
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        return _upload_error_response(request, redirect_to, str(exc))

    _audit_file_action(
        request,
        action="file.uploaded",
        storage=storage,
        path=str(result["path"]),
        details={"size_bytes": result["size_bytes"]},
    )
    _queue_upload_normalization(storage, [str(result["path"])], request.user)
    _refresh_latest_storage_directory(storage, current_path)
    return _upload_success_response(request, redirect_to)


@require_POST
@app_login_required
def upload_storage_folder(request, storage_id: str):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    current_path = _normalize_browser_path(request.POST.get("path", ""))
    redirect_to = _safe_next_url(request) or _storage_browser_url(storage, current_path)
    uploaded_files = request.FILES.getlist("files")
    relative_paths = request.POST.getlist("relative_path")
    if not uploaded_files:
        return _upload_error_response(request, redirect_to, "No upload files selected.")
    if not relative_paths:
        relative_paths = [uploaded_file.name for uploaded_file in uploaded_files]

    latest_scan = _latest_storage_result_scan(storage)
    if current_path:
        _storage_directory_or_404(storage, latest_scan, current_path)

    try:
        result = upload_folder_to_storage(
            storage=storage,
            directory_path=current_path,
            uploaded_files=uploaded_files,
            relative_paths=relative_paths,
        )
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        return _upload_error_response(request, redirect_to, str(exc))

    _audit_file_action(
        request,
        action="file.folder_uploaded",
        storage=storage,
        path=current_path or "/",
        details={
            "file_count": result["file_count"],
            "size_bytes": result["size_bytes"],
            "directory_path": result["directory_path"],
        },
    )
    _queue_upload_normalization(storage, [str(path) for path in result["paths"]], request.user)
    for directory_path in result["directory_paths"]:
        _refresh_latest_storage_directory(storage, str(directory_path))
    return _upload_success_response(request, redirect_to)


@app_login_required
def storage_trash(request, storage_id: str):
    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    _decorate_storage_with_space_info(storage)
    latest_scan = _latest_storage_result_scan(storage)
    if settings.STORAGE_WRITE_ENABLED and storage.storage_actions_enabled:
        try:
            cleanup_empty_app_trash_directories(storage=storage)
        except StorageActionError:
            pass
    if latest_scan:
        try:
            adopt_discovered_trash_items(storage=storage, scan=latest_scan)
        except StorageActionError:
            pass
    items = list(
        TrashItem.objects.filter(
            storage_id=storage.storage_id,
            restore_status=TrashItem.RestoreStatus.TRASHED,
        )
        .select_related("moved_by")
        .order_by("-moved_at", "-created_at")[:200]
    )
    items = [
        item
        for item in items
        if not is_nfs_silly_rename_path(item.original_path) and not is_nfs_silly_rename_path(item.trash_path)
    ]
    context = {
        **navigation_context("storage_browser", active_storage_id=storage.storage_id),
        "storage": storage,
        "items": items,
    }
    return render(request, "core/storage_trash.html", context)


@app_login_required
def storage_summary(request, storage_id: str):
    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    _decorate_storage_with_space_info(storage)
    latest_scan = _latest_storage_result_scan(storage)

    classification_counts = {}
    total_file_count = 0
    if latest_scan:
        classification_counts = _classification_counts(
            FileInventory.objects.filter(scan_run=latest_scan, storage=storage)
        )
        total_file_count = sum(classification_counts.values())

    gate_status = {}
    if latest_scan and latest_scan.storage_gate_status:
        gate_status = latest_scan.storage_gate_status.get(storage.storage_id, {})

    consumers = list(storage.consumer_statuses.order_by("expected_node_name"))

    context = {
        **_storage_tab_context(storage, latest_scan, "summary"),
        "classification_counts": classification_counts,
        "total_file_count": total_file_count,
        "gate_status": gate_status,
        "consumers": consumers,
    }
    return render(request, "core/storage_summary.html", context)


@app_login_required
def storage_monitor(request, storage_id: str):
    MONITOR_PAGE_SIZE = 10
    ACTIVITY_RETENTION_DAYS = 7

    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    _decorate_storage_with_space_info(storage)
    latest_scan = _latest_storage_result_scan(storage)

    scan_page = max(0, _int_request_param(request, "scan_page", 0))
    event_page = max(0, _int_request_param(request, "event_page", 0))

    activity_cutoff = tz.now() - timedelta(days=ACTIVITY_RETENTION_DAYS)
    all_scans = ScanRun.objects.filter(
        Q(target_storage=storage) | Q(target_storage__isnull=True),
        created_at__gte=activity_cutoff,
    ).order_by("-created_at")
    scan_total = all_scans.count()
    scan_start = scan_page * MONITOR_PAGE_SIZE
    scan_end = scan_start + MONITOR_PAGE_SIZE
    recent_scans = list(all_scans[scan_start:scan_end])

    all_events = AuditEvent.objects.filter(
        storage_id=storage.storage_id,
        timestamp__gte=activity_cutoff,
    ).order_by("-timestamp")
    event_total = all_events.count()
    event_start = event_page * MONITOR_PAGE_SIZE
    event_end = event_start + MONITOR_PAGE_SIZE
    recent_events = list(all_events[event_start:event_end])
    _decorate_audit_events(recent_events)

    space_chart_data = _storage_space_chart_data(storage, tz.now())

    context = {
        **_storage_tab_context(storage, latest_scan, "monitor"),
        "recent_scans": recent_scans,
        "scan_page": scan_page,
        "scan_total": scan_total,
        "scan_start": min(scan_start + 1, scan_total),
        "scan_end": min(scan_end, scan_total),
        "scan_has_prev": scan_page > 0,
        "scan_has_next": scan_end < scan_total,
        "recent_events": recent_events,
        "event_page": event_page,
        "event_total": event_total,
        "event_start": min(event_start + 1, event_total),
        "event_end": min(event_end, event_total),
        "event_has_prev": event_page > 0,
        "event_has_next": event_end < event_total,
        "space_chart_data_json": json.dumps(space_chart_data),
    }
    return render(request, "core/storage_monitor.html", context)


def _storage_space_chart_data(storage: StorageMount, now) -> list[dict[str, object]]:
    cutoff = now - timedelta(days=SPACE_CHART_DAYS)
    scheduled_history = list(
        StorageSpaceSnapshot.objects.filter(
            storage=storage,
            scan_run__isnull=True,
            recorded_at__gte=cutoff,
        ).order_by("recorded_at")
    )
    history = scheduled_history or list(
        StorageSpaceSnapshot.objects.filter(
            storage=storage,
            recorded_at__gte=cutoff,
        ).order_by("recorded_at")
    )

    bucket_seconds = SPACE_CHART_BUCKET_HOURS * 60 * 60
    buckets: dict[int, StorageSpaceSnapshot] = {}
    for snapshot in history:
        seconds_since_cutoff = max(0, int((snapshot.recorded_at - cutoff).total_seconds()))
        bucket = seconds_since_cutoff // bucket_seconds
        buckets[bucket] = snapshot

    snapshots = [buckets[bucket] for bucket in sorted(buckets)][-SPACE_CHART_MAX_POINTS:]
    return [
        {
            "timestamp": snapshot.recorded_at.isoformat(),
            "used_bytes": snapshot.used_bytes,
            "total_bytes": snapshot.total_bytes,
            "available_bytes": snapshot.available_bytes,
        }
        for snapshot in snapshots
    ]


@app_login_required
def storage_configure(request, storage_id: str):
    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    _decorate_storage_with_space_info(storage)
    latest_scan = _latest_storage_result_scan(storage)
    context = {
        **_storage_tab_context(storage, latest_scan, "configure"),
    }
    return render(request, "core/storage_configure.html", context)


@app_login_required
def storage_permissions_view(request, storage_id: str):
    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    _decorate_storage_with_space_info(storage)
    latest_scan = _latest_storage_result_scan(storage)

    perms = get_permissions(storage.path)

    context = {
        **_storage_tab_context(storage, latest_scan, "permissions"),
        "permissions": perms,
    }
    return render(request, "core/storage_permissions.html", context)


@app_login_required
def storage_hosts(request, storage_id: str):
    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    _decorate_storage_with_space_info(storage)
    latest_scan = _latest_storage_result_scan(storage)

    consumers = list(storage.consumer_statuses.order_by("expected_node_name"))

    proxmox_storage_entries = []
    if latest_scan:
        proxmox_storage_entries = list(
            ProxmoxInventory.objects.filter(
                scan_run=latest_scan,
                object_type=ProxmoxInventory.ObjectType.STORAGE,
                name=storage.storage_id,
            ).order_by("node")
        )

    context = {
        **_storage_tab_context(storage, latest_scan, "hosts"),
        "consumers": consumers,
        "proxmox_storage_entries": proxmox_storage_entries,
    }
    return render(request, "core/storage_hosts.html", context)


@app_login_required
def storage_vms(request, storage_id: str):
    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    _decorate_storage_with_space_info(storage)
    latest_scan = _latest_storage_result_scan(storage)

    guests = []
    if latest_scan:
        prefix = f"{storage.storage_id}:"
        for obj in ProxmoxInventory.objects.filter(
            scan_run=latest_scan,
            object_type__in=[
                ProxmoxInventory.ObjectType.VM,
                ProxmoxInventory.ObjectType.CT,
            ],
        ).order_by("object_type", "vmid"):
            matching_refs = [ref for ref in (obj.disk_references or []) if ref.startswith(prefix)]
            if matching_refs:
                obj.matching_disk_references = matching_refs
                guests.append(obj)

    if guests:
        live_status = fetch_live_guest_status()
        for guest in guests:
            guest.status = _live_status_for(live_status, guest.node or "", guest.object_type, guest.vmid, guest.status)
        _decorate_guests_with_scheduled_actions(guests)

    context = {
        **_storage_tab_context(storage, latest_scan, "vms"),
        "guests": guests,
        "inventory_scan_at": _scan_timestamp(latest_scan),
        "live_status_cache_seconds": LIVE_GUEST_STATUS_CACHE_SECONDS,
    }
    return render(request, "core/storage_vms.html", context)


@require_POST
@app_login_required
def trash_storage_file(request, storage_id: str):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    redirect_to = _safe_next_url(request)
    latest_scan = _latest_storage_result_scan(storage)
    entries = _selected_storage_file_entries(
        request,
        storage=storage,
        latest_scan=latest_scan,
        entry_types=[FileInventory.EntryType.FILE, FileInventory.EntryType.DIRECTORY],
    )

    try:
        _require_file_action_confirmations_for_entries(request, entries)
        results = [
            (entry, move_file_to_trash(storage=storage, entry=entry, user=request.user))
            for entry in entries
        ]
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_to)

    refresh_directories = set()
    pruned_paths = set()
    for entry, trash_item in results:
        _audit_file_action(
            request,
            action="file.trashed",
            storage=storage,
            path=entry.path,
            details={"trash_item": trash_item.id, "trash_path": trash_item.trash_path},
        )
        if entry.entry_type == FileInventory.EntryType.DIRECTORY:
            pruned_paths.add(entry.path)
        refresh_directories.add(_parent_path(entry.path))
    for path in pruned_paths:
        _prune_latest_storage_path(storage, path)
    for directory_path in refresh_directories:
        _refresh_latest_storage_directory(storage, directory_path)
    return redirect(redirect_to)


@require_POST
@app_login_required
def move_storage_file_view(request, storage_id: str):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    redirect_to = _safe_next_url(request)
    latest_scan = _latest_storage_result_scan(storage)
    entries = _selected_storage_file_entries(request, storage=storage, latest_scan=latest_scan)

    try:
        _require_file_action_confirmations_for_entries(request, entries)
        results = [
            move_storage_file(
                storage=storage,
                entry=entry,
                new_path=request.POST.get("new_path", ""),
            )
            for entry in entries
        ]
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_to)

    refresh_directories = set()
    for result in results:
        _audit_file_action(
            request,
            action="file.moved",
            storage=storage,
            path=str(result["new_path"]),
            details={"old_path": result["old_path"]},
        )
        refresh_directories.add(str(result["source_directory_path"]))
        refresh_directories.add(str(result["target_directory_path"]))
    for directory_path in refresh_directories:
        _refresh_latest_storage_directory(storage, directory_path)
    return redirect(redirect_to)


@require_POST
@app_login_required
def rename_storage_file_view(request, storage_id: str):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    redirect_to = _safe_next_url(request)
    latest_scan = _latest_storage_result_scan(storage)
    requested_path = _normalize_browser_path(request.POST.get("path", ""))
    entry = get_object_or_404(
        FileInventory,
        scan_run=latest_scan,
        storage=storage,
        path=requested_path,
        entry_type=FileInventory.EntryType.FILE,
    )
    risk = file_action_risk(entry)

    try:
        _require_file_action_confirmations(request, risk)
        result = rename_storage_file(
            storage=storage,
            entry=entry,
            new_name=request.POST.get("new_name", ""),
        )
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_to)

    _audit_file_action(
        request,
        action="file.renamed",
        storage=storage,
        path=str(result["new_path"]),
        details={"old_path": result["old_path"]},
    )
    _refresh_latest_storage_directory(storage, str(result["directory_path"]))
    return redirect(redirect_to)


@require_POST
@app_login_required
def inflate_storage_file_view(request, storage_id: str):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    storage = get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)
    redirect_to = _safe_next_url(request)
    target_preallocation = request.POST.get("target_preallocation") or INFLATE_PREALLOCATION_FULL
    if target_preallocation not in INFLATE_PREALLOCATION_MODES:
        messages.error(request, "Unknown inflate target.")
        return redirect(redirect_to)

    latest_scan = _latest_storage_result_scan(storage)
    requested_path = _normalize_browser_path(request.POST.get("path", ""))
    entry = get_object_or_404(
        FileInventory,
        scan_run=latest_scan,
        storage=storage,
        path=requested_path,
        entry_type=FileInventory.EntryType.FILE,
    )
    risk = file_action_risk(entry, block_running_guests=False)

    try:
        _require_file_action_confirmations(request, risk)
        validate_inflate_storage_file(
            storage=storage,
            entry=entry,
            target_preallocation=target_preallocation,
            validate_owner_locally=not settings.STORAGE_INFLATE_WORKER_PRESERVES_OWNER,
        )
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_to)

    task_id = async_task(
        "core.tasks.inflate_storage_file_task",
        storage.id,
        entry.id,
        request.user.get_username() if request.user.is_authenticated else "",
        target_preallocation,
    )
    _audit_file_action(
        request,
        action="file.inflate_queued",
        storage=storage,
        path=entry.path,
        details={"task_id": task_id, "target_preallocation": target_preallocation},
    )
    return redirect(redirect_to)


@require_POST
@app_login_required
def restore_storage_file(request, trash_item_id: int):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    item = get_object_or_404(TrashItem, pk=trash_item_id)
    redirect_to = _safe_next_url(request)
    try:
        result = restore_trash_item(item=item)
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_to)

    _audit_file_action(
        request,
        action="file.restored",
        storage=result["storage"],
        path=str(result["path"]),
        details={"trash_item": item.id},
    )
    _refresh_latest_storage_directory(result["storage"], _parent_path(str(result["path"])))
    if result.get("entry_type") == FileInventory.EntryType.DIRECTORY:
        _refresh_latest_storage_directory(result["storage"], str(result["path"]))
    _refresh_latest_storage_directory(result["storage"], _parent_path(str(result["trash_path"])))
    return redirect(redirect_to)


@require_POST
@app_login_required
def purge_trash_item(request, trash_item_id: int):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    item = get_object_or_404(TrashItem, pk=trash_item_id, restore_status=TrashItem.RestoreStatus.TRASHED)
    redirect_to = _safe_next_url(request)
    try:
        result = purge_trash_item_action(item=item)
    except StorageActionError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_to)

    _audit_file_action(
        request,
        action="file.purged",
        storage=result["storage"],
        path=str(result["path"]),
        details={"trash_item": item.id, "trash_path": result["trash_path"]},
    )
    _refresh_latest_storage_directory(result["storage"], _parent_path(str(result["trash_path"])))
    return redirect(redirect_to)


@app_login_required
def orphan_finder(request):
    latest_scan = _latest_result_scan()
    files = _current_orphan_files()
    _decorate_orphan_files_with_action_state(files)
    context = {
        **navigation_context("orphans"),
        "latest_scan": latest_scan,
        "files": files,
    }
    return render(request, "core/orphan_finder.html", context)


@app_login_required
def audit_log(request):
    try:
        audit_page = int(request.GET.get("page", "0"))
    except ValueError:
        audit_page = 0
    audit_page = max(0, audit_page)
    event_total = AuditEvent.objects.count()
    max_page = (event_total - 1) // AUDIT_PAGE_SIZE if event_total else 0
    audit_page = min(audit_page, max_page)
    event_offset = audit_page * AUDIT_PAGE_SIZE
    events = list(AuditEvent.objects.order_by("-timestamp")[event_offset:event_offset + AUDIT_PAGE_SIZE])
    _decorate_audit_events(events)
    context = {
        **navigation_context("audit"),
        "events": events,
        "audit_page": audit_page,
        "audit_has_prev": audit_page > 0,
        "audit_has_next": event_offset + len(events) < event_total,
        "audit_start": event_offset + 1 if event_total else 0,
        "audit_end": event_offset + len(events),
        "audit_total": event_total,
        "audit_retention_schedule": audit_retention_schedule_state(),
        "audit_filters": [
            {"key": "all", "label": "All"},
            {"key": "auth", "label": "Auth"},
            {"key": "clusters", "label": "Clusters"},
            {"key": "vms", "label": "VMs"},
            {"key": "storage", "label": "Storage"},
            {"key": "network", "label": "Network"},
            {"key": "system", "label": "System"},
        ],
    }
    return render(request, "core/audit_log.html", context)


@app_login_required
def recent_tasks(request):
    try:
        page = int(request.GET.get("page", "0"))
    except ValueError:
        page = 0

    return JsonResponse(serialize_task_page(recent_task_page(page=page)))


@app_login_required
def scan_status(request):
    active_scan = _active_scan()
    latest_scan = active_scan or ScanRun.objects.order_by("-created_at").first()
    return JsonResponse(
        {
            "active": active_scan is not None,
            "status": latest_scan.status if latest_scan else "",
            "status_label": latest_scan.get_status_display() if latest_scan else "",
            "button_label": _scan_button_label(active_scan),
            "progress": latest_scan.progress_message if latest_scan else "",
        }
    )


@require_POST
@app_login_required
def update_scan_schedule_view(request):
    enabled = request.POST.get("enabled") == "on"
    try:
        interval_minutes = int(request.POST.get("interval_minutes", "60"))
        state = update_scan_schedule(enabled=enabled, interval_minutes=interval_minutes)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("core:dashboard")

    AuditEvent.objects.create(
        user=request.user if request.user.is_authenticated else None,
        username=request.user.get_username() if request.user.is_authenticated else "",
        action="scan.schedule.updated",
        object_type="scan_schedule",
        object_id="automatic-storage-scan",
        outcome="success",
        details={
            "enabled": state.enabled,
            "interval_minutes": state.interval_minutes,
            "next_run": state.next_run.isoformat() if state.next_run else "",
        },
    )

    return redirect("core:dashboard")


@require_POST
@app_login_required
def update_trash_purge_schedule_view(request):
    enabled = request.POST.get("enabled") == "on"
    try:
        max_age_days = int(request.POST.get("max_age_days", "30"))
        state = update_trash_purge_schedule(enabled=enabled, max_age_days=max_age_days)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("core:dashboard")

    AuditEvent.objects.create(
        user=request.user if request.user.is_authenticated else None,
        username=request.user.get_username() if request.user.is_authenticated else "",
        action="trash.purge.schedule.updated",
        object_type="trash_purge_schedule",
        object_id="automatic-trash-purge",
        outcome="success",
        details={
            "enabled": state.enabled,
            "max_age_days": state.max_age_days,
            "next_run": state.next_run.isoformat() if state.next_run else "",
        },
    )

    return redirect("core:dashboard")


@require_POST
@app_login_required
def update_audit_retention_schedule_view(request):
    redirect_to = _safe_next_url(request)
    enabled = request.POST.get("enabled") == "on"
    try:
        retention_days = int(request.POST.get("retention_days", "90"))
        state = update_audit_retention_schedule(enabled=enabled, retention_days=retention_days)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_to)

    AuditEvent.objects.create(
        user=request.user if request.user.is_authenticated else None,
        username=request.user.get_username() if request.user.is_authenticated else "",
        action="audit.retention.schedule.updated",
        object_type="audit_retention_schedule",
        object_id="automatic-audit-retention",
        outcome="success",
        details={
            "enabled": state.enabled,
            "retention_days": state.retention_days,
            "next_run": state.next_run.isoformat() if state.next_run else "",
        },
    )

    return redirect(redirect_to)


@require_POST
@app_login_required
def start_scan(request):
    redirect_to = _safe_next_url(request)
    active_scan = _active_scan()
    if active_scan:
        AuditEvent.objects.create(
            user=request.user if request.user.is_authenticated else None,
            username=request.user.get_username() if request.user.is_authenticated else "",
            action="scan.manual.skipped",
            object_type="scan_run",
            object_id=str(active_scan.id),
            outcome="skipped",
            details={"reason": "A scan is already queued or running."},
        )
        return redirect(redirect_to)

    target_storage = _requested_scan_storage(request)
    scan = ScanRun.objects.create(
        progress_message="Queued from UI",
        target_storage=target_storage,
        target_label=target_storage.display_name if target_storage else "",
    )
    task_id = async_task("core.tasks.run_scan", scan.id)
    scan.queued_task_id = task_id
    scan.save(update_fields=["queued_task_id", "updated_at"])

    AuditEvent.objects.create(
        user=request.user if request.user.is_authenticated else None,
        username=request.user.get_username() if request.user.is_authenticated else "",
        action="scan.queued",
        object_type="scan_run",
        object_id=str(scan.id),
        outcome="success",
        details={
            "task_id": task_id,
            "target_storage": target_storage.storage_id if target_storage else "",
            "target_label": target_storage.display_name if target_storage else "All storages",
        },
    )
    return redirect(redirect_to)


def health_live(_request):
    return JsonResponse({"status": "ok", "service": "pve-helper"})


def health_ready(_request):
    checks = {"database": "unknown"}
    status = 200
    try:
        connection.ensure_connection()
        checks["database"] = "ok"
    except Exception as exc:  # pragma: no cover - defensive health endpoint
        checks["database"] = "error"
        checks["database_error"] = exc.__class__.__name__
        status = 503

    return JsonResponse({"status": "ok" if status == 200 else "error", "checks": checks}, status=status)


def _classification_counts(queryset) -> dict[str, int]:
    return {
        item["classification"]: item["count"]
        for item in queryset.values("classification").order_by().annotate(count=Count("id"))
    }


def _current_classification_counts(storages: list[StorageMount]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for storage in storages:
        scan = _latest_storage_result_scan(storage)
        if not scan:
            continue
        for classification, count in _classification_counts(
            FileInventory.objects.filter(scan_run=scan, storage=storage)
        ).items():
            totals[classification] = totals.get(classification, 0) + count
    return totals


def _current_orphan_files() -> list[FileInventory]:
    files = []
    for storage in StorageMount.objects.filter(enabled=True).order_by("display_name"):
        scan = _latest_storage_result_scan(storage)
        if not scan:
            continue
        files.extend(
            FileInventory.objects.select_related("storage", "scan_run")
            .filter(
                scan_run=scan,
                storage=storage,
                classification=FileInventory.Classification.LIKELY_ORPHAN,
            )
            .order_by("storage__display_name", "path")[:200]
        )
    return sorted(files, key=lambda item: (item.storage.display_name, item.path))[:200]


def _storage_gate_rows(storages: list[StorageMount], result_scan: ScanRun | None) -> list[dict[str, object]]:
    if not result_scan:
        return []

    rows = []
    gate_status = result_scan.storage_gate_status or {}
    for storage in storages:
        rows.append(
            {
                "storage": storage,
                "gate": gate_status.get(storage.storage_id, {}),
                "latest_scan_at": storage.latest_scan_at,
            }
        )
    return rows


def _decorate_audit_events(events: list[AuditEvent]) -> None:
    for event in events:
        event.display_module_key = _audit_module_key(event)
        event.display_module = _audit_module_label(event.display_module_key)
        event.display_action = _audit_action_label(event)
        event.guest_identity = _audit_guest_identity(event)
        event.display_object = _audit_object_label(event)
        event.search_text = " ".join(
            [
                event.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                event.username or "",
                event.display_module,
                event.display_action,
                event.display_object,
                event.outcome or "",
            ]
        )


def _audit_guest_identity(event: AuditEvent):
    if event.object_type != "guest":
        return None
    details = event.details if isinstance(event.details, dict) else {}
    target_type = details.get("target_type")
    vmid = details.get("vmid")
    if not target_type or vmid is None:
        raw_type, separator, raw_vmid = str(event.object_id or "").partition(":")
        if separator == ":":
            target_type = target_type or raw_type
            vmid = vmid if vmid is not None else raw_vmid
    return guest_identity(target_type, vmid, details.get("name") or "")


def _audit_module_key(event: AuditEvent) -> str:
    details = event.details if isinstance(event.details, dict) else {}
    action = event.action or ""
    object_type = event.object_type or ""

    if action.startswith("auth."):
        return "auth"
    if action.startswith("network.") or object_type.startswith("network"):
        return "network"
    if action.startswith("vm.") or action.startswith("scheduled_action.") or object_type in {"vm", "ct", "guest", "scheduled_action", "scheduled_action_run"}:
        return "vms"
    if action.startswith("cluster.") or object_type.startswith("cluster"):
        return "clusters"
    if (
        action.startswith("scan.")
        or action.startswith("file.")
        or action.startswith("trash.")
        or object_type in {"scan_run", "scan_schedule", "storage", "file"}
        or details.get("target_storage")
    ):
        return "storage"
    return "system"


def _audit_module_label(module_key: str) -> str:
    return {
        "auth": "Auth",
        "clusters": "Clusters",
        "network": "Network",
        "storage": "Storage",
        "system": "System",
        "vms": "VMs",
    }.get(module_key, "System")


def _audit_action_label(event: AuditEvent) -> str:
    details = event.details if isinstance(event.details, dict) else {}
    if event.action == "auth.login":
        return "Login"
    if event.action == "auth.logout":
        return "Logout"
    if event.action == "auth.login_failed":
        return "Login failed"
    if event.action == "scan.queued" and details.get("source") == "schedule":
        interval = details.get("interval_minutes")
        if interval:
            return f"Scheduled full scan ({interval} min)"
        return "Scheduled full scan"
    if event.action == "scan.queued":
        target = details.get("target_label")
        if target and target != "All storages":
            return f"Manual storage scan ({target})"
        return "Manual full scan"
    if event.action == "scan.completed":
        target = details.get("target_label")
        if target and target != "All storages":
            return f"Storage scan completed ({target})"
        return "Full scan completed"
    if event.action == "scan.failed":
        target = details.get("target_label")
        if target and target != "All storages":
            return f"Storage scan failed ({target})"
        return "Full scan failed"
    if event.action == "scan.schedule.skipped":
        return "Scheduled scan skipped"
    if event.action == "scan.schedule.updated":
        return "Scan schedule updated"
    if event.action == "scan.manual.skipped":
        return "Manual scan skipped"
    if event.action == "scan.retention.purge":
        return "Scan retention purge"
    if event.action == "scan.retention.purge_failed":
        return "Scan retention purge failed"
    if event.action == "file.downloaded":
        return "Download file"
    if event.action == "file.folder_created":
        return "Create folder"
    if event.action == "file.uploaded":
        return "Upload file"
    if event.action == "file.folder_uploaded":
        return "Upload folder"
    if event.action == "file.upload_normalized":
        return "Normalize uploaded disk metadata"
    if event.action == "file.upload_normalize_failed":
        return "Normalize uploaded disk metadata failed"
    if event.action == "file.moved":
        return "Move file"
    if event.action == "file.renamed":
        return "Rename file"
    if event.action == "file.trashed":
        return "Move file to trash"
    if event.action == "file.restored":
        return "Restore file"
    if event.action == "file.inflate_queued":
        return _inflate_action_label("Disk inflate queued", details)
    if event.action == "file.inflated":
        return _inflate_action_label("Inflate disk", details)
    if event.action == "file.inflate_failed":
        return _inflate_action_label("Inflate disk failed", details)
    if event.action == "trash.purge":
        return "Recycle Bin purge"
    if event.action == "trash.purge.schedule.updated":
        return "Recycle Bin purge schedule updated"
    if event.action == "audit.retention.purge":
        return "Audit retention purge"
    if event.action == "audit.retention.schedule.updated":
        return "Audit retention schedule updated"
    guest_action_labels = {
        "guest.power.start": "Power on guest",
        "guest.power.shutdown": "Shut down guest OS",
        "guest.power.reboot": "Restart guest OS",
        "guest.power.stop": "Power off guest",
        "guest.power.reset": "Reset guest",
        "guest.snapshot.create": "Create snapshot",
        "guest.snapshot.delete": "Delete snapshot",
        "guest.snapshot.delete_all": "Delete all snapshots",
        "guest.snapshot.rollback": "Roll back snapshot",
        "guest.template.convert": "Convert guest to template",
        "guest.clone.create": "Clone guest",
        "guest.tags.updated": "Update guest tags",
        "guest.destroy": "Destroy guest",
        "guest.config.updated": "Update guest configuration",
        "guest.hardware.updated": "Update guest hardware",
        "guest.cloudinit.update": "Update Cloud-Init",
        "guest.create": "Create guest",
        "guest.firewall.options": "Update firewall options",
        "guest.firewall.rule_add": "Add firewall rule",
        "guest.firewall.rule_delete": "Delete firewall rule",
        "guest.firewall.rule_toggle": "Toggle firewall rule",
        "guest.backup.run": "Run backup",
        "guest.backup.delete": "Delete backup",
        "guest.replication.create": "Create replication job",
        "guest.replication.delete": "Delete replication job",
    }
    if event.action in guest_action_labels:
        return guest_action_labels[event.action]
    if event.action == "scheduled_action.created":
        return "Scheduled task created"
    if event.action == "scheduled_action.updated":
        return "Scheduled task updated"
    if event.action == "scheduled_action.enabled":
        return "Scheduled task enabled"
    if event.action == "scheduled_action.disabled":
        return "Scheduled task disabled"
    if event.action == "scheduled_action.deleted":
        return "Scheduled task deleted"
    if event.action == "scheduled_action.run_queued":
        return "Scheduled task queued"
    if event.action == "scheduled_action.run_started":
        return "Scheduled task started"
    if event.action == "scheduled_action.run_completed":
        return "Scheduled task completed"
    if event.action == "scheduled_action.run_failed":
        return "Scheduled task failed"
    if event.action == "scheduled_action.run_skipped":
        return "Scheduled task skipped"
    if event.action == "scheduled_action.run_missed":
        return "Scheduled task missed"
    if event.action == "scheduled_action.run_retention.purge":
        return "Scheduled task retention purge"
    return event.action


def _inflate_action_label(base_label: str, details: dict) -> str:
    target_preallocation = details.get("target_preallocation")
    if target_preallocation:
        return f"{base_label} ({target_preallocation})"
    return base_label


def _audit_object_label(event: AuditEvent) -> str:
    details = event.details if isinstance(event.details, dict) else {}
    if event.object_type == "guest":
        target_type = details.get("target_type")
        vmid = details.get("vmid")
        if not target_type or vmid is None:
            raw_type, separator, raw_vmid = str(event.object_id or "").partition(":")
            if separator == ":":
                target_type = target_type or raw_type
                vmid = vmid if vmid is not None else raw_vmid
        return guest_identity(target_type, vmid, details.get("name") or "").full_label_with_type
    if event.object_type == "scan_run" and event.object_id:
        return "Storage inventory scan"
    if event.object_type == "scan_retention":
        return "Scan retention"
    if event.object_type == "scan_schedule":
        return "Automatic scan schedule"
    if event.object_type == "trash_purge_schedule":
        return "Recycle Bin purge schedule"
    if event.object_type == "trash":
        return "Recycle Bin"
    if event.object_type == "audit_retention_schedule":
        return "Audit retention schedule"
    if event.object_type == "audit_retention":
        return "Audit retention"
    if event.object_type in {"scheduled_action", "scheduled_action_run"}:
        name = details.get("scheduled_action_name")
        if name:
            return str(name)
        target_type = details.get("target_type")
        target_vmid = details.get("target_vmid")
        if target_type and target_vmid:
            return f"{str(target_type).upper()} {target_vmid}"
        return "Scheduled task"
    return f"{event.object_type} {event.object_id}".strip() or "-"


def _scan_timestamp(scan: ScanRun | None):
    if not scan:
        return None
    return scan.filesystem_scan_at or scan.finished_at or scan.created_at


def _latest_result_scan() -> ScanRun | None:
    return (
        ScanRun.objects.filter(status=ScanRun.Status.COMPLETED)
        .exclude(storage_gate_status={})
        .order_by("-finished_at", "-created_at")
        .first()
    )


def _latest_storage_result_scan(storage: StorageMount) -> ScanRun | None:
    return (
        ScanRun.objects.filter(status=ScanRun.Status.COMPLETED)
        .filter(Q(target_storage=storage) | Q(target_storage__isnull=True))
        .order_by("-filesystem_scan_at", "-finished_at", "-created_at")
        .first()
    )


def _active_scan() -> ScanRun | None:
    return (
        ScanRun.objects.filter(status__in=[ScanRun.Status.QUEUED, ScanRun.Status.RUNNING])
        .order_by("-created_at")
        .first()
    )


def _decorate_storage_with_space_info(storage: StorageMount) -> None:
    storage.space_info = storage_space_info(storage.path)
    storage.storage_actions_enabled = settings.STORAGE_WRITE_ENABLED and storage.space_info.can_write
    storage.details = storage_details(storage, _latest_storage_result_scan(storage), storage.space_info)


def _refresh_latest_storage_directory(storage: StorageMount, directory_path: str = "") -> None:
    latest_scan = _latest_storage_result_scan(storage)
    if latest_scan is None:
        return
    refresh_storage_directory(storage=storage, scan=latest_scan, directory_path=directory_path)


def _prune_latest_storage_path(storage: StorageMount, path: str) -> None:
    latest_scan = _latest_storage_result_scan(storage)
    if latest_scan is None:
        return
    prefix = f"{path}/"
    FileInventory.objects.filter(scan_run=latest_scan, storage=storage).filter(Q(path=path) | Q(path__startswith=prefix)).delete()


def _decorate_orphan_files_with_action_state(files: list[FileInventory]) -> None:
    storages: dict[int, StorageMount] = {}
    for file in files:
        if file.storage_id not in storages:
            _decorate_storage_with_space_info(file.storage)
            storages[file.storage_id] = file.storage
        file.storage = storages[file.storage_id]
        _decorate_browser_entry(file)


def _scan_button_label(active_scan: ScanRun | None) -> str:
    if not active_scan:
        return "Start scan"
    if active_scan.status == ScanRun.Status.QUEUED:
        return "Scan queued"
    return "Scanning"


def _requested_scan_storage(request) -> StorageMount | None:
    storage_id = request.POST.get("storage_id", "").strip()
    if not storage_id:
        return None
    return get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)


def _safe_next_url(request) -> str:
    next_url = request.POST.get("next", "").strip()
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return next_url
    return reverse("core:dashboard")


def _storage_browser_url(storage: StorageMount, path: str = "", **params: object) -> str:
    url = reverse("core:storage_browser", args=[storage.storage_id])
    query = {}
    if path:
        query["path"] = path
    for key, value in params.items():
        if value in ("", None):
            continue
        query[key] = value
    if query:
        return f"{url}?{urlencode(query)}"
    return url


def _int_request_param(request, name: str, default: int) -> int:
    try:
        return int(request.GET.get(name, default))
    except (TypeError, ValueError):
        return default


def _storage_directory_or_404(storage: StorageMount, latest_scan: ScanRun | None, path: str) -> None:
    if latest_scan is None:
        raise Http404("No storage inventory has been scanned yet.")
    exists = FileInventory.objects.filter(
        scan_run=latest_scan,
        storage=storage,
        path=path,
        entry_type=FileInventory.EntryType.DIRECTORY,
    ).exists()
    if not exists:
        raise Http404("Directory not found in latest scan.")


def _audit_file_action(request, *, action: str, storage: StorageMount, path: str, details: dict[str, object]) -> None:
    AuditEvent.objects.create(
        user=request.user if request.user.is_authenticated else None,
        username=request.user.get_username() if request.user.is_authenticated else "",
        source_ip=_client_ip(request),
        action=action,
        object_type="file",
        object_id=f"{storage.storage_id}:{path}",
        outcome="success",
        details={
            "storage_id": storage.storage_id,
            "storage_name": storage.display_name,
            "path": path,
            **details,
        },
    )


def _queue_upload_normalization(storage: StorageMount, paths: list[str], user) -> None:
    image_paths = [path for path in paths if _is_proxmox_image_upload_path(path)]
    if not image_paths:
        return
    async_task(
        "core.tasks.normalize_uploaded_proxmox_image_paths_task",
        storage.id,
        image_paths,
        user.get_username() if getattr(user, "is_authenticated", False) else "",
    )


def _is_proxmox_image_upload_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return len(parts) >= 3 and parts[0] == "images" and parts[1].isdigit()


def _require_file_action_confirmations(request, risk: FileActionRisk) -> None:
    if risk.blocked:
        raise StorageActionError(risk.warning_message)
    if request.POST.get("confirm_basic") != "yes":
        raise StorageActionError("File action was not confirmed.")
    if risk.requires_extra_confirmation and request.POST.get("confirm_risk") != "yes":
        raise StorageActionError("Risk confirmation was not confirmed.")


def _require_file_action_confirmations_for_entries(request, entries: list[FileInventory]) -> None:
    risks = [file_action_risk(entry) for entry in entries]
    blocked_risk = next((risk for risk in risks if risk.blocked), None)
    if blocked_risk:
        raise StorageActionError(blocked_risk.warning_message)
    if request.POST.get("confirm_basic") != "yes":
        raise StorageActionError("File action was not confirmed.")
    if any(risk.requires_extra_confirmation for risk in risks) and request.POST.get("confirm_risk") != "yes":
        raise StorageActionError("Risk confirmation was not confirmed.")


def _selected_storage_file_entries(
    request,
    *,
    storage: StorageMount,
    latest_scan: ScanRun | None,
    entry_types: list[str] | None = None,
) -> list[FileInventory]:
    paths: list[str] = []
    seen: set[str] = set()
    for raw_path in request.POST.getlist("path"):
        path = _normalize_browser_path(raw_path)
        if not path or path in seen:
            continue
        paths.append(path)
        seen.add(path)
    if not paths:
        raise Http404("File not found.")

    entry_types = entry_types or [FileInventory.EntryType.FILE]
    entries_by_path = {
        entry.path: entry
        for entry in FileInventory.objects.filter(
            scan_run=latest_scan,
            storage=storage,
            path__in=paths,
            entry_type__in=entry_types,
        )
    }
    if len(entries_by_path) != len(paths):
        raise Http404("File not found.")
    return [entries_by_path[path] for path in paths]


def _storage_write_disabled_response() -> HttpResponseForbidden:
    return HttpResponseForbidden("Storage write actions are disabled.")


def _is_async_upload_request(request) -> bool:
    return request.headers.get("X-PVE-Helper-Async-Upload") == "1"


def _upload_success_response(request, redirect_to: str):
    if _is_async_upload_request(request):
        return JsonResponse({"ok": True, "redirect": redirect_to})
    return redirect(redirect_to)


def _upload_error_response(request, redirect_to: str, message: str):
    if _is_async_upload_request(request):
        return JsonResponse({"ok": False, "error": message, "redirect": redirect_to}, status=400)
    messages.error(request, message)
    return redirect(redirect_to)


def _resolve_storage_file(storage: StorageMount, relative_path: str) -> Path:
    root = Path(storage.path).resolve(strict=True)
    candidate = root.joinpath(*PurePosixPath(relative_path).parts).resolve(strict=True)

    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise Http404("File not found.")
    return candidate


def _download_response(request, storage: StorageMount, relative_path: str, absolute_path: Path):
    if settings.STORAGE_DOWNLOAD_ACCEL_ENABLED:
        response = HttpResponse(content_type="application/octet-stream")
        response["X-Accel-Redirect"] = _download_accel_uri(storage, relative_path)
        _decorate_download_response(response, absolute_path)
        return response

    file_size = absolute_path.stat().st_size
    range_header = request.headers.get("Range", "")
    if range_header:
        try:
            byte_range = _parse_http_byte_range(range_header, file_size)
        except ValueError:
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{file_size}"
            response["Accept-Ranges"] = "bytes"
            return response

        if byte_range is not None:
            start, end = byte_range
            length = end - start + 1
            response = StreamingHttpResponse(
                _file_range_iterator(absolute_path, start=start, length=length),
                status=206,
                content_type="application/octet-stream",
            )
            response["Content-Range"] = f"bytes {start}-{end}/{file_size}"
            response["Content-Length"] = str(length)
            _decorate_download_response(response, absolute_path)
            return response

    response = FileResponse(
        absolute_path.open("rb"),
        as_attachment=True,
        filename=absolute_path.name,
    )
    response.block_size = 1024 * 1024
    response["Accept-Ranges"] = "bytes"
    response["X-Accel-Buffering"] = "no"
    return response


def _decorate_download_response(response, absolute_path: Path) -> None:
    response["Accept-Ranges"] = "bytes"
    response["X-Accel-Buffering"] = "no"
    response["Content-Disposition"] = content_disposition_header(True, absolute_path.name)


def _download_accel_uri(storage: StorageMount, relative_path: str) -> str:
    prefix = settings.STORAGE_DOWNLOAD_ACCEL_PREFIX.rstrip("/")
    storage_id = quote(storage.storage_id, safe="")
    path = quote(PurePosixPath(relative_path).as_posix(), safe="/")
    return f"{prefix}/{storage_id}/{path}"


def _parse_http_byte_range(range_header: str, file_size: int) -> tuple[int, int] | None:
    units, separator, value = range_header.partition("=")
    if units.strip().lower() != "bytes" or separator != "=" or "," in value:
        return None

    start_text, separator, end_text = value.strip().partition("-")
    if separator != "-":
        return None
    if not start_text and not end_text:
        raise ValueError("empty range")

    if start_text:
        start = int(start_text)
        end = int(end_text) if end_text else file_size - 1
    else:
        suffix_length = int(end_text)
        if suffix_length <= 0:
            raise ValueError("invalid suffix range")
        start = max(file_size - suffix_length, 0)
        end = file_size - 1

    if start < 0 or end < start or start >= file_size:
        raise ValueError("unsatisfiable range")
    return start, min(end, file_size - 1)


def _file_range_iterator(absolute_path: Path, *, start: int, length: int):
    remaining = length
    with absolute_path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def _normalize_browser_path(raw_path: str) -> str:
    path = (raw_path or "").strip().strip("/")
    if not path:
        return ""

    parts = PurePosixPath(path).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise Http404("Invalid storage path.")
    return PurePosixPath(*parts).as_posix()


def _client_ip(request) -> str | None:
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


def _parent_path(path: str) -> str:
    if not path or "/" not in path:
        return ""
    return path.rsplit("/", 1)[0]


def _browser_breadcrumbs(path: str) -> list[dict[str, str]]:
    breadcrumbs = [{"label": "Root", "path": ""}]
    if not path:
        return breadcrumbs

    current = []
    for part in path.split("/"):
        current.append(part)
        breadcrumbs.append({"label": part, "path": "/".join(current)})
    return breadcrumbs


def _browser_folder_tree(
    scan: ScanRun,
    storage: StorageMount,
    current_path: str,
    *,
    ignored_paths: set[str] | None = None,
) -> list[dict[str, object]]:
    ignored_paths = ignored_paths or set()
    directory_paths = sorted(
        set(
            path
            for path in (
                FileInventory.objects.filter(
                    scan_run=scan,
                    storage=storage,
                    entry_type=FileInventory.EntryType.DIRECTORY,
                )
                .order_by("path")
                .values_list("path", flat=True)
            )
            if not is_ignored_storage_path(path, ignored_paths)
        ),
        key=lambda item: [part.lower() for part in item.split("/")],
    )
    directory_path_set = set(directory_paths)
    expanded_paths = {""}
    if current_path:
        current_parts = current_path.split("/")
        expanded_paths.update(
            "/".join(current_parts[:index]) for index in range(1, len(current_parts) + 1)
        )

    def has_children(path: str) -> bool:
        if not path:
            return bool(directory_paths)
        return any(candidate.startswith(f"{path}/") for candidate in directory_path_set)

    def is_initially_visible(path: str) -> bool:
        if not path:
            return True
        parts = path.split("/")
        return all(
            "/".join(parts[:index]) in expanded_paths for index in range(0, len(parts))
        )

    nodes = [
        {
            "name": storage.display_name,
            "path": "",
            "depth": 0,
            "is_current": current_path == "",
            "is_ancestor": bool(current_path),
            "is_expanded": "" in expanded_paths,
            "is_initially_visible": True,
            "has_children": has_children(""),
        }
    ]
    for path in directory_paths:
        parts = path.split("/")
        nodes.append(
            {
                "name": parts[-1],
                "path": path,
                "depth": len(parts),
                "is_current": path == current_path,
                "is_ancestor": bool(current_path) and current_path.startswith(f"{path}/"),
                "is_expanded": path in expanded_paths,
                "is_initially_visible": is_initially_visible(path),
                "has_children": has_children(path),
            }
        )
    return nodes


def _decorate_browser_entry(entry: FileInventory) -> None:
    entry.classification_label = _classification_label(entry)
    entry.classification_class = _classification_class(entry)
    entry.category_label = _content_category_label(entry.content_category, entry.path)
    image_info = (entry.evidence or {}).get("image_info") or {}
    entry.image_format = image_info.get("format", "")
    entry.virtual_size_bytes = image_info.get("virtual_size_bytes") or entry.size_bytes
    entry.disk_size_bytes = image_info.get("disk_size_bytes")
    entry.image_info_error = image_info.get("error", "")
    entry.qcow2_allocation_percent = image_info.get("qcow2_allocation_percent")
    if not isinstance(entry.qcow2_allocation_percent, (int, float)):
        entry.qcow2_allocation_percent = None
    entry.qcow2_allocation_error = image_info.get("qcow2_allocation_error", "")
    entry.qcow2_allocation_title = ""
    if entry.qcow2_allocation_percent is not None:
        allocated_clusters = image_info.get("qcow2_allocated_clusters")
        total_clusters = image_info.get("qcow2_total_clusters")
        if isinstance(allocated_clusters, int) and isinstance(total_clusters, int):
            entry.qcow2_allocation_title = f"{allocated_clusters} of {total_clusters} qcow2 clusters mapped"
    entry.has_qcow2_full_allocation = (
        entry.qcow2_allocation_percent is not None
        and entry.qcow2_allocation_percent >= MIN_INFLATE_ALLOCATED_PERCENT
    )
    entry.full_inflate_already_recorded = (
        entry.entry_type == FileInventory.EntryType.FILE
        and full_inflate_already_recorded(
            entry,
            current_virtual_size_bytes=entry.virtual_size_bytes
            if isinstance(entry.virtual_size_bytes, int)
            else None,
        )
    )
    entry.has_thin_usage = (
        entry.disk_size_bytes is not None
        and entry.virtual_size_bytes is not None
        and entry.disk_size_bytes != entry.virtual_size_bytes
    )
    entry.action_risk = file_action_risk(entry)
    entry.inflate_action_risk = file_action_risk(entry, block_running_guests=False)
    entry.can_trash = entry.entry_type in {FileInventory.EntryType.FILE, FileInventory.EntryType.DIRECTORY} and not entry.action_risk.blocked
    entry.can_rename = entry.entry_type == FileInventory.EntryType.FILE and entry.can_trash
    entry.can_inflate_action = (
        entry.entry_type == FileInventory.EntryType.FILE
        and not entry.inflate_action_risk.blocked
    )
    entry.can_inflate_metadata = (
        entry.can_inflate_action
        and entry.content_category == "vm_disk"
        and entry.image_format == "qcow2"
        and entry.qcow2_allocation_percent is not None
        and entry.qcow2_allocation_percent < MIN_INFLATE_ALLOCATED_PERCENT
    )
    entry.can_inflate_full = (
        entry.can_inflate_action
        and entry.content_category == "vm_disk"
        and entry.image_format == "qcow2"
        and entry.virtual_size_bytes is not None
        and entry.disk_size_bytes is not None
        and entry.qcow2_allocation_percent is not None
        and not entry.full_inflate_already_recorded
    )
    entry.can_inflate = entry.can_inflate_metadata or entry.can_inflate_full
    entry.action_blocked = entry.entry_type in {FileInventory.EntryType.FILE, FileInventory.EntryType.DIRECTORY} and entry.action_risk.blocked
    entry.action_warning_message = entry.action_risk.warning_message
    entry.action_requires_extra_confirmation = entry.action_risk.requires_extra_confirmation
    entry.inflate_warning_message = entry.inflate_action_risk.warning_message
    entry.inflate_requires_extra_confirmation = entry.inflate_action_risk.requires_extra_confirmation


def _classification_label(entry: FileInventory) -> str:
    return entry.get_classification_display()


def _classification_class(entry: FileInventory) -> str:
    return entry.classification


def _content_category_label(category: str, path: str) -> str:
    if category == "unknown":
        if path == "images":
            return "VM images"
        if path.startswith("images/"):
            return "VM image directory"
        if path == "template":
            return "Templates"

    labels = {
        "backup": "Backups",
        "base_image": "Base image",
        "ct_private": "CT private data",
        "ct_template": "CT templates",
        "iso": "ISO images",
        "snippet": "Snippets",
        "template_directory": "Templates",
        "trash": "Trash",
        "vm_disk": "VM disk",
        "vm_image_directory": "VM image directory",
        "vm_images": "VM images",
    }
    return labels.get(category, "Other / unknown")


def _decorate_guests_with_scheduled_actions(guests: list[ProxmoxInventory]) -> None:
    vmids = [guest.vmid for guest in guests if guest.object_type == ProxmoxInventory.ObjectType.VM and guest.vmid]
    ctids = [guest.vmid for guest in guests if guest.object_type == ProxmoxInventory.ObjectType.CT and guest.vmid]
    action_filter = Q()
    if vmids:
        action_filter |= Q(target_type=ScheduledAction.TargetType.VM, target_vmid__in=vmids)
    if ctids:
        action_filter |= Q(target_type=ScheduledAction.TargetType.CT, target_vmid__in=ctids)

    actions_by_target: dict[tuple[str, int], list[ScheduledAction]] = {}
    if action_filter:
        actions = ScheduledAction.objects.filter(action_filter).order_by("-enabled", "next_run_at", "name")
        for action in actions:
            action.display_schedule = _scheduled_action_schedule_label(action)
            action.display_status_class = _scheduled_action_status_class(action.last_status)
            actions_by_target.setdefault((action.target_type, action.target_vmid), []).append(action)

    for guest in guests:
        target = f"{guest.object_type}:{guest.vmid}"
        guest.scheduled_actions = actions_by_target.get((guest.object_type, guest.vmid), [])
        guest.scheduled_action_count = len(guest.scheduled_actions)
        guest.scheduled_action_search_text = " ".join(action.name for action in guest.scheduled_actions)
        guest.schedule_action_url = f"{reverse('core:scheduled_task_create')}?{urlencode({'target': target})}"
        guest.scheduled_actions_url = f"{reverse('core:scheduled_tasks')}?{urlencode({'target': target})}"


def _scheduled_action_form_context(action: ScheduledAction, *, form_values: dict, errors: list[str], mode: str) -> dict:
    target_choices = _scheduled_action_target_choices(action)
    return {
        **navigation_context("scheduled_tasks"),
        "scheduled_action": action,
        "form_values": form_values,
        "form_errors": errors,
        "form_mode": mode,
        "target_choices": target_choices,
        "action_type_choices": ScheduledAction.ActionType.choices,
        "recurrence_kind_choices": [
            (SCHEDULED_ACTION_RECURRENCE_ONCE, "Once"),
            (ScheduledAction.RecurrenceKind.DAILY, "Daily"),
            (ScheduledAction.RecurrenceKind.WEEKLY, "Weekly"),
            (ScheduledAction.RecurrenceKind.MONTHLY_ORDINAL, "Monthly by weekday"),
            (ScheduledAction.RecurrenceKind.MONTHLY_DAY, "Monthly by date"),
        ],
        "weekday_choices": SCHEDULED_ACTION_WEEKDAYS,
        "ordinal_choices": SCHEDULED_ACTION_ORDINALS,
        "month_choices": SCHEDULED_ACTION_MONTHS,
        "scheduled_actions_enabled": settings.SCHEDULED_ACTIONS_ENABLED,
        "schedule_timezone": settings.TIME_ZONE,
    }


def _scheduled_action_form_values(action: ScheduledAction, post=None) -> dict:
    if post is not None:
        return {
            "name": post.get("name", ""),
            "enabled": post.get("enabled") == "on",
            "action_type": post.get("action_type", ScheduledAction.ActionType.SHUTDOWN),
            "target": post.get("target", ""),
            **_posted_datetime_parts(post, "run"),
            "recurrence_kind": post.get("recurrence_kind", SCHEDULED_ACTION_RECURRENCE_ONCE),
            "weekdays": _posted_values(post, "weekdays", fallback_names=["weekday"], default=["6"]),
            "ordinals": _posted_values(post, "ordinals", fallback_names=["ordinal"], default=["first"]),
            "days_of_month": post.get("days_of_month", post.get("day_of_month", "1")),
            "months": _posted_values(post, "months", default=SCHEDULED_ACTION_DEFAULT_MONTHS),
            "catch_up_enabled": post.get("catch_up_enabled") == "on",
            "max_lateness_hours": post.get("max_lateness_hours", "1"),
            "action_timeout_seconds": post.get("action_timeout_seconds", "1800"),
        }

    recurrence = action.recurrence if isinstance(action.recurrence, dict) else {}
    target = f"{action.target_type}:{action.target_vmid}" if action.target_type and action.target_vmid else ""
    run_at = None
    if action.schedule_type == ScheduledAction.ScheduleType.ONCE:
        run_at = action.run_at or action.next_run_at
    run_date, run_hour, run_minute = _datetime_parts(run_at)
    if action.schedule_type == ScheduledAction.ScheduleType.RECURRING:
        run_hour, run_minute = _time_parts(_recurrence_time_label(recurrence) if action.pk else "22:00")
    recurrence_kind = (
        SCHEDULED_ACTION_RECURRENCE_ONCE
        if action.schedule_type != ScheduledAction.ScheduleType.RECURRING
        else action.recurrence_kind or ScheduledAction.RecurrenceKind.DAILY
    )
    return {
        "name": action.name or "",
        "enabled": action.enabled if action.pk else True,
        "action_type": action.action_type or ScheduledAction.ActionType.SHUTDOWN,
        "target": target,
        "run_date": run_date,
        "run_hour": run_hour,
        "run_minute": run_minute,
        "recurrence_kind": recurrence_kind,
        "weekdays": _recurrence_values(recurrence, "weekdays", "weekday", default=["6"]),
        "ordinals": _recurrence_values(recurrence, "ordinals", "ordinal", "week", default=["first"]),
        "days_of_month": _recurrence_days_label(recurrence),
        "months": _recurrence_values(recurrence, "months", default=SCHEDULED_ACTION_DEFAULT_MONTHS),
        "catch_up_enabled": action.catch_up_policy == ScheduledAction.CatchUpPolicy.RUN_ONCE_LATE,
        "max_lateness_hours": str(max(1, action.max_lateness_minutes // 60) if action.max_lateness_minutes else 1),
        "action_timeout_seconds": str(action.action_timeout_seconds or 1800),
    }


def _scheduled_action_target_choices(action: ScheduledAction | None = None) -> list[dict]:
    choices = []
    seen = set()
    for guest in fetch_live_guest_inventory(use_cache=False):
        key = (guest.object_type, guest.vmid)
        if key in seen:
            continue
        seen.add(key)
        choices.append(
            {
                "value": f"{guest.object_type}:{guest.vmid}",
                "label": _live_guest_target_label(guest),
                "node": guest.node,
            }
        )

    latest_scan = _latest_proxmox_inventory_scan()
    if latest_scan:
        objects = (
            ProxmoxInventory.objects.filter(
                scan_run=latest_scan,
                object_type__in=[ProxmoxInventory.ObjectType.VM, ProxmoxInventory.ObjectType.CT],
                vmid__isnull=False,
            )
            .order_by("object_type", "vmid", "node")
        )
        for obj in objects:
            key = (obj.object_type, obj.vmid)
            if key in seen:
                continue
            seen.add(key)
            choices.append(
                {
                    "value": f"{obj.object_type}:{obj.vmid}",
                    "label": _inventory_target_label(obj),
                    "node": obj.node,
                }
            )

    if action and action.target_type and action.target_vmid:
        value = f"{action.target_type}:{action.target_vmid}"
        if (action.target_type, action.target_vmid) not in seen:
            choices.append(
                {
                    "value": value,
                    "label": _scheduled_action_target_label(action),
                    "node": action.target_node,
                }
            )
    return choices


def _latest_proxmox_inventory_scan() -> ScanRun | None:
    return (
        ScanRun.objects.filter(
            proxmox_objects__object_type__in=[
                ProxmoxInventory.ObjectType.VM,
                ProxmoxInventory.ObjectType.CT,
            ],
            proxmox_objects__vmid__isnull=False,
        )
        .order_by("-created_at")
        .distinct()
        .first()
    )


def _inventory_target_label(obj: ProxmoxInventory) -> str:
    return guest_identity_from_inventory(obj).full_label_with_type


def _live_guest_target_label(guest) -> str:
    return guest_identity_from_inventory(guest).full_label_with_type


def _scheduled_target_label(target_type: str | None, target_vmid: int | None) -> str:
    if target_type is None or target_vmid is None:
        return ""
    obj = (
        ProxmoxInventory.objects.filter(object_type=target_type, vmid=target_vmid)
        .order_by("-scan_run__created_at", "node")
        .first()
    )
    if obj:
        return _inventory_target_label(obj)
    type_label = "VM" if target_type == ScheduledAction.TargetType.VM else "Container"
    return f"{type_label} {target_vmid}"


def _live_guest_for_target(
    target_type: str | None,
    target_vmid: int | None,
    *,
    use_cache: bool,
):
    if target_type is None or target_vmid is None:
        return None
    for guest in fetch_live_guest_inventory(use_cache=use_cache):
        if guest.object_type == target_type and guest.vmid == target_vmid:
            return guest
    return None


def _apply_scheduled_action_form(action: ScheduledAction, post, user) -> list[str]:
    errors: list[str] = []
    name = post.get("name", "").strip()
    if not name:
        errors.append("Name is required.")
    elif ScheduledAction.objects.filter(name=name).exclude(pk=action.pk).exists():
        errors.append("A scheduled task with this name already exists.")
    target_type, target_vmid = _parse_scheduled_target(post.get("target", ""))
    if target_type is None or target_vmid is None:
        errors.append("Target is required.")

    action_type = post.get("action_type", "")
    if action_type not in ScheduledAction.ActionType.values:
        errors.append("Unknown action.")

    try:
        timeout_seconds = _bounded_int(post.get("action_timeout_seconds", "1800"), 30, 86400, "Timeout")
    except ValueError as exc:
        errors.append(str(exc))
        timeout_seconds = 1800

    selected_recurrence = post.get("recurrence_kind", SCHEDULED_ACTION_RECURRENCE_ONCE)
    catch_up_enabled = post.get("catch_up_enabled") == "on" and selected_recurrence != SCHEDULED_ACTION_RECURRENCE_ONCE
    try:
        max_lateness_hours = _bounded_int(post.get("max_lateness_hours", "1"), 1, 24, "Retry window")
    except ValueError as exc:
        errors.append(str(exc))
        max_lateness_hours = 1

    run_at = None
    recurrence = {}
    if selected_recurrence == SCHEDULED_ACTION_RECURRENCE_ONCE:
        schedule_type = ScheduledAction.ScheduleType.ONCE
        try:
            run_at = _parse_local_datetime_from_post(post)
        except ValueError as exc:
            errors.append(str(exc))
        recurrence_kind = ScheduledAction.RecurrenceKind.ADVANCED
    else:
        schedule_type = ScheduledAction.ScheduleType.RECURRING
        recurrence_kind = selected_recurrence
        if recurrence_kind not in ScheduledAction.RecurrenceKind.values:
            errors.append("Unknown recurrence type.")
        try:
            recurrence = _recurrence_from_post(post, recurrence_kind)
        except ValueError as exc:
            errors.append(str(exc))

    if errors:
        return errors

    action.name = name[:160]
    action.enabled = post.get("enabled") == "on"
    action.action_type = action_type
    action.action_timeout_seconds = timeout_seconds
    action.target_type = target_type
    action.target_vmid = target_vmid
    _apply_target_snapshot(action)
    action.schedule_type = schedule_type
    action.run_at = run_at
    action.recurrence = recurrence
    action.recurrence_kind = recurrence_kind
    action.timezone = settings.TIME_ZONE
    action.catch_up_policy = (
        ScheduledAction.CatchUpPolicy.RUN_ONCE_LATE
        if catch_up_enabled
        else ScheduledAction.CatchUpPolicy.SKIP_MISSED
    )
    action.max_lateness_minutes = max_lateness_hours * 60 if catch_up_enabled else 0
    action.parameters = {}
    if not action.pk:
        action.created_by = user if user.is_authenticated else None
        action.last_status = ScheduledAction.LastStatus.NEVER_RUN

    try:
        _refresh_scheduled_action_next_run(action)
    except ValueError as exc:
        return [str(exc)]

    action.save()
    return []


def _parse_scheduled_target(value: str) -> tuple[str | None, int | None]:
    if ":" not in value:
        return None, None
    target_type, raw_vmid = value.split(":", 1)
    if target_type not in ScheduledAction.TargetType.values:
        return None, None
    try:
        vmid = int(raw_vmid)
    except ValueError:
        return None, None
    return target_type, vmid if vmid > 0 else None


def _apply_target_snapshot(action: ScheduledAction) -> None:
    action.target_node = ""
    action.target_name_snapshot = ""
    live_guest = _live_guest_for_target(action.target_type, action.target_vmid, use_cache=False)
    if live_guest:
        action.target_node = live_guest.node
        action.target_name_snapshot = live_guest.name
        return

    latest_scan = _latest_result_scan()
    obj = None
    if latest_scan:
        obj = (
            ProxmoxInventory.objects.filter(
                scan_run=latest_scan,
                object_type=action.target_type,
                vmid=action.target_vmid,
            )
            .order_by("node")
            .first()
        )
    if obj is None:
        obj = (
            ProxmoxInventory.objects.filter(
                object_type=action.target_type,
                vmid=action.target_vmid,
            )
            .order_by("-scan_run__created_at", "node")
            .first()
        )
    if obj:
        action.target_node = obj.node
        action.target_name_snapshot = obj.name


def _recurrence_from_post(post, recurrence_kind: str) -> dict:
    time_value = _time_value_from_post(post, "run", fallback_name="recurrence_time", label="Run time")
    months = _choice_list_from_post(
        post,
        "months",
        valid_values={value for value, _label in SCHEDULED_ACTION_MONTHS},
        label="Month",
        default=SCHEDULED_ACTION_DEFAULT_MONTHS,
    )
    month_filter = _month_filter(months)
    if recurrence_kind == ScheduledAction.RecurrenceKind.DAILY:
        return {"time": time_value, **month_filter}
    if recurrence_kind == ScheduledAction.RecurrenceKind.WEEKLY:
        weekdays = _choice_list_from_post(
            post,
            "weekdays",
            fallback_names=["weekday"],
            valid_values={value for value, _label in SCHEDULED_ACTION_WEEKDAYS},
            label="Weekday",
        )
        return {"weekdays": weekdays, "time": time_value, **month_filter}
    if recurrence_kind == ScheduledAction.RecurrenceKind.MONTHLY_ORDINAL:
        ordinals = _choice_list_from_post(
            post,
            "ordinals",
            fallback_names=["ordinal"],
            valid_values={value for value, _label in SCHEDULED_ACTION_ORDINALS},
            label="Week",
        )
        weekdays = _choice_list_from_post(
            post,
            "weekdays",
            fallback_names=["weekday"],
            valid_values={value for value, _label in SCHEDULED_ACTION_WEEKDAYS},
            label="Weekday",
        )
        return {
            "ordinals": ordinals,
            "weekdays": weekdays,
            "time": time_value,
            **month_filter,
        }
    if recurrence_kind == ScheduledAction.RecurrenceKind.MONTHLY_DAY:
        return {
            "days_of_month": _days_of_month_from_post(post),
            "time": time_value,
            **month_filter,
        }
    if recurrence_kind == ScheduledAction.RecurrenceKind.ADVANCED:
        return {"rrule": post.get("rrule", "").strip()}
    return {}


def _month_filter(months: list[str]) -> dict:
    if months == SCHEDULED_ACTION_DEFAULT_MONTHS:
        return {}
    return {"months": months}


def _choice_list_from_post(
    post,
    name: str,
    *,
    valid_values: set[str],
    label: str,
    fallback_names: list[str] | None = None,
    default: list[str] | None = None,
) -> list[str]:
    values = _posted_values(post, name, fallback_names=fallback_names)
    if not values and post.get(f"{name}_present") != "1":
        values = list(default or [])
    if not values:
        raise ValueError(f"Select at least one {label.lower()}.")
    invalid = [value for value in values if value not in valid_values]
    if invalid:
        raise ValueError(f"Unknown {label.lower()}: {', '.join(invalid)}.")
    return values


def _days_of_month_from_post(post) -> list[int]:
    raw_value = post.get("days_of_month", post.get("day_of_month", "")).strip()
    days = []
    for part in raw_value.split(","):
        value = part.strip()
        if not value:
            continue
        try:
            day = int(value)
        except ValueError as exc:
            raise ValueError("Days of month must be comma-separated numbers.") from exc
        if day < 1 or day > 31:
            raise ValueError("Days of month must be between 1 and 31.")
        days.append(day)
    if not days:
        raise ValueError("Enter at least one day of month.")
    return days


def _refresh_scheduled_action_next_run(action: ScheduledAction) -> None:
    if action.schedule_type == ScheduledAction.ScheduleType.ONCE:
        if action.run_at is None:
            raise ValueError("One-time schedules require a run time.")
        action.next_run_at = action.run_at
        return

    try:
        action.next_run_at = next_run_after(action, after=tz.now())
    except RecurrenceError as exc:
        raise ValueError(str(exc)) from exc
    if action.next_run_at is None:
        raise ValueError("Could not calculate the next run time.")


def _parse_local_datetime(value: str):
    value = value.strip()
    if not value:
        raise ValueError("Run time is required.")
    parsed = parse_datetime(value)
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Run time must be a valid date and time.") from exc
    if tz.is_naive(parsed):
        return tz.make_aware(parsed, ZoneInfo(settings.TIME_ZONE))
    return parsed


def _parse_local_datetime_from_post(post):
    legacy_value = post.get("run_at", "").strip()
    if legacy_value:
        return _parse_local_datetime(legacy_value)

    run_date = post.get("run_date", "").strip()
    if not run_date:
        raise ValueError("Run date is required.")
    try:
        parsed_date = datetime.strptime(run_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("Run date must use YYYY-MM-DD format.") from exc

    hour = _bounded_int(post.get("run_hour", "0"), 0, 23, "Run hour")
    minute = _bounded_int(post.get("run_minute", "0"), 0, 59, "Run minute")
    return tz.make_aware(datetime.combine(parsed_date, time(hour, minute)), ZoneInfo(settings.TIME_ZONE))


def _time_value_from_post(post, prefix: str, *, fallback_name: str, label: str) -> str:
    hour_value = post.get(f"{prefix}_hour")
    minute_value = post.get(f"{prefix}_minute")
    if hour_value is None and minute_value is None:
        fallback = post.get(fallback_name, "").strip()
        if fallback:
            hour_value, minute_value = _time_parts(fallback)
    hour = _bounded_int(str(hour_value if hour_value is not None else "0"), 0, 23, f"{label} hour")
    minute = _bounded_int(str(minute_value if minute_value is not None else "0"), 0, 59, f"{label} minute")
    return f"{hour:02d}:{minute:02d}"


def _bounded_int(value: str, minimum: int, maximum: int, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number.") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}.")
    return parsed


def _datetime_parts(value) -> tuple[str, str, str]:
    if value is None:
        return "", "22", "00"
    local_value = tz.localtime(value)
    return local_value.strftime("%Y-%m-%d"), local_value.strftime("%H"), local_value.strftime("%M")


def _time_parts(value: str) -> tuple[str, str]:
    parts = str(value or "22:00").split(":")
    if len(parts) < 2:
        return "22", "00"
    try:
        hour = max(0, min(23, int(parts[0])))
        minute = max(0, min(59, int(parts[1])))
    except ValueError:
        return "22", "00"
    return f"{hour:02d}", f"{minute:02d}"


def _posted_values(post, name: str, *, fallback_names: list[str] | None = None, default: list[str] | None = None) -> list[str]:
    values = []
    if hasattr(post, "getlist"):
        values = [str(value) for value in post.getlist(name) if str(value) != ""]
    else:
        raw_value = post.get(name)
        if isinstance(raw_value, list):
            values = [str(value) for value in raw_value if str(value) != ""]
        elif raw_value not in (None, ""):
            values = [str(raw_value)]

    for fallback_name in fallback_names or []:
        if values:
            break
        fallback_value = post.get(fallback_name)
        if fallback_value not in (None, ""):
            values = [str(fallback_value)]

    return values or list(default or [])


def _recurrence_values(
    recurrence: dict,
    name: str,
    *fallback_names: str,
    default: list[str] | None = None,
) -> list[str]:
    raw_value = recurrence.get(name)
    for fallback_name in fallback_names:
        if raw_value not in (None, "", []):
            break
        raw_value = recurrence.get(fallback_name)
    if raw_value in (None, "", []):
        return list(default or [])
    if isinstance(raw_value, list):
        return [str(value) for value in raw_value]
    if isinstance(raw_value, tuple):
        return [str(value) for value in raw_value]
    if isinstance(raw_value, str) and "," in raw_value:
        return [part.strip() for part in raw_value.split(",") if part.strip()]
    return [str(raw_value)]


def _recurrence_days_label(recurrence: dict) -> str:
    days = _recurrence_values(recurrence, "days_of_month", "day", "day_of_month", default=["1"])
    return ",".join(days)


def _posted_datetime_parts(post, prefix: str) -> dict[str, str]:
    legacy_value = post.get(f"{prefix}_at", "").strip()
    if legacy_value:
        try:
            parsed = _parse_local_datetime(legacy_value)
        except ValueError:
            return {
                f"{prefix}_date": post.get(f"{prefix}_date", ""),
                f"{prefix}_hour": post.get(f"{prefix}_hour", "22"),
                f"{prefix}_minute": post.get(f"{prefix}_minute", "00"),
            }
        date_value, hour_value, minute_value = _datetime_parts(parsed)
        return {
            f"{prefix}_date": post.get(f"{prefix}_date", date_value),
            f"{prefix}_hour": post.get(f"{prefix}_hour", hour_value),
            f"{prefix}_minute": post.get(f"{prefix}_minute", minute_value),
        }
    return {
        f"{prefix}_date": post.get(f"{prefix}_date", ""),
        f"{prefix}_hour": post.get(f"{prefix}_hour", "22"),
        f"{prefix}_minute": post.get(f"{prefix}_minute", "00"),
    }


def _audit_scheduled_action_definition(request, action: str, scheduled_action: ScheduledAction) -> None:
    AuditEvent.objects.create(
        user=request.user if request.user.is_authenticated else None,
        username=request.user.get_username() if request.user.is_authenticated else "",
        action=action,
        object_type="scheduled_action",
        object_id=str(scheduled_action.id),
        outcome="success",
        details={
            "scheduled_action_id": scheduled_action.id,
            "scheduled_action_name": scheduled_action.name,
            "enabled": scheduled_action.enabled,
            "action_type": scheduled_action.action_type,
            "target_type": scheduled_action.target_type,
            "target_vmid": scheduled_action.target_vmid,
            "target_node": scheduled_action.target_node,
            "schedule_type": scheduled_action.schedule_type,
            "recurrence_kind": scheduled_action.recurrence_kind,
            "next_run_at": scheduled_action.next_run_at.isoformat() if scheduled_action.next_run_at else "",
        },
    )


def _scheduled_action_target_label(action: ScheduledAction) -> str:
    return guest_identity_from_scheduled_action(action).full_label_with_type


def _latest_scheduled_runs(
    *,
    target_type: str | None = None,
    target_vmid: int | None = None,
    limit: int = 10,
) -> list[ScheduledActionRun]:
    runs_query = ScheduledActionRun.objects.select_related("scheduled_action")
    if target_type and target_vmid is not None:
        runs_query = runs_query.filter(scheduled_action__target_type=target_type, scheduled_action__target_vmid=target_vmid)

    runs = list(runs_query.order_by("-created_at")[:limit])
    for run in runs:
        run.display_target = _scheduled_action_target_label(run.scheduled_action)
        run.guest_identity = guest_identity_from_scheduled_action(run.scheduled_action)
        run.display_status_class = _scheduled_run_status_class(run.status)
        run.display_outcome = run.get_outcome_display() if run.outcome else "-"
        run.display_node = _scheduled_run_node_label(run)
    return runs


def _serialize_scheduled_run(run: ScheduledActionRun) -> dict[str, object]:
    return {
        "planned_for": _format_local_datetime(run.planned_for),
        "task": run.scheduled_action.name,
        "target": run.display_target,
        "target_guest": guest_identity_from_scheduled_action(run.scheduled_action).as_dict(),
        "status": run.get_status_display(),
        "status_class": run.display_status_class,
        "outcome": run.display_outcome,
        "started_at": _format_local_datetime(run.started_at),
        "finished_at": _format_local_datetime(run.finished_at),
        "node": run.display_node,
        "message": run.error or "-",
    }


def _scheduled_run_node_label(run: ScheduledActionRun) -> str:
    preflight = run.preflight_snapshot if isinstance(run.preflight_snapshot, dict) else {}
    return run.proxmox_task_node or str(preflight.get("node") or "") or run.scheduled_action.target_node or "-"


def _format_local_datetime(value: datetime | None) -> str:
    if value is None:
        return "-"
    return tz.localtime(value).strftime("%Y-%m-%d %H:%M:%S")


def _scheduled_action_schedule_label(action: ScheduledAction) -> str:
    if action.schedule_type == ScheduledAction.ScheduleType.ONCE:
        return f"Once at {tz.localtime(action.run_at or action.next_run_at).strftime('%Y-%m-%d %H:%M')}" if (action.run_at or action.next_run_at) else "Once"

    recurrence = action.recurrence if isinstance(action.recurrence, dict) else {}
    time_label = _recurrence_time_label(recurrence)
    if action.recurrence_kind == ScheduledAction.RecurrenceKind.DAILY:
        return f"Daily at {time_label}"
    if action.recurrence_kind == ScheduledAction.RecurrenceKind.WEEKLY:
        weekdays = _display_recurrence_values(recurrence, "weekdays", "weekday")
        day_label = f" on {weekdays}" if weekdays else ""
        return f"Weekly{day_label} at {time_label}"
    if action.recurrence_kind == ScheduledAction.RecurrenceKind.MONTHLY_ORDINAL:
        ordinals = _display_recurrence_values(recurrence, "ordinals", "ordinal", "week", default="first")
        weekdays = _display_recurrence_values(recurrence, "weekdays", "weekday", default="weekday")
        return f"Monthly on the {ordinals} {weekdays} at {time_label}"
    if action.recurrence_kind == ScheduledAction.RecurrenceKind.MONTHLY_DAY:
        days = _display_recurrence_values(recurrence, "days_of_month", "day", "day_of_month", default="?")
        return f"Monthly on day {days} at {time_label}"
    return "Advanced recurrence"


def _display_recurrence_values(recurrence: dict, name: str, *fallback_names: str, default: str = "") -> str:
    values = _recurrence_values(recurrence, name, *fallback_names, default=[default] if default else [])
    return ", ".join(str(value) for value in values if str(value))


def _recurrence_time_label(recurrence: dict) -> str:
    raw_time = recurrence.get("time")
    if raw_time:
        return str(raw_time)
    try:
        hour = int(recurrence.get("hour", 0))
        minute = int(recurrence.get("minute", 0))
    except (TypeError, ValueError):
        hour = 0
        minute = 0
    return f"{hour:02d}:{minute:02d}"


def _scheduled_action_status_class(status: str) -> str:
    return {
        ScheduledAction.LastStatus.COMPLETED: "completed",
        ScheduledAction.LastStatus.QUEUED: "queued",
        ScheduledAction.LastStatus.FAILED: "failed",
        ScheduledAction.LastStatus.TIMEOUT: "failed",
        ScheduledAction.LastStatus.SKIPPED: "warning",
        ScheduledAction.LastStatus.MISSED: "warning",
    }.get(status, "")


def _scheduled_run_status_class(status: str) -> str:
    return {
        ScheduledActionRun.Status.COMPLETED: "completed",
        ScheduledActionRun.Status.QUEUED: "queued",
        ScheduledActionRun.Status.PREFLIGHT: "running",
        ScheduledActionRun.Status.SUBMITTED: "running",
        ScheduledActionRun.Status.POLLING: "running",
        ScheduledActionRun.Status.FAILED: "failed",
        ScheduledActionRun.Status.TIMEOUT: "failed",
        ScheduledActionRun.Status.STALE: "failed",
        ScheduledActionRun.Status.SKIPPED: "warning",
        ScheduledActionRun.Status.MISSED: "warning",
    }.get(status, "")


def _trash_purge_schedule_state():
    return trash_purge_schedule_state()

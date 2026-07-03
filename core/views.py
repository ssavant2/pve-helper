from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta, timezone as dt_timezone
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
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
    LIVE_GUEST_STATUS_CACHE_SECONDS,
    ProxmoxAPIError,
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
    """Central, cluster-wide VMs & Templates workspace (left list, no selection)."""
    rows, live_available, scan_at = _guest_rows()
    context = {
        **navigation_context("vms"),
        "guests": rows,
        "guest_list": rows,
        "guest_count": len(rows),
        "running_count": sum(1 for row in rows if row.status == "running"),
        "live_available": live_available,
        "inventory_scan_at": scan_at,
        "live_status_cache_seconds": LIVE_GUEST_STATUS_CACHE_SECONDS,
        "guest_write_enabled": settings.VM_WRITE_ENABLED,
        "active_object_type": "",
        "active_vmid": None,
    }
    return render(request, "core/vms.html", context)


def _guest_rows():
    """Cluster-wide guest rows: live membership/status/name joined with the
    latest scan for template flag and tags. Falls back to scan if the API is
    unreachable. Returns (rows, live_available, scan_timestamp)."""
    live_guests = fetch_live_guest_inventory()
    latest_scan = _latest_proxmox_inventory_scan()

    scan_by_key: dict[tuple[str, int], ProxmoxInventory] = {}
    if latest_scan:
        for obj in ProxmoxInventory.objects.filter(
            scan_run=latest_scan,
            object_type__in=[ProxmoxInventory.ObjectType.VM, ProxmoxInventory.ObjectType.CT],
            vmid__isnull=False,
        ):
            scan_by_key[(obj.object_type, obj.vmid)] = obj

    live_available = bool(live_guests)
    rows: list[SimpleNamespace] = []
    if live_available:
        for guest in live_guests:
            rows.append(
                _build_guest_row(
                    object_type=guest.object_type,
                    vmid=guest.vmid,
                    name=guest.name,
                    status=guest.status,
                    node=guest.node,
                    scan_obj=scan_by_key.get((guest.object_type, guest.vmid)),
                )
            )
    else:
        for (object_type, vmid), scan_obj in scan_by_key.items():
            rows.append(
                _build_guest_row(
                    object_type=object_type,
                    vmid=vmid,
                    name=scan_obj.name,
                    status=scan_obj.status,
                    node=scan_obj.node,
                    scan_obj=scan_obj,
                )
            )

    rows.sort(key=lambda row: (row.type_sort, row.vmid or 0, row.node))
    _decorate_guests_with_scheduled_actions(rows)
    return rows, live_available, _scan_timestamp(latest_scan)


def _build_guest_row(*, object_type, vmid, name, status, node, scan_obj) -> SimpleNamespace:
    config = scan_obj.config if scan_obj is not None and isinstance(scan_obj.config, dict) else {}
    template = object_type == ProxmoxInventory.ObjectType.VM and is_template(config)
    if template:
        type_label, type_filter, type_sort = "Template", "template", 0
    elif object_type == ProxmoxInventory.ObjectType.CT:
        type_label, type_filter, type_sort = "CT", "ct", 2
    else:
        type_label, type_filter, type_sort = "VM", "vm", 1
    return SimpleNamespace(
        object_type=object_type,
        vmid=vmid,
        name=name or "",
        status=status or "",
        node=node or "",
        is_template=template,
        type_label=type_label,
        type_filter=type_filter,
        type_sort=type_sort,
        tags=parse_guest_tags(config),
        in_scan=scan_obj is not None,
    )


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
            "guest_agent_summary": _guest_agent_summary(detail),
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


def _next_device_index(config: dict, prefix: str) -> int:
    used = set()
    pattern = re.compile(rf"^{prefix}(\d+)$")
    for key in config:
        match = pattern.match(key)
        if match:
            used.add(int(match.group(1)))
    index = 0
    while index in used:
        index += 1
    return index


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
        "memory": config.get("memory", ""),
        "disks": disks,
        "nics": nics,
        "cdrom": cdrom,
        "cdrom_iso": cdrom_iso,
        "options": options,
    }
    return render(request, "core/guest_hardware_edit.html", context)


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

    for form_field, key in (("cores", "cores"), ("sockets", "sockets"), ("memory", "memory")):
        raw = post.get(form_field, "").strip()
        if raw and raw.isdigit() and int(raw) > 0 and raw != str(fresh.get(key, "") or ""):
            updates[key] = raw

    for key in [k for k in fresh if DISK_BUS_RE.match(k) and "media=cdrom" not in str(fresh[k])]:
        if post.get(f"disk_{key}_remove") == "on":
            delete.append(key)
            continue
        new_size = post.get(f"disk_{key}_size", "").strip()
        if new_size and new_size.isdigit():
            resizes.append((key, f"{new_size}G"))

    nd_storage = post.get("newdisk_storage", "").strip()
    nd_size = post.get("newdisk_size", "").strip()
    if nd_storage and nd_size.isdigit():
        updates[f"scsi{_next_device_index(fresh, 'scsi')}"] = f"{nd_storage}:{nd_size}"

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

    new_bridge = post.get("newnic_bridge", "").strip()
    if new_bridge:
        net = f"virtio,bridge={new_bridge}"
        new_vlan = post.get("newnic_vlan", "").strip()
        if new_vlan:
            net += f",tag={new_vlan}"
        updates[f"net{_next_device_index(fresh, 'net')}"] = net

    cd_key = post.get("cdrom_key", "").strip()
    if cd_key:
        iso = post.get("cdrom_iso", "").strip()
        value = f"{iso},media=cdrom" if iso else "none,media=cdrom"
        if value != str(fresh.get(cd_key, "") or ""):
            updates[cd_key] = value

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


def _guest_config_sections(config: dict) -> list[dict]:
    shown: set[str] = set()
    sections: list[dict] = []
    for title, keys in CONFIG_SECTIONS:
        rows = [{"key": key, "value": config[key]} for key in keys if key in config]
        for row in rows:
            shown.add(row["key"])
        if rows:
            sections.append({"title": title, "rows": rows})

    disk_rows = [{"key": key, "value": config[key]} for key in sorted(config) if DISK_BUS_RE.match(key)]
    shown.update(row["key"] for row in disk_rows)
    if disk_rows:
        sections.append({"title": "Disks", "rows": disk_rows})

    net_rows = [{"key": key, "value": config[key]} for key in sorted(config) if re.match(r"^net\d+$", key)]
    shown.update(row["key"] for row in net_rows)
    if net_rows:
        sections.append({"title": "Network", "rows": net_rows})

    other = [
        {"key": key, "value": config[key]}
        for key in sorted(config)
        if key not in shown and key not in CONFIG_HIDE
    ]
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
    axis = axis_max if axis_max else max(global_max * 1.15, 1e-9)

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

    if fmt == "pct":
        label = f"{axis:.1f}%"
    elif fmt == "rate":
        label = _fmt_bytes(axis) + "/s"
    else:
        label = _fmt_bytes(axis)
    return {"series": series, "axis_max_label": label, "width": width, "height": height}


@app_login_required
def guest_configure(request, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid)
    actions = list(
        ScheduledAction.objects.filter(target_type=object_type, target_vmid=vmid).order_by("-enabled", "next_run_at", "name")
    )
    for action in actions:
        action.display_schedule = _scheduled_action_schedule_label(action)
        action.display_status_class = _scheduled_action_status_class(action.last_status)
    context = _guest_tab_context(detail, "configure")
    context["config_sections"] = _guest_config_sections(detail.config)
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
    context = _guest_tab_context(detail, "networks")
    context["nets"] = guest_networks(detail.config)
    return render(request, "core/guest_networks.html", context)


@app_login_required
def guest_snapshots(request, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid)
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
        return item["snaptime"] or datetime.min.replace(tzinfo=dt_timezone.utc)

    ordered = []

    def _walk(node, depth):
        ordered.append({**node, "depth": depth, "indent": depth * 22})
        for child in sorted(children.get(node["name"], []), key=_sort_key):
            _walk(child, depth + 1)

    for root in sorted(roots, key=_sort_key):
        _walk(root, 0)

    context = _guest_tab_context(detail, "snapshots")
    context.update(
        {
            "snapshot_tree": ordered,
            "snapshot_count": sum(1 for item in entries if not item["is_current"]),
            "snapshot_error": error,
        }
    )
    return render(request, "core/guest_snapshots.html", context)


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
                "chart": _rrd_chart(points, ["cpu"], to_value=lambda v: float(v or 0) * 100, fmt="pct"),
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


def _require_guest(object_type: str, vmid: int) -> SimpleNamespace:
    if object_type not in GUEST_OBJECT_TYPES:
        raise Http404("Unknown guest type")
    detail = _resolve_guest_detail(object_type, vmid)
    if not detail.found:
        raise Http404("Guest not found")
    return detail


def _guest_kind(detail: SimpleNamespace) -> str:
    return "qemu" if detail.object_type == ProxmoxInventory.ObjectType.VM else "lxc"


def _guest_post(detail: SimpleNamespace, subpath: str, data: dict | None = None):
    if not detail.node:
        return None, "The guest's node could not be resolved."
    err = "No Proxmox endpoint could reach this guest."
    for client in configured_clients():
        try:
            return client.post(
                f"nodes/{quote(detail.node, safe='')}/{_guest_kind(detail)}/{detail.vmid}/{subpath}",
                data=data or {},
            ), None
        except ProxmoxAPIError as exc:
            err = str(exc)
    return None, err


def _guest_delete(detail: SimpleNamespace, subpath: str):
    if not detail.node:
        return None, "The guest's node could not be resolved."
    err = "No Proxmox endpoint could reach this guest."
    for client in configured_clients():
        try:
            return client.delete(
                f"nodes/{quote(detail.node, safe='')}/{_guest_kind(detail)}/{detail.vmid}/{subpath}"
            ), None
        except ProxmoxAPIError as exc:
            err = str(exc)
    return None, err


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


def _audit_guest(request, detail: SimpleNamespace, action: str, details: dict | None = None) -> None:
    AuditEvent.objects.create(
        user=request.user if request.user.is_authenticated else None,
        username=request.user.get_username() if request.user.is_authenticated else "system",
        action=action,
        object_type="guest",
        object_id=f"{detail.object_type}:{detail.vmid}",
        outcome="success",
        details={"node": detail.node, "vmid": detail.vmid, "target_type": detail.object_type, "name": detail.name, **(details or {})},
    )


GUEST_POWER_ACTIONS = {"start", "shutdown", "reboot", "stop"}


@require_POST
@app_login_required
def guest_power(request, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid)
    action = request.POST.get("action", "")
    if action not in GUEST_POWER_ACTIONS:
        messages.error(request, "Unknown power action.")
        return redirect("core:guest_summary", object_type=object_type, vmid=vmid)
    _data, err = _guest_post(detail, f"status/{action}")
    if err:
        if "403" in err:
            messages.error(request, "Proxmox denied the power action (403) - the token needs VM.PowerMgmt.")
        else:
            messages.error(request, f"Power action failed: {err}")
    else:
        _audit_guest(request, detail, f"guest.power.{action}")
    return redirect("core:guest_summary", object_type=object_type, vmid=vmid)


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
    _data, err = _guest_post(detail, "snapshot", data)
    if err:
        messages.error(request, _snapshot_error(err))
    else:
        _audit_guest(request, detail, "guest.snapshot.create", {"snapshot": name})
    return redirect("core:guest_snapshots", object_type=object_type, vmid=vmid)


@require_POST
@app_login_required
def guest_snapshot_delete(request, object_type: str, vmid: int, snapname: str):
    detail = _require_guest(object_type, vmid)
    _data, err = _guest_delete(detail, f"snapshot/{quote(snapname, safe='')}")
    if err:
        messages.error(request, _snapshot_error(err))
    else:
        _audit_guest(request, detail, "guest.snapshot.delete", {"snapshot": snapname})
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
        params = {
            **common,
            "name": post.get("name", "").strip(),
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


def _resolve_guest_detail(object_type: str, vmid: int) -> SimpleNamespace:
    """Resolve a guest to its current node + live status/config.

    Membership/node come from the live cluster inventory; if the API is
    unreachable, fall back to the latest scan. Never silently pick one guest
    when the same type+VMID is ambiguous across multiple nodes.
    """
    matches = [g for g in fetch_live_guest_inventory() if g.object_type == object_type and g.vmid == vmid]
    nodes = {g.node for g in matches if g.node}
    ambiguous = len(nodes) > 1
    node = next(iter(matches)).node if matches else ""
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

    if not config:
        scan = _latest_proxmox_inventory_scan()
        if scan:
            obj = ProxmoxInventory.objects.filter(scan_run=scan, object_type=object_type, vmid=vmid).first()
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


def _guest_api_get(detail: SimpleNamespace, subpath: str):
    """GET a guest-scoped Proxmox path (e.g. 'snapshot', 'rrddata?...',
    'agent/get-osinfo'); returns (data, error_message)."""
    kind = "qemu" if detail.object_type == ProxmoxInventory.ObjectType.VM else "lxc"
    if not detail.node:
        return None, "The guest's node could not be resolved."
    err = "No Proxmox endpoint could reach this guest."
    for client in configured_clients():
        try:
            return client.get(f"nodes/{quote(detail.node, safe='')}/{kind}/{detail.vmid}/{subpath}"), None
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


def _guest_agent_summary(detail: SimpleNamespace) -> dict:
    """Best-effort guest-agent OS name + IPs for the Summary Guest OS card.
    Only queries when the agent is enabled; degrades silently otherwise."""
    config = detail.config
    if detail.object_type != ProxmoxInventory.ObjectType.VM or not config.get("agent"):
        return {"enabled": False, "running": False, "os_name": "", "hostname": "", "ips": []}
    os_name = ""
    hostname = ""
    ips: list[str] = []
    os_data, _err = _guest_api_get(detail, "agent/get-osinfo")
    if isinstance(os_data, dict):
        result = os_data.get("result") if isinstance(os_data.get("result"), dict) else os_data
        if isinstance(result, dict):
            os_name = result.get("pretty-name") or result.get("name") or ""
    host_data, _err = _guest_api_get(detail, "agent/get-host-name")
    if isinstance(host_data, dict):
        result = host_data.get("result") if isinstance(host_data.get("result"), dict) else host_data
        if isinstance(result, dict):
            hostname = result.get("host-name", "")
    net_data, _err = _guest_api_get(detail, "agent/network-get-interfaces")
    if isinstance(net_data, dict):
        for iface in net_data.get("result") or []:
            if not isinstance(iface, dict) or iface.get("name") == "lo":
                continue
            for addr in iface.get("ip-addresses") or []:
                ip = addr.get("ip-address") if isinstance(addr, dict) else None
                if ip and not ip.startswith("127.") and ip != "::1":
                    ips.append(ip)
    return {"enabled": True, "running": bool(os_name or hostname or ips), "os_name": os_name, "hostname": hostname, "ips": ips[:4]}


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
            key = (guest.object_type, guest.vmid)
            if key in live_status:
                guest.status = live_status[key]
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
        details = event.details if isinstance(event.details, dict) else {}
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

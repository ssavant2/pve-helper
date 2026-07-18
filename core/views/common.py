from __future__ import annotations

import json
import re
from functools import wraps
from datetime import datetime, time, timedelta, timezone as dt_timezone
from time import monotonic
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
from django.db.models import Count, F, Q
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

from ..models import (
    AuditEvent,
    CurrentGuestInventory,
    FileInventory,
    ProxmoxInventory,
    ScanRun,
    ScheduledAction,
    ScheduledActionRun,
    StorageMount,
    StorageSpaceSnapshot,
    TrashItem,
)
from ..services.classification import extract_disk_references, parse_config_value_volid
from ..services.file_actions import FileActionRisk, file_action_risk
from ..services.audit_retention_schedule import audit_retention_schedule_state, update_audit_retention_schedule
from ..services.audit_events import audit_module_key, record_audit_event
from ..services.filesystem import storage_space_info
from ..services.guests import (
    guest_identity,
    guest_identity_from_inventory,
    guest_identity_from_scheduled_action,
    is_template,
    parse_guest_tags,
)
from ..services.guest_storage import DISK_BUS_RE, guest_disks, guest_networks
from ..services.guest_create import create_ct, create_options, create_vm
from ..services.partial_scan import refresh_storage_directory
from ..services.permissions import storage_permissions as get_permissions
from ..services.proxmox import (
    LIVE_GUEST_INVENTORY_CACHE_SECONDS,
    LIVE_GUEST_STATUS_CACHE_SECONDS,
    ProxmoxAPIError,
    ProxmoxTaskTimeout,
    clear_live_guest_caches,
    fetch_live_guest_inventory,
    fetch_live_guest_lineage,
    fetch_live_guest_locks,
    fetch_live_guest_status,
)
from ..services.recent_tasks import recent_task_page, serialize_task_page
from ..services.request_metadata import client_ip
from ..services.task_queues import BULK_QUEUE_NAME
from ..services.scan_schedule import scan_schedule_state, update_scan_schedule
from ..services.scheduled_actions import ScheduledActionQueueError, queue_manual_scheduled_action_run
from ..services.scheduled_recurrence import RecurrenceError, next_run_after
from ..services.storage_actions import (
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
    public_storage_upload_error,
    purge_trash_item as purge_trash_item_action,
    rename_storage_file,
    restore_trash_item,
    transfer_storage_file,
    upload_to_storage,
    upload_folder_to_storage,
)
from ..services.storage_details import storage_details
from ..services.storage_visibility import ignored_relative_paths_for_storage, is_ignored_storage_path
from ..services.trash_schedule import trash_purge_schedule_state, update_trash_purge_schedule


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


def json_task_response(view_func):
    """Let a redirecting action view answer fetch (XHR) callers with JSON so the
    optimistic Recent Tasks row can be updated in place. For a fetch request the
    view's normal redirect is swapped for ``{"ok": ..., "errors": [...]}`` built
    from any error-level messages it queued; plain requests are unchanged. Apply
    below @app_login_required so it only wraps the view body."""

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        response = view_func(request, *args, **kwargs)
        if request.headers.get("X-Requested-With") != "fetch":
            return response
        errors = [m.message for m in messages.get_messages(request) if m.level >= messages.ERROR]
        return JsonResponse({"ok": not errors, "errors": errors})

    return wrapper


# Total wall-time budget for live Proxmox calls in an overview enrichment XHR.
# Cached probes stay cheap after it is spent; uncached ones fall back to scan.
OVERVIEW_ENRICH_BUDGET_SECONDS = 6.0


GUEST_OBJECT_TYPES = {"vm": ProxmoxInventory.ObjectType.VM, "ct": ProxmoxInventory.ObjectType.CT}


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
CT_FEATURE_OPTIONS = (
    ("nesting", "Nesting"),
    ("keyctl", "keyctl()"),
    ("fuse", "FUSE"),
    ("mknod", "mknod()"),
    ("force_rw_sys", "Force RW /sys"),
)
CT_OSTYPE_LABELS = {
    "alpine": "Alpine Linux",
    "archlinux": "Arch Linux",
    "centos": "CentOS",
    "debian": "Debian",
    "devuan": "Devuan",
    "fedora": "Fedora",
    "gentoo": "Gentoo",
    "nixos": "NixOS",
    "opensuse": "openSUSE",
    "ubuntu": "Ubuntu",
    "unmanaged": "Unmanaged",
}
CT_ARCH_LABELS = {
    "amd64": "amd64",
    "arm64": "arm64",
    "armhf": "armhf",
    "i386": "i386",
    "riscv32": "riscv32",
    "riscv64": "riscv64",
}
CT_NET_ORDER = ("name", "bridge", "firewall", "gw", "gw6", "hwaddr", "ip", "ip6", "link_down", "mtu", "rate", "tag", "trunks", "type")
CT_MOUNT_ORDER = ("mp", "acl", "backup", "quota", "replicate", "ro", "shared", "size", "mountoptions")


CONFIG_SECTIONS = [
    ("General", ["name", "hostname", "ostype", "arch", "bios", "machine", "boot", "onboot", "startup", "agent", "tablet", "protection", "hotplug"]),
    ("Processors", ["cores", "sockets", "vcpus", "cpu", "numa", "cpuunits", "cpulimit", "affinity"]),
    ("Memory", ["memory", "balloon", "shares", "swap"]),
]
CONFIG_HIDE = {"digest", "description", "tags", "meta", "smbios1", "vmgenid"}


SNAPSHOT_TASK_WAIT_SECONDS = 60


def _guest_destroy(detail: SimpleNamespace, query: str):
    response, err, _client = _guest_destroy_with_client(detail, query)
    return response, err


GUEST_POWER_ACTIONS = {"start", "shutdown", "reboot", "stop", "reset", "suspend", "resume", "hibernate"}

# Power action -> (status subpath, extra POST params). Hibernate is
# suspend-to-disk (frees RAM, survives a host reboot); resuming a hibernated
# guest is a normal Power On.
POWER_ACTION_REQUESTS = {
    "start": ("status/start", {}),
    "shutdown": ("status/shutdown", {}),
    "reboot": ("status/reboot", {}),
    "stop": ("status/stop", {}),
    "reset": ("status/reset", {}),
    "suspend": ("status/suspend", {}),
    "hibernate": ("status/suspend", {"todisk": 1}),
    "resume": ("status/resume", {}),
}

# QEMU-only power actions (LXC has no reliable suspend/resume/reset).
VM_ONLY_POWER_ACTIONS = {"reset", "suspend", "resume", "hibernate"}
VM_BULK_ACTIONS = {
    *GUEST_POWER_ACTIONS,
    "snapshot",
    "delete_snapshots",
    "template",
    "untemplate",
    "pool",
    "migrate",
    "clone",
    "tags",
    "destroy",
    "agent_enable",
    "agent_disable",
    "backup",
}


GUEST_AGENT_API_TIMEOUT_SECONDS = 2


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
    "win11": "Windows 11/2022/2025",
    "wxp": "Windows XP",
    "solaris": "Solaris",
    "other": "Other",
}


def _int_or_zero(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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
    return guest_identity(
        target_type,
        vmid,
        details.get("name") or "",
        cluster_key=event.cluster_key_snapshot or details.get("cluster_key") or "",
        node=details.get("node") or details.get("target_node") or "",
    )


def _audit_module_key(event: AuditEvent) -> str:
    return event.module or _audit_module_key_for(event.action, event.object_type, event.details)


def _audit_module_key_for(action: str, object_type: str, details) -> str:
    return audit_module_key(action, object_type, details)


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
    if event.action == "storage.content.updated":
        return "Update storage content"
    if event.action == "audit.retention.purge":
        return "Audit retention purge"
    if event.action == "audit.retention.schedule.updated":
        return "Audit retention schedule updated"
    if event.action == "task.cancelled":
        return "Cancel task"
    tag_action_labels = {
        "tag.registered": "Create tag",
        "tag.recolored": "Change tag color",
        "tag.inventory.refresh": "Refresh tag inventory",
        "tag.renamed": "Rename tag",
        "tag.deleted": "Delete tag",
        "tag.removed": "Remove tag from guest",
        "tag.membership.renamed": "Rename tag on guest",
        "tag.membership.removed": "Remove tag from guest",
        "tag.bulk_operation": "Update tag assignments",
    }
    if event.action in tag_action_labels:
        return tag_action_labels[event.action]
    cluster_action_labels = {
        "cluster.added": "Add cluster",
        "cluster.display_name_changed": "Change cluster display name",
        "cluster.disabled": "Disable cluster",
        "cluster.enabled": "Enable cluster",
        "cluster.endpoint_added": "Add cluster endpoint",
        "cluster.endpoint_enabled": "Enable cluster endpoint",
        "cluster.endpoint_disabled": "Disable cluster endpoint",
        "cluster.credential_rotated": "Rotate cluster credential",
        "cluster.credential_removed": "Remove cluster credential",
        "cluster.credential.set": "Set cluster credential",
        "cluster.credential.cutover": "Import legacy cluster credential",
        "cluster.credential.rotate": "Re-encrypt cluster credential",
        "cluster.identity_reapproved": "Re-approve cluster identity",
        "cluster.identity.reapprove": "Re-approve cluster identity",
    }
    if event.action in cluster_action_labels:
        return cluster_action_labels[event.action]
    guest_action_labels = {
        "guest.power.start": "Power on guest",
        "guest.power.shutdown": "Shut down guest OS",
        "guest.power.reboot": "Restart guest OS",
        "guest.power.stop": "Power off guest",
        "guest.power.reset": "Reset guest",
        "guest.power.suspend": "Suspend guest",
        "guest.power.resume": "Resume guest",
        "guest.power.hibernate": "Hibernate guest",
        "guest.snapshot.create": "Create snapshot",
        "guest.snapshot.delete": "Delete snapshot",
        "guest.snapshot.delete_all": "Delete all snapshots",
        "guest.snapshot.rollback": "Roll back snapshot",
        "guest.template.convert": "Convert guest to template",
        "guest.template.revert": "Convert template to VM",
        "guest.pool.updated": "Move guest to pool",
        "guest.migrate": "Migrate guest",
        "guest.clone.create": "Clone guest",
        "guest.template.clone": "Clone to template",
        "guest.tags.updated": "Update guest tags",
        "guest.agent.enable": "Enable guest agent",
        "guest.agent.disable": "Disable guest agent",
        "guest.destroy": "Destroy guest",
        "guest.config.updated": "Update guest configuration",
        "guest.hardware.updated": "Update guest hardware",
        "guest.cloudinit.update": "Update Cloud-Init",
        "guest.create": "Create guest",
        "guest.register.adopt": "Register VM from disk",
        "guest.register.import": "Import VM from disk",
        "guest.firewall.options": "Update firewall options",
        "guest.firewall.rule_add": "Add firewall rule",
        "guest.firewall.rule_delete": "Delete firewall rule",
        "guest.firewall.rule_toggle": "Toggle firewall rule",
        "guest.backup.run": "Run backup",
        "guest.backup.restore": "Restore backup",
        "guest.backup.delete": "Delete backup",
        "guest.replication.create": "Create replication job",
        "guest.replication.delete": "Delete replication job",
        "guest.console.opened": "Open console",
        "guest.console.closed": "Close console",
        "guest.console.failed": "Console failed",
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
    if event.action == "scheduled_action.run_cancelled":
        return "Scheduled task cancelled"
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
    cluster_label = event.cluster.display_name if event.cluster_id else (
        details.get("display_name") or event.cluster_key_snapshot or details.get("cluster_key")
    )
    if event.object_type == "cluster":
        return str(cluster_label or event.object_id or "Cluster")
    if event.object_type == "cluster_credential":
        return f"{cluster_label or event.object_id or 'Cluster'} API credential"
    if event.object_type == "cluster_endpoint":
        endpoint_name = details.get("endpoint_name") or event.object_id
        return f"{cluster_label or 'Cluster'} · {endpoint_name}"
    if event.object_type == "guest":
        target_type = details.get("target_type")
        vmid = details.get("vmid")
        if not target_type or vmid is None:
            raw_type, separator, raw_vmid = str(event.object_id or "").partition(":")
            if separator == ":":
                target_type = target_type or raw_type
                vmid = vmid if vmid is not None else raw_vmid
        return guest_identity(
            target_type,
            vmid,
            details.get("name") or "",
            cluster_key=event.cluster_key_snapshot or details.get("cluster_key") or "",
            node=details.get("node") or details.get("target_node") or "",
        ).full_label_with_type
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


def _active_scan() -> ScanRun | None:
    return (
        ScanRun.objects.filter(status__in=[ScanRun.Status.QUEUED, ScanRun.Status.RUNNING])
        .order_by("-created_at")
        .first()
    )


def _safe_next_url(request) -> str:
    next_url = request.POST.get("next", "").strip()
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return next_url
    return reverse("core:dashboard")


def _decorate_guests_with_scheduled_actions(guests: list) -> None:
    action_filter = Q()
    guest_keys: set[tuple[int, str, int]] = set()
    for guest in guests:
        cluster_id = getattr(guest, "cluster_id", None) or getattr(
            getattr(guest, "cluster", None), "pk", None
        )
        if cluster_id and guest.vmid and guest.object_type in {
            ProxmoxInventory.ObjectType.VM,
            ProxmoxInventory.ObjectType.CT,
        }:
            guest_keys.add((cluster_id, guest.object_type, guest.vmid))
            action_filter |= Q(
                cluster_id=cluster_id,
                target_type=guest.object_type,
                target_vmid=guest.vmid,
            )

    actions_by_target: dict[tuple[int, str, int], list[ScheduledAction]] = {}
    if action_filter:
        actions = ScheduledAction.objects.filter(action_filter, deleted_at__isnull=True).order_by("-enabled", "next_run_at", "name")
        for action in actions:
            action.display_schedule = _scheduled_action_schedule_label(action)
            action.display_status_class = _scheduled_action_status_class(action.last_status)
            actions_by_target.setdefault(
                (action.cluster_id, action.target_type, action.target_vmid), []
            ).append(action)

    for guest in guests:
        cluster = getattr(guest, "cluster", None)
        cluster_id = getattr(guest, "cluster_id", None) or getattr(cluster, "pk", None)
        ref = guest.guest_ref() if callable(getattr(guest, "guest_ref", None)) else getattr(guest, "guest_ref", None)
        target = ref.without_node().serialize() if ref is not None else ""
        guest.scheduled_actions = actions_by_target.get(
            (cluster_id, guest.object_type, guest.vmid), []
        )
        guest.scheduled_action_count = len(guest.scheduled_actions)
        guest.scheduled_action_search_text = " ".join(action.name for action in guest.scheduled_actions)
        guest.schedule_action_url = (
            f"{reverse('core:scheduled_task_create')}?{urlencode({'target': target})}"
            if target
            else reverse("core:scheduled_task_create")
        )
        guest.scheduled_actions_url = f"{reverse('core:scheduled_tasks')}?{urlencode({'target': target})}"


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


def proxmox_permission_hint(privilege: str) -> str:
    """Consistent 403 hint text; ``privilege`` names the missing Proxmox right."""
    return f"Proxmox denied the operation (403) - the token needs {privilege}."


def enqueue_bulk_task(func, *args, **kwargs):
    """Queue data-plane work away from the scheduler/control worker."""
    q_options = dict(kwargs.pop("q_options", {}))
    q_options["cluster"] = BULK_QUEUE_NAME
    return async_task(func, *args, q_options=q_options, **kwargs)


def cluster_scoped_clients(cluster):
    """Provider clients for the guest views, bounded to one cluster.

    This replaces the global client fan-out these views used to share. That fan-out selected clients from settings with no cluster scope, so a
    view looking for `vm:500` on `pve1` would accept whichever endpoint answered
    first — including one belonging to a different cluster.

    Callers carry the cluster resolved from a canonical path or GuestRef and all
    returned clients belong to it. A missing/disabled/quarantined scope fails
    closed as an empty passive-read result; mutation callers validate that no
    client was available before submitting work.

    Returns an empty list when the explicit cluster cannot be acquired, preserving
    passive last-known reads without selecting a different cluster.
    """
    from core.services.cluster_resolver import ClusterResolutionError, cluster_clients

    try:
        return cluster_clients(cluster)
    except ClusterResolutionError:
        return []


_PATCHABLE_TEST_DEPS = {
    'async_task',
    'cluster_scoped_clients',
    'fetch_live_guest_inventory',
    'fetch_live_guest_lineage',
    'fetch_live_guest_locks',
    'fetch_live_guest_status',
    'storage_space_info',
}
# Export everything (including underscore helpers) to the domain modules,
# except the mockable dependencies, which domain modules reach via
# ``common.<name>`` so ``patch('core.views.common.<name>')`` works everywhere.
__all__ = [name for name in dir() if not name.startswith('__') and name not in _PATCHABLE_TEST_DEPS]

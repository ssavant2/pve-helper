from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path, PurePosixPath

from django.templatetags.static import static

from .common import *  # noqa: F401,F403
from . import common
from core.models import ProxmoxEndpoint, StorageMount
from core.services.classification import DISK_CONFIG_KEYS, extract_disk_references
from core.services.console_sessions import create_guest_console_session


SNAPSHOT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
SNAPSHOT_NAME_HELP = "Snapshot names must start with a letter and can then contain letters, digits, _ and -."


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
def global_search(request):
    query = request.GET.get("q", "").strip()
    tokens = _search_tokens(query)
    if not tokens:
        return JsonResponse({"query": query, "results": []})

    results = []
    results.extend(_global_search_guests(tokens))
    results.extend(_global_search_storages(tokens))
    results.extend(_global_search_hosts(tokens))
    results.extend(_global_search_endpoints(tokens))
    results.sort(key=lambda item: (item["score"], item["category"], item["label"].casefold()))

    return JsonResponse({"query": query, "results": [_public_search_result(item) for item in results[:14]]})


@app_login_required
def vms_overview_agent_info(request):
    rows, _live_available, _scan_at = _guest_rows()
    deadline = monotonic() + OVERVIEW_ENRICH_BUDGET_SECONDS
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
        # Cached summaries stay cheap; once the per-request budget is spent we
        # stop issuing new agent calls and serve only what is already cached.
        summary = _guest_agent_summary(detail, allow_fetch=monotonic() < deadline)
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
    deadline = monotonic() + OVERVIEW_ENRICH_BUDGET_SECONDS
    payload = []
    for row in rows:
        has_snapshot = _live_guest_has_snapshot(row, allow_fetch=monotonic() < deadline)
        # None = probe unavailable/budget spent; keep it unknown ("-") rather
        # than reporting a misleading "No".
        payload.append(
            {
                "target": row.target_id,
                "has_snapshot": bool(has_snapshot),
                "has_snapshot_label": "-" if has_snapshot is None else ("Yes" if has_snapshot else "No"),
            }
        )
    return JsonResponse({"guests": payload})


@app_login_required
def vms_status(request):
    statuses = common.fetch_live_guest_status()
    locks = common.fetch_live_guest_locks()
    guests = [
        {
            "target": _guest_target_value(object_type, vmid, node),
            "status": status,
            "state_label": _guest_state_label(status),
            "lock": _display_lock(locks.get((node, object_type, vmid), "")),
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


def _global_search_guests(tokens: list[str]) -> list[dict]:
    rows, _live_available, _scan_at = _guest_rows()
    results = []
    for row in rows:
        detail = SimpleNamespace(
            object_type=row.object_type,
            vmid=row.vmid,
            name=row.name,
            node=row.node,
            status=row.status,
            config=getattr(row, "config", {}),
        )
        agent = _guest_agent_summary(detail, allow_fetch=False)
        agent_ips = agent.get("ips") or []
        agent_os = agent.get("os_pretty_name") or agent.get("os_name") or ""
        agent_hostname = agent.get("hostname") or ""
        parts = [
            row.name,
            row.vmid,
            row.type_label,
            row.node,
            row.state_label,
            row.guest_os_label,
            row.ip_label,
            row.mac_label,
            row.storage_label,
            row.tags_label,
            agent_hostname,
            agent_os,
            *agent_ips,
        ]
        if not _matches_search(parts, tokens):
            continue
        meta = [row.type_label]
        if row.vmid is not None:
            meta.append(str(row.vmid))
        if row.node:
            meta.append(row.node)
        if row.state_label:
            meta.append(row.state_label)
        ip_text = ", ".join([*agent_ips, *getattr(row, "ip_addresses", [])][:3])
        if ip_text:
            meta.append(ip_text)
        results.append(
            _search_result(
                category="Guests",
                kind=row.type_label,
                label=row.name or f"{row.type_label} {row.vmid}",
                meta=" • ".join(meta),
                url=row.detail_url,
                icon_family="vicon",
                icon="template" if getattr(row, "is_template", False) else ("container" if row.object_type == ProxmoxInventory.ObjectType.CT else "vm"),
                score=_search_score(tokens, row.name, row.vmid, row.node, agent_hostname, *agent_ips),
            )
        )
    return results


def _global_search_storages(tokens: list[str]) -> list[dict]:
    results = []
    mounted_ids = set()
    for storage in StorageMount.objects.filter(enabled=True).order_by("display_name"):
        mounted_ids.add(storage.storage_id)
        parts = [storage.display_name, storage.storage_id, storage.export, storage.path, *(storage.expected_consumers or [])]
        if not _matches_search(parts, tokens):
            continue
        meta = ["Mounted datastore", storage.storage_id]
        if storage.expected_consumers:
            meta.append(", ".join(storage.expected_consumers))
        results.append(
            _search_result(
                category="Storage",
                kind="Datastore",
                label=storage.display_name or storage.storage_id,
                meta=" • ".join(meta),
                url=reverse("core:storage_summary", args=[storage.storage_id]),
                icon_family="vicon",
                icon="storage",
                score=_search_score(tokens, storage.display_name, storage.storage_id),
            )
        )

    latest_scan = _latest_proxmox_inventory_scan()
    if latest_scan:
        seen_api_storage: set[tuple[str, str]] = set()
        for obj in ProxmoxInventory.objects.filter(scan_run=latest_scan, object_type=ProxmoxInventory.ObjectType.STORAGE).order_by("node", "name"):
            if not obj.node or not obj.name:
                continue
            if obj.name in mounted_ids:
                continue
            key = (obj.node or "", obj.name or "")
            if key in seen_api_storage:
                continue
            seen_api_storage.add(key)
            content = _config_text(obj.config, "content")
            storage_type = _config_text(obj.config, "type")
            shared = _config_text(obj.config, "shared")
            parts = [obj.name, obj.node, storage_type, content, shared]
            if not _matches_search(parts, tokens):
                continue
            meta = ["API datastore"]
            if obj.node:
                meta.append(obj.node)
            if storage_type:
                meta.append(storage_type)
            if content:
                meta.append(content)
            results.append(
                _search_result(
                    category="Storage",
                    kind="Datastore",
                    label=obj.name or "Storage",
                    meta=" • ".join(meta),
                    url=reverse("core:api_storage_summary", args=[obj.node, obj.name]),
                    icon_family="vicon",
                    icon="storage",
                    score=_search_score(tokens, obj.name, obj.node, storage_type),
                )
            )
    return results


def _global_search_hosts(tokens: list[str]) -> list[dict]:
    latest_scan = _latest_proxmox_inventory_scan()
    seen: set[str] = set()
    candidates = []
    if latest_scan:
        for obj in ProxmoxInventory.objects.filter(scan_run=latest_scan, object_type=ProxmoxInventory.ObjectType.NODE).order_by("name"):
            if not obj.name:
                continue
            seen.add(obj.name)
            candidates.append((obj.name, obj.status, obj.config))
        for obj in ProxmoxInventory.objects.filter(scan_run=latest_scan).exclude(object_type=ProxmoxInventory.ObjectType.NODE).exclude(node="").order_by("node"):
            if not obj.node or obj.node in seen:
                continue
            seen.add(obj.node)
            candidates.append((obj.node, "", {}))
    for row in _guest_rows()[0]:
        if row.node and row.node not in seen:
            seen.add(row.node)
            candidates.append((row.node, "", {}))

    results = []
    for node, status, config in sorted(candidates, key=lambda item: item[0].casefold()):
        parts = [node, status, _config_text(config, "cpu"), _config_text(config, "mem")]
        if not _matches_search(parts, tokens):
            continue
        meta = ["Host"]
        if status:
            meta.append(status)
        results.append(
            _search_result(
                category="Infrastructure",
                kind="Host",
                label=node,
                meta=" • ".join(meta),
                url=f"{reverse('core:vms_overview')}?{urlencode({'q': node})}",
                icon_family="vicon",
                icon="host",
                score=_search_score(tokens, node, status),
            )
        )
    return results


def _global_search_endpoints(tokens: list[str]) -> list[dict]:
    results = []
    for endpoint in ProxmoxEndpoint.objects.filter(enabled=True).order_by("name"):
        parts = [endpoint.name, endpoint.url, endpoint.last_health_status]
        if not _matches_search(parts, tokens):
            continue
        meta = ["Proxmox endpoint"]
        if endpoint.last_health_status:
            meta.append(endpoint.last_health_status)
        results.append(
            _search_result(
                category="Infrastructure",
                kind="Cluster",
                label=endpoint.name,
                meta=" • ".join(meta),
                url=reverse("core:dashboard"),
                icon_family="vicon",
                icon="cluster",
                score=_search_score(tokens, endpoint.name, endpoint.url),
            )
        )
    return results


def _search_tokens(query: str) -> list[str]:
    return [token.casefold() for token in re.split(r"\s+", query.strip()) if token.strip()]


def _matches_search(parts: list[object], tokens: list[str]) -> bool:
    haystack = " ".join(str(part) for part in parts if part not in (None, "", "-")).casefold()
    return bool(haystack) and all(token in haystack for token in tokens)


def _search_score(tokens: list[str], *priority_parts: object) -> int:
    priority = " ".join(str(part) for part in priority_parts if part not in (None, "", "-")).casefold()
    if any(priority == token for token in tokens):
        return 0
    if any(priority.startswith(token) for token in tokens):
        return 10
    if all(token in priority for token in tokens):
        return 20
    return 50


def _search_result(*, category: str, kind: str, label: str, meta: str, url: str, icon_family: str, icon: str, score: int) -> dict:
    return {
        "category": category,
        "kind": kind,
        "label": label,
        "meta": meta,
        "url": url,
        "icon_family": icon_family,
        "icon": icon,
        "score": score,
    }


def _public_search_result(result: dict) -> dict:
    return {key: value for key, value in result.items() if key != "score"}


def _config_text(config: dict, key: str) -> str:
    if not isinstance(config, dict):
        return ""
    value = config.get(key, "")
    if value is None or isinstance(value, (dict, list, tuple)):
        return ""
    return str(value)


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
    rows, live_available, scan_at = _guest_rows()
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
    live_guests = common.fetch_live_guest_inventory()
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
        for guest in live_guests:
            rows.append(
                _build_guest_row(
                    object_type=guest.object_type,
                    vmid=guest.vmid,
                    name=guest.name,
                    status=guest.status,
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


def _guest_target_value(object_type: str, vmid: int | str | None, node: str = "") -> str:
    base = f"{object_type}:{vmid}"
    return f"{base}@{node}" if node else base


def _display_lock(value: object) -> str:
    """A guest's config lock for the 'needs a look' badge — but ``suspended`` is
    hibernate (an expected state shown by the moon icon), not a problem, so drop it."""
    lock = str(value or "").strip()
    return "" if lock == "suspended" else lock


def _build_guest_row(*, object_type, vmid, name, status, node, scan_obj, live_guest=None) -> SimpleNamespace:
    config = scan_obj.config if scan_obj is not None and isinstance(scan_obj.config, dict) else {}
    template = object_type == ProxmoxInventory.ObjectType.VM and (
        bool(getattr(live_guest, "is_template", False)) or is_template(config)
    )
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
        config=config,
        guest_identity=identity,
        status=status or "",
        state_label=_guest_state_label(status),
        node=node or "",
        lock=_display_lock(getattr(live_guest, "lock", "") or config.get("lock")),
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
    if not row.node or row.vmid is None:
        return None
    cache_key = f"pve-helper:guest-snapshot-present:v1:{row.node}:{row.object_type}:{row.vmid}"
    cached = cache.get(cache_key)
    if cached is not None:
        return bool(cached)
    if not allow_fetch:
        return None
    kind = "qemu" if row.object_type == ProxmoxInventory.ObjectType.VM else "lxc"
    path = f"nodes/{quote(row.node, safe='')}/{kind}/{row.vmid}/snapshot"
    for client in common.configured_clients():
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
        for guest in common.fetch_live_guest_inventory()
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
    guest_pool = _guest_pool_label(detail)
    guest_ha = _guest_ha_summary(detail)
    context.update(
        {
            "guest_health": _guest_health(detail),
            "guest_lineage": _guest_lineage(detail),
            "guest_os_label": _guest_os_label(config),
            "guest_agent_summary": _guest_agent_summary(detail, allow_fetch=False),
            "guest_usage": _guest_usage(current, config, detail.object_type),
            "guest_cpu_topology": _guest_cpu_topology(config, detail.object_type),
            "related_storages": related_storages,
            "related_networks": related_networks,
            "vm_details": _guest_vm_details(detail, guest_pool),
            "guest_ha": guest_ha,
            "guest_cpu_label": _guest_cpu_label(config, detail.object_type),
            "guest_memory_label": f"{config.get('memory')} MB" if config.get("memory") else "",
            "guest_disks": disks,
            "guest_cdroms": cdroms,
            "guest_nets": nets,
            "guest_notes": config.get("description") or "",
            "guest_current": current,
            "guest_config": config,
            # A hibernated (suspend-to-disk) VM is 'stopped' but carries
            # lock=suspended + a saved vmstate; Power On resumes it. The live
            # inventory may already surface it as 'hibernated'.
            "guest_is_hibernated": detail.status == "hibernated"
            or (
                detail.status == "stopped"
                and ((current or {}).get("lock") or config.get("lock")) == "suspended"
            ),
        }
    )
    return render(request, "core/guest_summary.html", context)


@app_login_required
def guest_console(request, object_type: str, vmid: int):
    if object_type not in GUEST_OBJECT_TYPES:
        raise Http404("Unknown guest type")
    detail = _resolve_guest_detail(object_type, vmid)
    if not detail.found:
        raise Http404("Guest not found")

    context = _guest_tab_context(detail, "console")
    context.update(
        {
            "console_enabled": settings.CONSOLE_ENABLED,
            "console_supported": detail.object_type in {ProxmoxInventory.ObjectType.VM, ProxmoxInventory.ObjectType.CT},
            "console_session_url": reverse("core:guest_console_session", args=[object_type, vmid]),
            # Locally vendored (no CDN). Pinned versions + update steps:
            # static/vendor/README.md.
            "console_novnc_url": static("vendor/novnc/rfb.esm.js"),
            "console_xterm_js_url": static("vendor/xterm/xterm.min.js"),
            "console_xterm_fit_url": static("vendor/xterm/addon-fit.min.js"),
            "console_xterm_css_url": static("vendor/xterm/xterm.min.css"),
            "console_require_running": detail.status != "running",
        }
    )
    return render(request, "core/guest_console.html", context)


@require_POST
@app_login_required
def guest_console_session(request, object_type: str, vmid: int):
    if object_type not in GUEST_OBJECT_TYPES:
        return JsonResponse({"error": "Unknown guest type."}, status=404)
    detail = _resolve_guest_detail(object_type, vmid)
    if not detail.found:
        return JsonResponse({"error": "Guest not found."}, status=404)

    try:
        result = create_guest_console_session(request=request, detail=detail)
    except ProxmoxAPIError as exc:
        _audit_guest(request, detail, "guest.console.failed", {"error": str(exc)}, outcome="failed")
        return JsonResponse({"error": str(exc)}, status=400)

    _audit_guest(
        request,
        detail,
        "guest.console.opened",
        {"console_session_id": result.session.id, "proxmox_task_upid": result.session.proxmox_upid},
    )
    return JsonResponse(
        {
            "token": result.token,
            "password": result.password,
            "console_type": result.console_type,
            "websocket_url": f"/console/ws/{result.token}/",
            "expires_at": result.session.expires_at.isoformat(),
        }
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


def _split_kv_config(value: object) -> tuple[str, dict[str, str]]:
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


def _format_kv_config(head: str, params: dict[str, str], order: Iterable[str]) -> str:
    parts = [head] if head else []
    used: set[str] = set()
    for key in order:
        if key in params and params[key] != "":
            parts.append(f"{key}={params[key]}")
            used.add(key)
    for key in sorted(k for k in params if k not in used and params[k] != ""):
        parts.append(f"{key}={params[key]}")
    return ",".join(parts)


def _truthy_config_value(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _set_param_bool(params: dict[str, str], key: str, enabled: bool) -> None:
    if enabled:
        params[key] = "1"
    else:
        params.pop(key, None)


def _set_param_text(params: dict[str, str], key: str, value: str) -> None:
    if value:
        params[key] = value
    else:
        params.pop(key, None)


def _ct_mount_summary(head: str, params: dict[str, str]) -> str:
    mount_path = params.get("mp")
    size = params.get("size")
    bits = [head or "unconfigured"]
    if mount_path:
        bits.append(mount_path)
    if size:
        bits.append(size)
    return " · ".join(bits)


def _disk_size_gib_text(value: object) -> str:
    text = str(value or "").strip()
    match = re.match(r"^(\d+)(?:[Gg](?:i?[Bb])?)?$", text)
    return match.group(1) if match else ""


def _ct_mount_rows(config: dict) -> tuple[dict, list[dict]]:
    root_head, root_params = _split_kv_config(config.get("rootfs"))
    rootfs = {
        "key": "rootfs",
        "source": root_head,
        "size": root_params.get("size", ""),
        "size_gb": _disk_size_gib_text(root_params.get("size")),
        "acl": _truthy_config_value(root_params.get("acl")),
        "quota": _truthy_config_value(root_params.get("quota")),
        "ro": _truthy_config_value(root_params.get("ro")),
        "replicate": _truthy_config_value(root_params.get("replicate")),
        "shared": _truthy_config_value(root_params.get("shared")),
        "mountoptions": root_params.get("mountoptions", ""),
        "summary": _ct_mount_summary(root_head, root_params),
    }
    mounts = []
    for key in sorted((k for k in config if re.match(r"^mp\d+$", k)), key=lambda value: int(value[2:])):
        head, params = _split_kv_config(config.get(key))
        mounts.append(
            {
                "key": key,
                "source": head,
                "path": params.get("mp", ""),
                "size": params.get("size", ""),
                "size_gb": _disk_size_gib_text(params.get("size")),
                "backup": _truthy_config_value(params.get("backup")),
                "acl": _truthy_config_value(params.get("acl")),
                "quota": _truthy_config_value(params.get("quota")),
                "ro": _truthy_config_value(params.get("ro")),
                "replicate": _truthy_config_value(params.get("replicate")),
                "shared": _truthy_config_value(params.get("shared")),
                "mountoptions": params.get("mountoptions", ""),
                "summary": _ct_mount_summary(head, params),
            }
        )
    return rootfs, mounts


def _ct_network_rows(config: dict) -> list[dict]:
    rows = []
    for key in sorted((k for k in config if NET_KEY_RE.match(k)), key=lambda value: int(value[3:])):
        _head, params = _split_kv_config(config.get(key))
        params.setdefault("type", "veth")
        rows.append(
            {
                "key": key,
                "name": params.get("name", key.replace("net", "eth")),
                "bridge": params.get("bridge", ""),
                "firewall": _truthy_config_value(params.get("firewall")),
                "gw": params.get("gw", ""),
                "gw6": params.get("gw6", ""),
                "hwaddr": params.get("hwaddr", ""),
                "ip": params.get("ip", ""),
                "ip6": params.get("ip6", ""),
                "link_down": _truthy_config_value(params.get("link_down")),
                "mtu": params.get("mtu", ""),
                "rate": params.get("rate", ""),
                "tag": params.get("tag", ""),
                "trunks": params.get("trunks", ""),
                "type": params.get("type", "veth"),
                "summary": " · ".join(part for part in (params.get("name"), params.get("bridge"), params.get("ip")) if part),
            }
        )
    return rows


def _ct_features(config: dict) -> dict[str, object]:
    _head, params = _split_kv_config(config.get("features"))
    return {
        "raw": str(config.get("features", "") or ""),
        "mount": params.get("mount", ""),
        "flags": {key: _truthy_config_value(params.get(key)) for key, _label in CT_FEATURE_OPTIONS},
    }


def _ct_options(config: dict) -> dict[str, object]:
    startup = _parse_startup_options(config.get("startup"))
    return {
        "hostname": str(config.get("hostname", "") or ""),
        "description": str(config.get("description", "") or ""),
        "onboot": _config_enabled(config, "onboot"),
        "protection": _config_enabled(config, "protection"),
        "nameserver": str(config.get("nameserver", "") or ""),
        "searchdomain": str(config.get("searchdomain", "") or ""),
        "arch": str(config.get("arch", "") or "amd64"),
        "ostype": str(config.get("ostype", "") or ""),
        "unprivileged": _config_enabled(config, "unprivileged", default=True),
        "startup_order": startup["order"],
        "startup_up": startup["up"],
        "startup_down": startup["down"],
    }


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
    if not settings.VM_WRITE_ENABLED:
        messages.error(request, "VM/CT editing is disabled (VM_WRITE_ENABLED is off).")
        return redirect("core:guest_summary", object_type=object_type, vmid=vmid)

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
    for candidate in common.configured_clients():
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
    for candidate in common.configured_clients():
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
    for candidate in common.configured_clients():
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
            return proxmox_permission_hint("a VM.Config.* privilege")
        return f"Proxmox rejected the change: {exc}"

    record_audit_event(
        request,
        action="guest.config.updated",
        object_type="guest",
        object_id=f"{detail.object_type}:{detail.vmid}",
        details={"fields": changed, "node": node, "vmid": detail.vmid, "target_type": detail.object_type, "name": detail.name},
        system_username="system",
    )
    return True


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
    if request.GET.get("snapshot_partial") == "1":
        return JsonResponse(
            {
                "html": render_to_string("core/partials/guest_snapshot_panel.html", context, request=request),
                "rendered_at_ms": context["snapshot_rendered_at_ms"],
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
    backups, backup_storages, error = _guest_backup_archives(detail)

    jobs = []
    try:
        raw_jobs = common.configured_clients()[0].get("cluster/backup") if common.configured_clients() else []
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
    if request.GET.get("backup_partial") == "1":
        return JsonResponse(
            {
                "html": render_to_string("core/partials/guest_backup_panel.html", context, request=request),
                "rendered_at_ms": int(tz.now().timestamp() * 1000),
            }
        )
    return render(request, "core/guest_backup.html", context)


def _storage_supports_content(storage: dict, content_type: str) -> bool:
    return content_type in {value.strip() for value in str(storage.get("content", "")).split(",") if value.strip()}


def _guest_backup_archives(detail: SimpleNamespace) -> tuple[list[dict], list[dict], str]:
    """Return backup-capable storage and archive records from the endpoint that
    owns this guest.  Storage is node-scoped in PVE, so never assume client 0.
    """
    if not detail.node:
        return [], [], "The guest's node could not be resolved."
    error = ""
    for client in common.configured_clients():
        try:
            # A cheap live request also proves this configured endpoint owns the
            # guest instead of accepting a same-named node on another endpoint.
            client.guest_current(node=detail.node, object_type=detail.object_type, vmid=detail.vmid)
            storages = client.get(f"nodes/{quote(detail.node, safe='')}/storage")
        except ProxmoxAPIError as exc:
            error = str(exc)
            continue
        backup_storages = [
            {"id": str(storage.get("storage") or ""), "label": str(storage.get("storage") or "")}
            for storage in (storages if isinstance(storages, list) else [])
            if storage.get("storage") and _storage_supports_content(storage, "backup") and storage.get("active", 1)
        ]
        backups: list[dict] = []
        for storage in backup_storages:
            try:
                content = client.get(
                    f"nodes/{quote(detail.node, safe='')}/storage/{quote(storage['id'], safe='')}/content?content=backup&vmid={detail.vmid}"
                )
            except ProxmoxAPIError:
                continue
            for entry in content if isinstance(content, list) else []:
                volid = str(entry.get("volid") or "")
                if not volid:
                    continue
                backups.append(
                    {
                        "volid": volid,
                        "size": entry.get("size"),
                        "ctime": datetime.fromtimestamp(int(entry["ctime"]), dt_timezone.utc) if entry.get("ctime") else None,
                        "notes": entry.get("notes", ""),
                        "storage": storage["id"],
                        "source_endpoint": str(getattr(client, "endpoint", "")),
                        "source_type": detail.object_type,
                        "source_vmid": detail.vmid,
                        "source_node": detail.node,
                    }
                )
        backups.sort(key=lambda item: item["ctime"] or datetime.min.replace(tzinfo=dt_timezone.utc), reverse=True)
        return backups, backup_storages, ""
    return [], [], error or "No Proxmox endpoint could read this guest's backup storage."


def _guest_backup_storages(detail: SimpleNamespace) -> tuple[list[dict], str]:
    """Return backup-capable storage without enumerating every archive."""
    if not detail.node:
        return [], "The guest's node could not be resolved."
    error = ""
    for client in common.configured_clients():
        try:
            client.guest_current(node=detail.node, object_type=detail.object_type, vmid=detail.vmid)
            storages = client.get(f"nodes/{quote(detail.node, safe='')}/storage")
        except ProxmoxAPIError as exc:
            error = str(exc)
            continue
        return (
            [
                {"id": str(storage.get("storage") or ""), "label": str(storage.get("storage") or "")}
                for storage in (storages if isinstance(storages, list) else [])
                if storage.get("storage")
                and _storage_supports_content(storage, "backup")
                and storage.get("active", 1)
            ],
            "",
        )
    return [], error or "No Proxmox endpoint could read this guest's backup storage."


@app_login_required
def guest_backup_options(request, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid)
    storages, error = _guest_backup_storages(detail)
    return JsonResponse(
        {
            "storages": storages,
            "error": error,
            "guest": {"type": detail.object_type, "vmid": detail.vmid, "node": detail.node},
        }
    )


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
        raw = common.configured_clients()[0].get("cluster/replication") if common.configured_clients() else []
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
    if common.configured_clients():
        target_nodes = [n for n in common.configured_clients()[0].node_names(fallback="") if n != detail.node]
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


def _vm_write_disabled_redirect(request, object_type: str, vmid: int, redirect_name: str):
    if object_type not in GUEST_OBJECT_TYPES:
        raise Http404("Unknown guest type")
    if settings.VM_WRITE_ENABLED:
        return None
    messages.error(request, "VM/CT write actions are disabled (VM_WRITE_ENABLED is off).")
    return redirect(redirect_name, object_type=object_type, vmid=vmid)


def _guest_kind(detail: SimpleNamespace) -> str:
    return "qemu" if detail.object_type == ProxmoxInventory.ObjectType.VM else "lxc"


def _guest_post_with_client(detail: SimpleNamespace, subpath: str, data: dict | None = None):
    if not detail.node:
        return None, "The guest's node could not be resolved.", None
    err = "No Proxmox endpoint could reach this guest."
    for client in common.configured_clients():
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
    for client in common.configured_clients():
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


def _guest_post_wait_task(detail: SimpleNamespace, subpath: str, data: dict | None = None, *, timeout_seconds: int = SNAPSHOT_TASK_WAIT_SECONDS):
    if not detail.node:
        return None, "The guest's node could not be resolved."
    err = "No Proxmox endpoint could reach this guest."
    for client in common.configured_clients():
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
    for client in common.configured_clients():
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
    for client in common.configured_clients():
        try:
            return client.delete(path), None, client
        except ProxmoxAPIError as exc:
            err = str(exc)
    return None, err, None


def _guest_put(detail: SimpleNamespace, subpath: str, data: dict | None = None):
    if not detail.node:
        return None, "The guest's node could not be resolved."
    err = "No Proxmox endpoint could reach this guest."
    for client in common.configured_clients():
        try:
            return client.put(
                f"nodes/{quote(detail.node, safe='')}/{_guest_kind(detail)}/{detail.vmid}/{subpath}",
                data=data or {},
            ), None
        except ProxmoxAPIError as exc:
            err = str(exc)
    return None, err


def _write_result(request, detail, redirect_name, err, audit_action, audit_details=None):
    if err:
        if "403" in err:
            messages.error(request, proxmox_permission_hint("the required privilege"))
        else:
            messages.error(request, f"Failed: {err}")
    else:
        _audit_guest(request, detail, audit_action, audit_details)
    return redirect(redirect_name, object_type=detail.object_type, vmid=detail.vmid)


@require_POST
@app_login_required
def guest_firewall_options(request, object_type, vmid):
    disabled = _vm_write_disabled_redirect(request, object_type, vmid, "core:guest_firewall")
    if disabled:
        return disabled
    detail = _require_guest(object_type, vmid)
    data = {"enable": "1" if request.POST.get("enable") == "on" else "0"}
    for key in ("policy_in", "policy_out"):
        val = request.POST.get(key, "").strip()
        if val:
            data[key] = val
    _d, err = _guest_put(detail, "firewall/options", data)
    return _write_result(request, detail, "core:guest_firewall", err, "guest.firewall.options")


@require_POST
@app_login_required
def guest_firewall_rule_add(request, object_type, vmid):
    disabled = _vm_write_disabled_redirect(request, object_type, vmid, "core:guest_firewall")
    if disabled:
        return disabled
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
    return _write_result(request, detail, "core:guest_firewall", err, "guest.firewall.rule_add")


@require_POST
@app_login_required
def guest_firewall_rule_delete(request, object_type, vmid, pos):
    disabled = _vm_write_disabled_redirect(request, object_type, vmid, "core:guest_firewall")
    if disabled:
        return disabled
    detail = _require_guest(object_type, vmid)
    _d, err = _guest_delete(detail, f"firewall/rules/{pos}")
    return _write_result(request, detail, "core:guest_firewall", err, "guest.firewall.rule_delete", {"pos": pos})


@require_POST
@app_login_required
def guest_firewall_rule_toggle(request, object_type, vmid, pos):
    disabled = _vm_write_disabled_redirect(request, object_type, vmid, "core:guest_firewall")
    if disabled:
        return disabled
    detail = _require_guest(object_type, vmid)
    enable = "1" if request.POST.get("enable") == "1" else "0"
    _d, err = _guest_put(detail, f"firewall/rules/{pos}", {"enable": enable})
    return _write_result(request, detail, "core:guest_firewall", err, "guest.firewall.rule_toggle", {"pos": pos, "enable": enable})


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


@require_POST
@app_login_required
def guest_backup_now(request, object_type, vmid):
    def result(error_label: str = ""):
        return _guest_action_response(request, object_type, vmid, error_label, redirect_name="core:guest_backup")

    disabled = _vm_write_disabled_redirect(request, object_type, vmid, "core:guest_backup")
    if disabled:
        return result("VM/CT writes are disabled.") if _wants_task_json(request) else disabled
    detail = _require_guest(object_type, vmid)
    response, err, client, audit_details = _submit_guest_backup(request, detail)
    running_event = _audit_guest(request, detail, "guest.backup.run", audit_details, outcome="running")
    if err:
        error_label = _backup_error(err)
        _finish_guest_running_audit(running_event, detail, response, client, err=error_label, audit_details=audit_details)
        return result(error_label)
    _finish_guest_running_audit(
        running_event,
        detail,
        response,
        client,
        audit_details=audit_details,
        timeout_seconds=settings.BACKUP_TASK_TIMEOUT_SECONDS,
    )
    return result()


def _backup_error(err: str) -> str:
    if "403" in err:
        return proxmox_permission_hint("VM.Backup")
    return f"Backup failed: {err}"


def _submit_guest_backup(request, detail: SimpleNamespace):
    storage = request.POST.get("storage", "").strip()
    mode = request.POST.get("mode", "snapshot").strip()
    compress = request.POST.get("compress", "zstd").strip()
    if not storage:
        return None, "Select a backup storage.", None, {}
    if mode not in {"snapshot", "suspend", "stop"}:
        return None, "Choose a valid backup mode.", None, {"storage": storage}
    if compress not in {"zstd", "gzip", "lzo", "0"}:
        return None, "Choose a valid compression mode.", None, {"storage": storage}
    if detail.config.get("lock") or detail.current.get("lock"):
        return None, f"This guest is locked ({detail.config.get('lock') or detail.current.get('lock')}).", None, {"storage": storage}

    body: dict[str, object] = {
        "vmid": detail.vmid,
        "storage": storage,
        "mode": mode,
        "compress": compress,
        "remove": 0,
        "protected": 1 if request.POST.get("protected") in {"1", "on", "true"} else 0,
    }
    notification_mode = request.POST.get("notification_mode", "auto").strip()
    if notification_mode not in {"auto", "legacy-sendmail", "notification-system"}:
        return None, "Choose a valid notification mode.", None, {"storage": storage}
    body["notification-mode"] = notification_mode
    notes_template = request.POST.get("notes_template", "").strip()
    if notes_template:
        body["notes-template"] = notes_template
    audit_details = {
        "storage": storage,
        "mode": mode,
        "compression": compress or "none",
        "protected": bool(body["protected"]),
        "notification_mode": notification_mode,
        "notes_template": notes_template,
    }

    if not detail.node:
        return None, "The guest's node could not be resolved.", None, audit_details
    last_error = "No Proxmox endpoint could reach this guest."
    for client in common.configured_clients():
        try:
            # Resolve storage through the endpoint that currently owns the guest.
            client.guest_current(node=detail.node, object_type=detail.object_type, vmid=detail.vmid)
            storages = client.get(f"nodes/{quote(detail.node, safe='')}/storage")
            match = next(
                (
                    item
                    for item in (storages if isinstance(storages, list) else [])
                    if str(item.get("storage") or "") == storage
                ),
                None,
            )
            if not match or not match.get("active", 1) or not _storage_supports_content(match, "backup"):
                return None, f"Storage '{storage}' is not an active backup storage on {detail.node}.", client, audit_details
            return client.post(f"nodes/{quote(detail.node, safe='')}/vzdump", data=body), None, client, audit_details
        except ProxmoxAPIError as exc:
            last_error = str(exc)
    return None, last_error, None, audit_details


@require_POST
@app_login_required
def guest_backup_delete(request, object_type, vmid):
    def result(error_label: str = ""):
        return _guest_action_response(request, object_type, vmid, error_label, redirect_name="core:guest_backup")

    disabled = _vm_write_disabled_redirect(request, object_type, vmid, "core:guest_backup")
    if disabled:
        return result("VM/CT writes are disabled.") if _wants_task_json(request) else disabled
    detail = _require_guest(object_type, vmid)
    volid = request.POST.get("volid", "").strip()
    storage = request.POST.get("storage", "").strip()
    if not volid or not storage:
        return result("Missing backup reference.")
    response = None
    client = None
    err = "No Proxmox endpoint could reach this guest."
    for client in common.configured_clients():
        try:
            response = client.delete(f"nodes/{quote(detail.node, safe='')}/storage/{quote(storage, safe='')}/content/{quote(volid, safe='')}")
            err = ""
            break
        except ProxmoxAPIError as exc:
            err = str(exc)
    running_event = _audit_guest(request, detail, "guest.backup.delete", {"storage": storage, "volid": volid}, outcome="running")
    if err:
        _finish_guest_running_audit(running_event, detail, response, client, err=f"Delete backup failed: {err}")
        return result(f"Delete backup failed: {err}")
    _finish_guest_running_audit(
        running_event,
        detail,
        response,
        client,
        audit_details={"storage": storage, "volid": volid},
        timeout_seconds=settings.BACKUP_TASK_TIMEOUT_SECONDS,
    )
    return result()


@require_POST
@app_login_required
def guest_replication_create(request, object_type, vmid):
    disabled = _vm_write_disabled_redirect(request, object_type, vmid, "core:guest_replication")
    if disabled:
        return disabled
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
    for client in common.configured_clients():
        try:
            client.post("cluster/replication", data=body)
            err = ""
            break
        except ProxmoxAPIError as exc:
            err = str(exc)
    return _write_result(request, detail, "core:guest_replication", err, "guest.replication.create", {"target": target})


@require_POST
@app_login_required
def guest_replication_delete(request, object_type, vmid):
    disabled = _vm_write_disabled_redirect(request, object_type, vmid, "core:guest_replication")
    if disabled:
        return disabled
    detail = _require_guest(object_type, vmid)
    job_id = request.POST.get("job_id", "").strip()
    if not job_id:
        messages.error(request, "Missing job id.")
        return redirect("core:guest_replication", object_type=object_type, vmid=vmid)
    err = ""
    for client in common.configured_clients():
        try:
            client.delete(f"cluster/replication/{quote(job_id, safe='')}")
            err = ""
            break
        except ProxmoxAPIError as exc:
            err = str(exc)
    return _write_result(request, detail, "core:guest_replication", err, "guest.replication.delete", {"job_id": job_id})


def _audit_guest(request, detail: SimpleNamespace, action: str, details: dict | None = None, *, outcome: str = "success") -> AuditEvent:
    return record_audit_event(
        request,
        action=action,
        object_type="guest",
        object_id=f"{detail.object_type}:{detail.vmid}",
        outcome=outcome,
        details={"node": detail.node, "vmid": detail.vmid, "target_type": detail.object_type, "name": detail.name, **(details or {})},
        system_username="system",
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
        task_id = common.async_task(
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


def _finish_guest_running_audit(
    event: AuditEvent,
    detail: SimpleNamespace,
    response,
    client,
    *,
    err: str = "",
    audit_details: dict | None = None,
    timeout_seconds: int | None = None,
) -> AuditEvent:
    details = dict(event.details if isinstance(event.details, dict) else {})
    details.update(audit_details or {})
    if response is _MIGRATE_ASYNC:
        # A worker task owns this event (multi-disk storage migration); just
        # persist the details and leave it running for the worker to finalize.
        event.details = details
        event.save(update_fields=["details"])
        return event
    if err:
        event.outcome = "failed"
        details["error"] = err
        details["finished_at"] = tz.now().isoformat()
        event.details = details
        event.save(update_fields=["outcome", "details"])
        return event

    if isinstance(response, str) and response.startswith("UPID:") and client is not None:
        details.update(
            {
                "proxmox_task_upid": response,
                "proxmox_task_node": detail.node,
                "proxmox_endpoint": getattr(client, "endpoint", ""),
            }
        )
        task_id = common.async_task(
            "core.tasks.poll_guest_audit_task",
            event.id,
            getattr(client, "endpoint", ""),
            detail.node,
            response,
            timeout_seconds or settings.SCHEDULED_ACTION_TIMEOUT_SECONDS,
        )
        details["poll_task_id"] = task_id
        event.details = details
        event.save(update_fields=["details"])
        return event

    event.outcome = "success"
    details["finished_at"] = tz.now().isoformat()
    event.details = details
    event.save(update_fields=["outcome", "details"])
    return event


def _bulk_action_initial_audit_details(
    request,
    action: str,
    detail: SimpleNamespace,
    snapshot_name: str = "",
) -> tuple[str, dict]:
    if action == "snapshot":
        return "guest.snapshot.create", {"snapshot": snapshot_name}
    if action == "delete_snapshots":
        return "guest.snapshot.delete_all", {}
    if action == "clone":
        newid = request.POST.get("clone_newid", "").strip()
        clone_name = request.POST.get("clone_name", "").strip()
        storage = request.POST.get("clone_storage", "").strip()
        details = {
            "source_vmid": detail.vmid,
            "new_name": clone_name,
            "full": request.POST.get("clone_full") == "1",
            "storage": storage,
        }
        if newid.isdigit():
            details["new_vmid"] = int(newid)
        elif newid:
            details["new_vmid"] = newid
        return "guest.clone.create", details
    if action == "tags":
        return "guest.tags.updated", {
            "mode": request.POST.get("tags_mode", "").strip(),
            "tags": _split_tag_text(request.POST.get("tags_value", "")),
        }
    if action == "agent_enable":
        return "guest.agent.enable", {"agent": "enabled"}
    if action == "agent_disable":
        return "guest.agent.disable", {"agent": "disabled"}
    if action == "destroy":
        return "guest.destroy", {
            "purge": request.POST.get("destroy_purge") == "1",
            "destroy_unreferenced_disks": request.POST.get("destroy_unreferenced_disks") == "1",
        }
    if action == "template":
        return "guest.template.convert", {}
    if action == "untemplate":
        return "guest.template.revert", {}
    if action == "pool":
        return "guest.pool.updated", {"pool": request.POST.get("pool_id", "").strip()}
    if action == "migrate":
        return "guest.migrate", {
            "kind": request.POST.get("migrate_kind", "").strip(),
            "source_node": detail.node,
            "target_node": request.POST.get("migrate_target_node", "").strip(),
            "target_storage": request.POST.get("migrate_target_storage", "").strip(),
        }
    if action == "backup":
        return "guest.backup.run", {
            "storage": request.POST.get("storage", "").strip(),
            "mode": request.POST.get("mode", "snapshot").strip(),
            "compression": request.POST.get("compress", "zstd").strip(),
        }
    return f"guest.power.{action}", {}


def _wants_task_json(request) -> bool:
    """Detail-page action forms post via fetch (so the optimistic Recent Tasks
    row can be updated in place). Plain posts still redirect."""
    return request.headers.get("X-Requested-With") == "fetch"


def _guest_action_response(request, object_type, vmid, error_label="", *, redirect_name):
    if _wants_task_json(request):
        return JsonResponse({"ok": not error_label, "errors": [error_label] if error_label else []})
    if error_label:
        messages.error(request, error_label)
    return redirect(redirect_name, object_type=object_type, vmid=vmid)


@require_POST
@app_login_required
def guest_power(request, object_type: str, vmid: int):
    def result(error_label: str = ""):
        return _guest_action_response(request, object_type, vmid, error_label, redirect_name="core:guest_summary")

    disabled = _vm_write_disabled_redirect(request, object_type, vmid, "core:guest_summary")
    if disabled:
        return result("VM/CT writes are disabled.") if _wants_task_json(request) else disabled
    detail = _require_guest(object_type, vmid)
    action = request.POST.get("action", "")
    if action not in GUEST_POWER_ACTIONS:
        return result("Unknown power action.")
    if action in VM_ONLY_POWER_ACTIONS and object_type != ProxmoxInventory.ObjectType.VM:
        return result("This action is only available for VMs.")
    subpath, params = POWER_ACTION_REQUESTS[action]
    running_event = _audit_guest(request, detail, f"guest.power.{action}", outcome="running")
    data, err, client = _guest_post_with_client(detail, subpath, params)
    if err:
        error_label = proxmox_permission_hint("VM.PowerMgmt") if "403" in err else f"Power action failed: {err}"
        _finish_guest_running_audit(running_event, detail, data, client, err=error_label)
        return result(error_label)
    _finish_guest_running_audit(running_event, detail, data, client)
    clear_live_guest_caches()
    return result()


@require_POST
@app_login_required
def vms_bulk_action(request):
    wants_json = request.headers.get("X-Requested-With") == "fetch" or "application/json" in request.headers.get("Accept", "")

    def done(ok: bool = True, errors: list[str] | None = None):
        if wants_json:
            return JsonResponse({"ok": ok and not errors, "errors": errors or []})
        return None

    if not settings.VM_WRITE_ENABLED:
        response = done(False, ["VM/CT write actions are disabled."])
        if response:
            return response
        return redirect("core:vms_overview")

    action = request.POST.get("bulk_action", "").strip()
    targets = request.POST.getlist("guest")
    if action not in VM_BULK_ACTIONS:
        response = done(False, ["Unknown VM/CT action."])
        if response:
            return response
        return redirect("core:vms_overview")
    if not targets:
        response = done(False, ["No VM/CT targets selected."])
        if response:
            return response
        return redirect("core:vms_overview")

    snapshot_name = request.POST.get("snapshot_name", "").strip()
    if action == "snapshot" and not snapshot_name:
        response = done(False, ["Snapshot name is required."])
        if response:
            return response
        return redirect("core:vms_overview")
    if action == "snapshot" and not SNAPSHOT_NAME_RE.match(snapshot_name):
        if wants_json:
            return done(False, [SNAPSHOT_NAME_HELP])
        messages.error(request, SNAPSHOT_NAME_HELP)
        return redirect("core:vms_overview")
    if action == "clone" and len(targets) != 1:
        response = done(False, ["Clone requires exactly one selected guest."])
        if response:
            return response
        return redirect("core:vms_overview")
    if action == "destroy" and len(targets) != 1:
        response = done(False, ["Destroy requires exactly one selected guest."])
        if response:
            return response
        return redirect("core:vms_overview")
    if action == "untemplate" and len(targets) != 1:
        response = done(False, ["Convert template to VM requires exactly one selected template."])
        if response:
            return response
        return redirect("core:vms_overview")
    # Migrate supports one or many guests for every kind: host/both migrate each
    # guest to the target node, storage moves each guest's volumes to the target
    # storage — all per-guest in the loop below (bulk skips per-guest NIC remap).
    if action == "pool" and "pool_id" not in request.POST:
        response = done(False, ["Choose a target pool or No pool."])
        if response:
            return response
        return redirect("core:vms_overview")
    if action == "tags" and request.POST.get("tags_mode", "").strip() not in {"add", "remove", "replace"}:
        response = done(False, ["Unknown tag update mode."])
        if response:
            return response
        return redirect("core:vms_overview")

    errors = []
    for target in targets:
        object_type, vmid, target_node = _parse_guest_target_value(target)
        if not object_type or vmid is None:
            errors.append(f"Invalid target: {target}")
            continue
        try:
            detail = _require_guest(object_type, vmid, node=target_node)
        except Http404:
            errors.append(f"Could not find target: {target}")
            continue

        audit_action, initial_audit_details = _bulk_action_initial_audit_details(
            request,
            action,
            detail,
            snapshot_name,
        )
        running_event = _audit_guest(request, detail, audit_action, initial_audit_details, outcome="running")
        response = None
        client = None
        if action == "snapshot":
            response, err, client = _guest_post_with_client(detail, "snapshot", {"snapname": snapshot_name})
            audit_details = {"snapshot": snapshot_name}
            error_label = _snapshot_error(err) if err else ""
        elif action == "delete_snapshots":
            deleted, err = _delete_all_guest_snapshots(detail)
            audit_details = {"deleted": deleted}
            error_label = _snapshot_error(err) if err else ""
        elif action == "clone":
            err, audit_details, response, client = _clone_guest_from_bulk_request(request, detail)
            error_label = f"Clone failed: {err}" if err else ""
        elif action == "tags":
            err, audit_details = _update_guest_tags_from_bulk_request(request, detail)
            error_label = f"Tag update failed: {err}" if err else ""
            response = None
            client = None
        elif action in {"agent_enable", "agent_disable"}:
            err, audit_details, response, client = _set_guest_agent_from_bulk_request(
                detail,
                enabled=action == "agent_enable",
            )
            error_label = f"Guest agent update failed: {err}" if err else ""
        elif action == "destroy":
            err, audit_details, response, client = _destroy_guest_from_bulk_request(request, detail)
            error_label = f"Destroy failed: {err}" if err else ""
        elif action == "template":
            if object_type != ProxmoxInventory.ObjectType.VM:
                _finish_guest_running_audit(
                    running_event,
                    detail,
                    None,
                    None,
                    err="Only VMs can be converted to templates.",
                )
                errors.append("Only VMs can be converted to templates.")
                continue
            if detail.vmid in common.fetch_live_guest_lineage():
                msg = (
                    f"{detail.name or detail.vmid} is a linked clone; converting it to a "
                    "template would create a fragile chained lineage. Full-clone it first."
                )
                _finish_guest_running_audit(running_event, detail, None, None, err=msg)
                errors.append(msg)
                continue
            response, err, client = _guest_post_with_client(detail, "template")
            audit_details = None
            error_label = f"Template conversion failed: {err}" if err else ""
        elif action == "untemplate":
            err, audit_details, response, client = _convert_template_back_to_vm(request, detail)
            error_label = f"Template conversion failed: {err}" if err else ""
        elif action == "pool":
            err, audit_details = _move_guest_to_pool_from_bulk_request(request, detail)
            response = None
            client = None
            error_label = f"Pool update failed: {err}" if err else ""
        elif action == "migrate":
            err, audit_details, response, client = _migrate_guest_from_bulk_request(request, detail, running_event)
            error_label = f"Migrate failed: {err}" if err else ""
        elif action == "backup":
            response, err, client, audit_details = _submit_guest_backup(request, detail)
            error_label = _backup_error(err) if err else ""
        else:
            subpath, params = POWER_ACTION_REQUESTS.get(action, (f"status/{action}", {}))
            response, err, client = _guest_post_with_client(detail, subpath, params)
            audit_details = None
            error_label = f"Power action failed: {err}" if err else ""

        if err:
            _finish_guest_running_audit(
                running_event,
                detail,
                response,
                client,
                err=error_label,
                audit_details=audit_details,
            )
            errors.append(error_label)
            continue

        _finish_guest_running_audit(
            running_event,
            detail,
            response,
            client,
            audit_details=audit_details,
            timeout_seconds=settings.BACKUP_TASK_TIMEOUT_SECONDS if action == "backup" else None,
        )
        if action == "template":
            _update_latest_guest_scan_config(detail, {"template": "1"}, [])
        if action == "untemplate":
            _update_latest_guest_scan_config(detail, {"template": "0"}, [])
        if action == "destroy":
            _delete_latest_guest_scan_object(detail)
        if action in GUEST_POWER_ACTIONS or action in {"template", "untemplate", "pool", "migrate", "clone", "tags", "destroy", "agent_enable", "agent_disable", "backup"}:
            clear_live_guest_caches()

    response = done(not errors, errors)
    if response:
        return response

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
    # The clone's disks land on the source's storage (linked / same-storage full)
    # or the chosen target storage; rescan those so the new disks reclassify at once.
    rescan_storages = list(_config_storage_ids(detail.config))
    if storage and full and storage not in rescan_storages:
        rescan_storages.append(storage)
    audit_details = {
        "source_vmid": detail.vmid,
        "new_vmid": int(newid),
        "new_name": clone_name,
        "full": full,
        "storage": storage,
        "rescan_storage_ids": rescan_storages,
    }
    return err or "", audit_details, response, client


def _move_guest_to_pool_from_bulk_request(request, detail: SimpleNamespace) -> tuple[str, dict]:
    """Move one guest between PVE pools with a rollback on a failed add."""
    target_pool = request.POST.get("pool_id", "").strip()
    client = None
    pools: list[str] = []
    memberships: list[str] = []
    for candidate in common.configured_clients():
        try:
            # Resolve both the guest and the pool list through the same endpoint.
            # Pools are cluster-local, not globally shared between configured PVE
            # endpoints.
            candidate.guest_current(node=detail.node, object_type=detail.object_type, vmid=detail.vmid)
            pools, memberships = _guest_pool_memberships(candidate, detail)
            client = candidate
            break
        except ProxmoxAPIError:
            continue
    if client is None:
        return "Could not read the current pool membership from Proxmox.", {}
    if len(memberships) > 1:
        return "This guest appears in multiple pools; resolve that inconsistent Proxmox state before moving it.", {
            "previous_pools": memberships,
            "target_pool": target_pool,
        }
    if target_pool and target_pool not in pools:
        return f"Pool '{target_pool}' no longer exists on this Proxmox endpoint.", {"target_pool": target_pool}

    current_pool = memberships[0] if memberships else ""
    audit_details = {
        "previous_pool": current_pool,
        "target_pool": target_pool,
    }
    if current_pool == target_pool:
        audit_details["noop"] = True
        return "", audit_details

    if current_pool:
        try:
            client.put(f"pools/{quote(current_pool, safe='')}", data={"vms": str(detail.vmid), "delete": 1})
        except ProxmoxAPIError as exc:
            return str(exc), audit_details

    if not target_pool:
        return "", audit_details

    try:
        client.put(f"pools/{quote(target_pool, safe='')}", data={"vms": str(detail.vmid)})
    except ProxmoxAPIError as exc:
        rollback_error = ""
        if current_pool:
            try:
                client.put(f"pools/{quote(current_pool, safe='')}", data={"vms": str(detail.vmid)})
            except ProxmoxAPIError as rollback_exc:
                rollback_error = f" Rollback to '{current_pool}' also failed: {rollback_exc}"
        return f"{exc}.{rollback_error}", audit_details
    return "", audit_details


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


MIGRATE_KINDS = {"host", "storage", "both"}
# Sentinel response: the migrate is being carried out by a worker task that owns
# the audit event's lifecycle, so _finish_guest_running_audit must not finalize it.
_MIGRATE_ASYNC = object()
# States where a VM/CT is not fully stopped, so a host migration must go online
# (VM live migration) or restart (LXC) rather than plain offline.
_MIGRATE_ACTIVE_STATES = {"running", "paused", "hibernated"}


def _migrate_guest_from_bulk_request(request, detail: SimpleNamespace, running_event) -> tuple[str, dict, object | None, object | None]:
    """Issue one Migrate operation (host / storage / both) for a single guest.

    host/both go through the cluster ``migrate`` endpoint (one UPID, so the same
    async poll + cancel path as clone); storage-only relocates **all** of the
    guest's volumes to the target storage via a worker task that runs the
    per-volume ``move_disk`` / ``move_volume`` operations sequentially.
    """
    kind = request.POST.get("migrate_kind", "").strip()
    target_node = request.POST.get("migrate_target_node", "").strip()
    target_storage = request.POST.get("migrate_target_storage", "").strip()
    is_vm = detail.object_type == ProxmoxInventory.ObjectType.VM
    active = str(detail.status or "").strip() in _MIGRATE_ACTIVE_STATES
    audit = {
        "kind": kind,
        "source_node": detail.node,
        "target_node": target_node,
        "target_storage": target_storage,
    }
    if kind not in MIGRATE_KINDS:
        return "Choose what to migrate (host, storage, or both).", audit, None, None
    # Relocating a template's disks (storage / both) would move the base volume
    # out from under its linked clones and orphan their backing chain.
    if kind in {"storage", "both"}:
        children = _linked_clone_children(detail.vmid)
        if children:
            labels = ", ".join(str(child) for child in children)
            return (
                "Cannot move this template's storage — linked clone(s) still depend on its "
                f"base volume: {labels}. Full-clone or delete them first.",
                {**audit, "linked_children": children},
                None,
                None,
            )
    if not detail.node:
        return "Could not resolve the guest's current node.", audit, None, None

    if kind in {"host", "both"}:
        if not target_node:
            return "Choose a target node.", audit, None, None
        if target_node == detail.node:
            return "The target node must differ from the current node.", audit, None, None
        data: dict[str, object] = {"target": target_node}
        # A running VM must migrate online (live); a running CT has no live
        # migration, so use restart migration. Stopped guests migrate offline.
        if active:
            data["online" if is_vm else "restart"] = 1
        if kind == "both":
            if not target_storage:
                return "Choose a target storage.", audit, None, None
            data["targetstorage" if is_vm else "target-storage"] = target_storage
        # Optional NIC bridge remap for bridges missing on the target. Proxmox
        # has no migrate-time network mapping, so this edits the guest config
        # (a cluster-wide, permanent change) before the migrate.
        remap_err, remapped = _apply_migrate_net_remap(request, detail)
        if remap_err:
            return remap_err, audit, None, None
        if remapped:
            audit["net_remap"] = remapped
        response, err, client = _guest_post_with_client(detail, "migrate", data)
        return err or "", audit, response, client

    # kind == "storage": relocate ALL of the guest's volumes to the target
    # storage on the same node, one move at a time (Proxmox locks the guest per
    # move), handed off to a worker task.
    if not target_storage:
        return "Choose a target storage.", audit, None, None
    disks = _guest_movable_disks(detail)
    if not disks:
        return "This guest has no movable disk/volume.", audit, None, None
    moves = [[disk["key"], target_storage] for disk in disks if disk["storage"] != target_storage]
    audit["disks"] = [disk["key"] for disk in disks]
    audit["moves"] = [move[0] for move in moves]
    if not moves:
        audit["noop"] = True
        return "", audit, None, None
    endpoint = ""
    for client in common.configured_clients():
        try:
            client.guest_current(node=detail.node, object_type=detail.object_type, vmid=detail.vmid)
            endpoint = getattr(client, "endpoint", "")
            break
        except ProxmoxAPIError:
            continue
    if not endpoint:
        return "Could not reach the guest's Proxmox endpoint.", audit, None, None
    common.async_task(
        "core.tasks.migrate_guest_disks_task",
        running_event.id,
        endpoint,
        detail.node,
        detail.object_type,
        detail.vmid,
        moves,
        settings.SCHEDULED_ACTION_TIMEOUT_SECONDS,
    )
    return "", audit, _MIGRATE_ASYNC, None


def _guest_movable_disks(detail: SimpleNamespace) -> list[dict]:
    """Owned disks/volumes that a storage-only migration can relocate."""
    config = detail.config if isinstance(detail.config, dict) else {}
    disks: list[dict] = []
    for key in sorted(config):
        value = config[key]
        if not isinstance(value, str):
            continue
        if detail.object_type == ProxmoxInventory.ObjectType.VM:
            # data/system disks + the EFI vars and TPM state volumes (all move via
            # move_disk); skip CD-ROM/cloudinit-style entries.
            if not (_is_disk_device_key(key) or key in ("efidisk0", "tpmstate0")) or "media=cdrom" in value:
                continue
        elif key != "rootfs" and not re.match(r"^mp\d+$", key):
            continue
        storage = value.split(":", 1)[0] if ":" in value else ""
        # A volume with no storage prefix (e.g. an ISO path) can't be moved.
        if not storage:
            continue
        disks.append({"key": key, "storage": storage, "label": f"{key} ({storage})" if storage else key})
    return disks


def _apply_migrate_net_remap(request, detail: SimpleNamespace) -> tuple[str, dict]:
    """Rewrite selected NICs' bridge before a host migration.

    Reads ``migrate_net_remap`` (JSON ``{"net0": "vmbr0", ...}``) and PUTs the
    changed netX lines. Returns ``(error, {net: bridge} applied)``.
    """
    raw = request.POST.get("migrate_net_remap", "").strip()
    if not raw:
        return "", {}
    try:
        remap = json.loads(raw)
    except ValueError:
        return "Invalid network remap request.", {}
    if not isinstance(remap, dict) or not remap:
        return "", {}
    config = detail.config if isinstance(detail.config, dict) else {}
    applied: dict[str, str] = {}
    for net_key in sorted(remap):
        new_bridge = str(remap[net_key] or "").strip()
        if not re.match(r"^net\d+$", str(net_key)) or not new_bridge:
            continue
        current = config.get(net_key)
        if not isinstance(current, str) or "bridge=" not in current:
            continue
        new_value = re.sub(r"(^|,)bridge=[^,]+", lambda m: f"{m.group(1)}bridge={new_bridge}", current)
        if new_value == current:
            continue
        _response, err = _guest_put(detail, "config", {net_key: new_value})
        if err:
            return f"Could not remap {net_key} to '{new_bridge}': {err}", applied
        applied[net_key] = new_bridge
    return "", applied


def _guest_cpu_model(detail: SimpleNamespace) -> str:
    """The VM's configured CPU model (e.g. ``x86-64-v2-AES``, ``host``), or ``""``
    for the portable default. CT has no CPU model (shares the host kernel)."""
    if detail.object_type != ProxmoxInventory.ObjectType.VM:
        return ""
    config = detail.config if isinstance(detail.config, dict) else {}
    raw = config.get("cpu")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    return raw.split(",", 1)[0].strip()


def _node_cpu_models(client, node: str) -> set[str]:
    try:
        caps = client.get(f"nodes/{quote(node, safe='')}/capabilities/qemu/cpu")
    except ProxmoxAPIError:
        return set()
    if not isinstance(caps, list):
        return set()
    return {str(item.get("name")) for item in caps if isinstance(item, dict) and item.get("name")}


def _node_cpu_signature(client, node: str) -> tuple[str, frozenset[str]] | None:
    """(model name, flag set) for a node's physical CPU — used to decide whether a
    ``cpu=host`` guest can be **live**-migrated between two hosts (only safe when
    the exposed CPU is identical)."""
    try:
        status = client.get(f"nodes/{quote(node, safe='')}/status")
    except ProxmoxAPIError:
        return None
    cpuinfo = status.get("cpuinfo") if isinstance(status, dict) else None
    if not isinstance(cpuinfo, dict):
        return None
    return (str(cpuinfo.get("model") or ""), frozenset(str(cpuinfo.get("flags") or "").split()))


def _guest_nic_bridges(detail: SimpleNamespace) -> list[dict]:
    """The guest's NICs and the bridge each is attached to (netX → bridge=...)."""
    config = detail.config if isinstance(detail.config, dict) else {}
    nics: list[dict] = []
    for key in sorted(config):
        if not re.match(r"^net\d+$", key):
            continue
        value = config[key]
        if not isinstance(value, str):
            continue
        match = re.search(r"(?:^|,)bridge=([^,]+)", value)
        if match:
            nics.append({"key": key, "bridge": match.group(1)})
    return nics


def _node_available_bridges(client, node: str, sdn_vnets: set[str]) -> list[str]:
    """Bridges a NIC can attach to on ``node``: Linux/OVS bridges + realized SDN
    vnets. Proxmox has no per-host port-group concept, so a NIC's bridge name
    must exist on the target node or the guest lands without a network there."""
    try:
        raw = client.get(f"nodes/{quote(node, safe='')}/network")
    except ProxmoxAPIError:
        return []
    if not isinstance(raw, list):
        return []
    bridges: set[str] = set()
    for iface in raw:
        if not isinstance(iface, dict):
            continue
        name = str(iface.get("iface") or "")
        if not name:
            continue
        if str(iface.get("type") or "") in {"bridge", "OVSBridge"} or name in sdn_vnets:
            bridges.add(name)
    return sorted(bridges)


def _migrate_not_allowed_reason(reason: object) -> str:
    if isinstance(reason, dict):
        parts: list[str] = []
        for key, label in (
            ("unavailable_storages", "missing storage"),
            ("unavailable_networks", "missing network"),
            ("local_resources", "local resources"),
        ):
            value = reason.get(key)
            if isinstance(value, list) and value:
                parts.append(f"{label}: " + ", ".join(str(item) for item in value))
        return "; ".join(parts) or "not a valid target"
    if reason:
        return str(reason)
    return "not a valid target"


@require_POST
@app_login_required
def guest_bulk_nics(request):
    """Per-guest NIC bridges for a set of guests, for the bulk-migrate network
    preflight (which guests would land without a network on the target node)."""
    if not settings.VM_WRITE_ENABLED:
        return JsonResponse({"error": "VM/CT writes are disabled."}, status=403)
    guests: list[dict] = []
    for value in request.POST.getlist("guest"):
        object_type, vmid, node = _parse_guest_target_value(value)
        if not object_type or vmid is None:
            continue
        try:
            detail = _require_guest(object_type, vmid, node=node)
        except Http404:
            continue
        guests.append(
            {
                "target": value,
                "label": detail.name or str(vmid),
                "bridges": [nic["bridge"] for nic in _guest_nic_bridges(detail)],
            }
        )
    return JsonResponse({"guests": guests})


@app_login_required
def guest_migrate_options(request, object_type: str, vmid: int):
    if not settings.VM_WRITE_ENABLED:
        return JsonResponse({"error": "VM/CT writes are disabled."}, status=403)
    detail = _require_guest(object_type, vmid)
    is_vm = detail.object_type == ProxmoxInventory.ObjectType.VM
    content = "images" if is_vm else "rootdir"
    active = str(detail.status or "").strip() in _MIGRATE_ACTIVE_STATES

    nodes: list[dict] = []
    storages_by_node: dict[str, list[str]] = {}
    storage_free_by_node: dict[str, dict[str, int]] = {}
    bridges_by_node: dict[str, list[str]] = {}
    sdn_vnet_names: list[str] = []
    local_resources: list[str] = []
    for client in common.configured_clients():
        try:
            raw_nodes = client.get("nodes")
        except ProxmoxAPIError:
            continue
        if not isinstance(raw_nodes, list):
            continue
        try:
            sdn_vnets = {
                str(vnet.get("vnet"))
                for vnet in client.get("cluster/sdn/vnets")
                if isinstance(vnet, dict) and vnet.get("vnet")
            }
        except ProxmoxAPIError:
            sdn_vnets = set()
        # SDN vnets are cluster-scoped, so any of them can be assigned to a NIC on
        # any node (the per-node realized set below still drives the warning).
        sdn_vnet_names = sorted(sdn_vnets)
        # Proxmox migration preconditions give the real allowed/blocked target
        # set + reasons (missing storage/bridge, passthrough, ...). Defensive:
        # if the endpoint can't answer, fall back to "all online nodes allowed".
        allowed = None
        not_allowed: dict = {}
        try:
            pre = client.get(f"nodes/{quote(detail.node, safe='')}/{_guest_kind(detail)}/{detail.vmid}/migrate")
        except ProxmoxAPIError:
            pre = None
        if isinstance(pre, dict):
            if isinstance(pre.get("allowed_nodes"), list):
                allowed = [str(item) for item in pre["allowed_nodes"]]
            if isinstance(pre.get("not_allowed_nodes"), dict):
                not_allowed = pre["not_allowed_nodes"]
            if isinstance(pre.get("local_resources"), list):
                local_resources = [str(item) for item in pre["local_resources"]]

        cpu_model = _guest_cpu_model(detail)
        # For cpu=host, live migration is only safe between hosts exposing an
        # identical CPU; capture the source signature to compare each target.
        source_cpu_sig = _node_cpu_signature(client, detail.node) if cpu_model == "host" else None
        node_names: list[str] = []
        for node in raw_nodes:
            if not isinstance(node, dict) or not node.get("node"):
                continue
            name = str(node["node"])
            node_names.append(name)
            if name == detail.node:
                continue
            online = str(node.get("status") or "") == "online"
            entry = {
                "node": name,
                "online": online,
                "allowed": True,
                "reason": "",
                "cpu_ok": True,
                "cpu_reason": "",
                "host_cpu_match": True,
                "host_cpu_reason": "",
            }
            if not online:
                entry["allowed"] = False
                entry["reason"] = "node offline"
            elif allowed is not None and name not in allowed:
                entry["allowed"] = False
                entry["reason"] = _migrate_not_allowed_reason(not_allowed.get(name))
            # EVC-lite: a named CPU model must be runnable on the target. Proxmox's
            # own precondition does not check this. Default (unset) is portable.
            if online and cpu_model and cpu_model != "host" and cpu_model not in _node_cpu_models(client, name):
                entry["cpu_ok"] = False
                entry["cpu_reason"] = f"CPU model '{cpu_model}' is not available on {name}"
            # cpu=host: only safe to live-migrate to an identical CPU. Compare on
            # the CPU model so two identical hosts stay silent (a trivial flag/
            # microcode delta shouldn't nag), while a real mismatch (e.g. Intel →
            # AMD) is flagged.
            if online and source_cpu_sig is not None:
                target_sig = _node_cpu_signature(client, name)
                if target_sig is None or target_sig[0] != source_cpu_sig[0]:
                    entry["host_cpu_match"] = False
                    src_model = source_cpu_sig[0] or "source CPU"
                    tgt_model = (target_sig[0] if target_sig else "") or "target CPU"
                    entry["host_cpu_reason"] = f"host CPUs differ ({src_model} → {tgt_model})"
            nodes.append(entry)

        for name in node_names:
            try:
                raw_storages = client.get(f"nodes/{quote(name, safe='')}/storage")
            except ProxmoxAPIError:
                continue
            if not isinstance(raw_storages, list):
                continue
            ids: list[str] = []
            free: dict[str, int] = {}
            for storage in raw_storages:
                if not isinstance(storage, dict) or not storage.get("storage"):
                    continue
                contents = {item.strip() for item in str(storage.get("content", "")).split(",")}
                if content in contents and str(storage.get("active", "1")) != "0":
                    storage_id = str(storage["storage"])
                    ids.append(storage_id)
                    try:
                        free[storage_id] = int(storage.get("avail"))
                    except (TypeError, ValueError):
                        pass
            storages_by_node[name] = sorted(set(ids))
            storage_free_by_node[name] = free
            bridges_by_node[name] = _node_available_bridges(client, name, sdn_vnets)
        break

    return JsonResponse(
        {
            "object_type": object_type,
            "current_node": detail.node,
            "running": active,
            "nodes": nodes,
            "disks": _guest_movable_disks(detail),
            "guest_nics": _guest_nic_bridges(detail),
            "guest_cpu": _guest_cpu_model(detail),
            "guest_disk_bytes": _config_disk_bytes(detail.config),
            "storages_by_node": storages_by_node,
            "storage_free_by_node": storage_free_by_node,
            "bridges_by_node": bridges_by_node,
            "sdn_vnets": sdn_vnet_names,
            "local_resources": local_resources,
        }
    )


def _convert_template_back_to_vm(request, detail: SimpleNamespace) -> tuple[str, dict, object | None, object | None]:
    """Safely clear the QEMU template flag for a standalone template.

    Proxmox accepts ``template=0`` even when linked clones still reference a
    template base disk.  The API does not protect that relationship, so this
    deliberately has narrow V1 support and fails closed when it cannot prove
    the template has no children on every backing storage.
    """
    audit_details: dict[str, object] = {"operation": "template_to_vm"}
    confirmation = request.POST.get("untemplate_confirm_vmid", "").strip()
    acknowledgement = request.POST.get("untemplate_acknowledge", "").strip()
    if confirmation != str(detail.vmid):
        return "The confirmation VMID did not match.", audit_details, None, None
    if acknowledgement != "convert":
        return "Confirm that you understand this converts the template back to a VM.", audit_details, None, None
    if detail.object_type != ProxmoxInventory.ObjectType.VM:
        return "Only VM templates can be converted back to VMs.", audit_details, None, None
    if not detail.node:
        return "Could not resolve the template's current node.", audit_details, None, None

    client = None
    fresh_config: dict = {}
    current: dict = {}
    for candidate in common.configured_clients():
        try:
            fresh_config = candidate.guest_config(node=detail.node, object_type=detail.object_type, vmid=detail.vmid)
            current = candidate.guest_current(node=detail.node, object_type=detail.object_type, vmid=detail.vmid)
            client = candidate
            break
        except ProxmoxAPIError:
            continue
    if client is None:
        return "Could not read the template's current configuration from Proxmox.", audit_details, None, None

    if not is_template(fresh_config):
        return "This guest is no longer a template.", audit_details, None, client
    status = str(current.get("status") or "")
    if status != "stopped":
        return "Stop the template before converting it back to a VM.", {**audit_details, "status": status}, None, client
    if fresh_config.get("lock"):
        return (
            f"Template is locked by another Proxmox operation ({fresh_config.get('lock')}).",
            audit_details,
            None,
            client,
        )
    if _config_enabled(fresh_config, "protection"):
        return "Disable protection before converting this template back to a VM.", audit_details, None, client

    try:
        snapshots = client.get(f"nodes/{quote(detail.node, safe='')}/qemu/{detail.vmid}/snapshot")
    except ProxmoxAPIError as exc:
        return f"Could not verify template snapshots: {exc}", audit_details, None, client
    if not isinstance(snapshots, list):
        return "Could not verify template snapshots: unexpected Proxmox response.", audit_details, None, client
    snapshot_names = [
        str(snapshot.get("name") or "")
        for snapshot in snapshots if isinstance(snapshot, dict)
        if str(snapshot.get("name") or "") not in {"", "current"}
    ]
    if snapshot_names:
        return "Remove template snapshots before converting it back to a VM.", {**audit_details, "snapshots": snapshot_names}, None, client

    disk_references = extract_disk_references(fresh_config)
    if not disk_references:
        return "This template has no supported disk volumes to validate.", audit_details, None, client
    storage_paths, storage_error = _template_storage_paths(disk_references)
    if storage_error:
        return storage_error, audit_details, None, client

    children, child_error = _template_linked_clone_children(client, detail.node, storage_paths)
    if child_error:
        return child_error, audit_details, None, client
    if children:
        child_labels = ", ".join(sorted({str(child.get("vmid") or "unknown") for child in children}))
        return (
            f"Cannot convert this template back to a VM because linked clone(s) still depend on it: {child_labels}.",
            {**audit_details, "linked_children": children},
            None,
            client,
        )

    audit_details["storage_ids"] = sorted(storage_paths)
    try:
        client.set_guest_config(
            node=detail.node,
            object_type=detail.object_type,
            vmid=detail.vmid,
            updates={"template": "0"},
            delete=[],
            digest=fresh_config.get("digest"),
        )
    except ProxmoxAPIError as exc:
        return str(exc), audit_details, None, client
    return "", audit_details, None, client


def _template_storage_paths(disk_references: list[str]) -> tuple[dict[str, set[str]], str]:
    """Return template-disk paths by storage, limited to app-mounted file storage."""
    mounted_storages = {
        storage.storage_id: storage
        for storage in StorageMount.objects.filter(enabled=True).only("storage_id", "path")
    }
    paths: dict[str, set[str]] = {}
    for reference in disk_references:
        storage_id, separator, relative_path = str(reference).partition(":")
        normalized = _normalized_storage_relative_path(relative_path)
        storage = mounted_storages.get(storage_id)
        if not separator or not normalized or storage is None or not Path(storage.path).is_dir():
            return {}, (
                "Template-to-VM conversion currently supports only disk volumes on configured, mounted file storage. "
                f"Unsupported volume: {reference}."
            )
        paths.setdefault(storage_id, set()).add(normalized)
    return paths, ""


def _template_linked_clone_children(client, node: str, storage_paths: dict[str, set[str]]) -> tuple[list[dict], str]:
    children: list[dict] = []
    for storage_id, template_paths in storage_paths.items():
        try:
            content = client.get(
                f"nodes/{quote(node, safe='')}/storage/{quote(storage_id, safe='')}/content"
            )
        except ProxmoxAPIError as exc:
            return [], f"Could not verify linked clones on storage '{storage_id}': {exc}"
        if not isinstance(content, list):
            return [], f"Could not verify linked clones on storage '{storage_id}': unexpected Proxmox response."
        for item in content:
            if not isinstance(item, dict):
                continue
            parent = str(item.get("parent") or "")
            if not parent:
                continue
            child_vmid = item.get("vmid")
            candidates = _linked_parent_candidates(parent, child_vmid)
            if candidates.intersection(template_paths):
                children.append(
                    {
                        "vmid": child_vmid,
                        "volid": str(item.get("volid") or ""),
                        "parent": parent,
                    }
                )
    return children, ""


def _normalized_storage_relative_path(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if ":" in text:
        _storage, _separator, text = text.partition(":")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        return ""
    return posixpath.normpath(str(path))


def _linked_parent_candidates(parent: str, child_vmid: object) -> set[str]:
    """Normalize PVE's relative ``parent`` values against a child VMID.

    The content API reports linked-clone parents as e.g.
    ``../102/base-102-disk-0.qcow2`` for VMID 103.  Some backends return the
    direct relative form instead, so accept both representations.
    """
    raw = str(parent or "").strip()
    if ":" in raw:
        _storage, _separator, raw = raw.partition(":")
    if not raw or raw.startswith("/"):
        return set()
    candidates = {posixpath.normpath(raw)}
    if child_vmid not in {None, ""}:
        candidates.add(posixpath.normpath(posixpath.join(str(child_vmid), raw)))
    return {candidate for candidate in candidates if candidate and not candidate.startswith("../")}


def _destroy_guest_from_bulk_request(request, detail: SimpleNamespace) -> tuple[str, dict, object | None, object | None]:
    confirm_vmid = request.POST.get("destroy_confirm_vmid", "").strip()
    if confirm_vmid != str(detail.vmid):
        return "The confirmation VMID did not match.", {}, None, None
    if detail.status == "running":
        return "Stop the guest before destroying it.", {"status": detail.status}, None, None
    # A template whose base volume still backs linked clones must not be destroyed:
    # Proxmox refuses it anyway, but fail early with a clear message.
    children = _linked_clone_children(detail.vmid)
    if children:
        labels = ", ".join(str(child) for child in children)
        return (
            "Cannot destroy this template — linked clone(s) still depend on its base "
            f"volume: {labels}. Delete the linked clones first.",
            {"linked_children": children},
            None,
            None,
        )

    purge = request.POST.get("destroy_purge") == "1"
    destroy_unreferenced_disks = request.POST.get("destroy_unreferenced_disks") == "1"
    params = {"purge": "1" if purge else "0"}
    if detail.object_type == ProxmoxInventory.ObjectType.VM:
        params["destroy-unreferenced-disks"] = "1" if destroy_unreferenced_disks else "0"
    query = urlencode(params)
    response, err, client = _guest_destroy_with_client(detail, query)
    return (
        err or "",
        {
            "purge": purge,
            "destroy_unreferenced_disks": destroy_unreferenced_disks,
            # Rescan the freed storage so the removed disks drop out of inventory.
            "rescan_storage_ids": list(_config_storage_ids(detail.config)),
        },
        response,
        client,
    )


def _update_guest_tags_from_bulk_request(request, detail: SimpleNamespace) -> tuple[str, dict]:
    mode = request.POST.get("tags_mode", "").strip()
    requested_tags = _split_tag_text(request.POST.get("tags_value", ""))
    if mode not in {"add", "remove", "replace"}:
        return "Unknown tag operation.", {}
    if mode in {"add", "remove"} and not requested_tags:
        return "Enter at least one tag.", {"mode": mode, "tags": requested_tags}

    client = None
    fresh: dict = {}
    for candidate in common.configured_clients():
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


def _set_guest_agent_from_bulk_request(
    detail: SimpleNamespace,
    *,
    enabled: bool,
) -> tuple[str, dict, object | None, object | None]:
    audit_details = {"agent": "enabled" if enabled else "disabled"}
    if detail.object_type != ProxmoxInventory.ObjectType.VM:
        return "Guest agent applies to VMs only.", audit_details, None, None

    client = None
    fresh: dict = {}
    for candidate in common.configured_clients():
        try:
            fresh = candidate.guest_config(node=detail.node, object_type=detail.object_type, vmid=detail.vmid)
            client = candidate
            break
        except ProxmoxAPIError:
            continue
    if client is None:
        return "Could not read the current guest config from Proxmox.", audit_details, None, None
    if fresh.get("lock"):
        return f"Guest is locked by another Proxmox operation ({fresh.get('lock')}).", audit_details, None, None

    currently_enabled = _guest_agent_config_enabled(fresh, detail.object_type)
    audit_details["previous_agent"] = "enabled" if currently_enabled else "disabled"
    if currently_enabled == enabled:
        audit_details["noop"] = True
        return "", audit_details, None, client

    updates = {"agent": "1" if enabled else "0"}
    try:
        client.set_guest_config(
            node=detail.node,
            object_type=detail.object_type,
            vmid=detail.vmid,
            updates=updates,
            delete=[],
            digest=fresh.get("digest"),
        )
    except ProxmoxAPIError as exc:
        return str(exc), audit_details, None, client

    _update_latest_guest_scan_config(detail, updates, [])
    return "", audit_details, None, client


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
    for client in common.configured_clients():
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

    used_vmids = sorted(
        {guest.vmid for guest in common.fetch_live_guest_inventory() if guest.vmid is not None}
    )

    return JsonResponse(
        {
            "nextid": nextid,
            "used_vmids": used_vmids,
            "storages": [{"id": storage, "label": storage} for storage in storages],
            "default_storage": default_storage,
            "source_storages": source_storages,
            "suggested_name": f"{detail.name}-clone" if detail.name else "",
            # Linked clones are only supported from a template; a regular guest
            # must be full-cloned (Proxmox rejects linked otherwise).
            "is_template": is_template(detail.config),
        }
    )


@app_login_required
def guest_pool_options(request, object_type: str, vmid: int):
    if not settings.VM_WRITE_ENABLED:
        return JsonResponse({"error": "VM/CT writes are disabled."}, status=403)
    detail = _require_guest(object_type, vmid)
    for client in common.configured_clients():
        try:
            client.guest_current(node=detail.node, object_type=detail.object_type, vmid=detail.vmid)
            pools, memberships = _guest_pool_memberships(client, detail)
            return JsonResponse(
                {
                    "pools": [{"id": pool_id, "label": pool_id} for pool_id in pools],
                    "current_pool": memberships[0] if len(memberships) == 1 else "",
                    "multiple_memberships": memberships if len(memberships) > 1 else [],
                }
            )
        except ProxmoxAPIError:
            continue
    return JsonResponse({"error": "Could not load pools from the guest's Proxmox endpoint."}, status=502)


@require_POST
@app_login_required
def guest_snapshot_create(request, object_type: str, vmid: int):
    def result(error_label: str = ""):
        return _guest_action_response(request, object_type, vmid, error_label, redirect_name="core:guest_snapshots")

    disabled = _vm_write_disabled_redirect(request, object_type, vmid, "core:guest_snapshots")
    if disabled:
        return result("VM/CT writes are disabled.") if _wants_task_json(request) else disabled
    detail = _require_guest(object_type, vmid)
    name = request.POST.get("snapname", "").strip()
    if not name:
        return result("Snapshot name is required.")
    if not SNAPSHOT_NAME_RE.match(name):
        return result(SNAPSHOT_NAME_HELP)
    data = {"snapname": name}
    description = request.POST.get("description", "").strip()
    if description:
        data["description"] = description
    # vmstate (include RAM) only exists for QEMU VMs; LXC has no such option.
    if object_type == ProxmoxInventory.ObjectType.VM and request.POST.get("vmstate") == "on":
        data["vmstate"] = 1
    running_event = _audit_guest(request, detail, "guest.snapshot.create", {"snapshot": name}, outcome="running")
    response, err, client = _guest_post_with_client(detail, "snapshot", data)
    if err:
        error_label = _snapshot_error(err)
        _finish_guest_running_audit(running_event, detail, response, client, err=error_label, audit_details={"snapshot": name})
        return result(error_label)
    _finish_guest_running_audit(running_event, detail, response, client, audit_details={"snapshot": name})
    return result()


@require_POST
@app_login_required
def guest_snapshot_delete(request, object_type: str, vmid: int, snapname: str):
    def result(error_label: str = ""):
        return _guest_action_response(request, object_type, vmid, error_label, redirect_name="core:guest_snapshots")

    disabled = _vm_write_disabled_redirect(request, object_type, vmid, "core:guest_snapshots")
    if disabled:
        return result("VM/CT writes are disabled.") if _wants_task_json(request) else disabled
    detail = _require_guest(object_type, vmid)
    running_event = _audit_guest(request, detail, "guest.snapshot.delete", {"snapshot": snapname}, outcome="running")
    response, err, client = _guest_delete_with_client(detail, f"snapshot/{quote(snapname, safe='')}")
    if err:
        error_label = _snapshot_error(err)
        _finish_guest_running_audit(running_event, detail, response, client, err=error_label, audit_details={"snapshot": snapname})
        return result(error_label)
    _finish_guest_running_audit(running_event, detail, response, client, audit_details={"snapshot": snapname})
    return result()


@require_POST
@app_login_required
def guest_snapshot_delete_all(request, object_type: str, vmid: int):
    def result(error_label: str = ""):
        return _guest_action_response(request, object_type, vmid, error_label, redirect_name="core:guest_snapshots")

    disabled = _vm_write_disabled_redirect(request, object_type, vmid, "core:guest_snapshots")
    if disabled:
        return result("VM/CT writes are disabled.") if _wants_task_json(request) else disabled
    detail = _require_guest(object_type, vmid)
    running_event = _audit_guest(request, detail, "guest.snapshot.delete_all", outcome="running")
    deleted, err = _delete_all_guest_snapshots(detail)
    if err:
        error_label = _snapshot_error(err)
        _finish_guest_running_audit(running_event, detail, None, None, err=error_label)
        return result(error_label)
    _finish_guest_running_audit(running_event, detail, None, None, audit_details={"deleted": deleted})
    return result()


@require_POST
@app_login_required
def guest_snapshot_rollback(request, object_type: str, vmid: int, snapname: str):
    def result(error_label: str = ""):
        return _guest_action_response(request, object_type, vmid, error_label, redirect_name="core:guest_snapshots")

    disabled = _vm_write_disabled_redirect(request, object_type, vmid, "core:guest_snapshots")
    if disabled:
        return result("VM/CT writes are disabled.") if _wants_task_json(request) else disabled
    detail = _require_guest(object_type, vmid)
    running_event = _audit_guest(request, detail, "guest.snapshot.rollback", {"snapshot": snapname}, outcome="running")
    response, err, client = _guest_post_with_client(detail, f"snapshot/{quote(snapname, safe='')}/rollback")
    if err:
        error_label = _snapshot_error(err)
        _finish_guest_running_audit(running_event, detail, response, client, err=error_label, audit_details={"snapshot": snapname})
        return result(error_label)
    _finish_guest_running_audit(running_event, detail, response, client, audit_details={"snapshot": snapname})
    return result()


def _snapshot_error(err: str) -> str:
    if "403" in err:
        return proxmox_permission_hint("VM.Snapshot (and VM.Snapshot.Rollback for rollback)")
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
            return proxmox_permission_hint("VM.Allocate + Datastore.AllocateSpace (+ SDN.Use for the NIC)")
        return f"Creation failed: {err}"

    record_audit_event(
        request,
        action="guest.create",
        object_type="guest",
        object_id=f"{object_type}:{vmid}",
        details={"node": node, "vmid": vmid, "target_type": object_type, "name": post.get("name") or post.get("hostname") or ""},
        system_username="system",
    )
    return None


def _backup_archive_type(volid: str) -> str:
    name = str(volid).rsplit("/", 1)[-1]
    if "vzdump-qemu-" in name:
        return ProxmoxInventory.ObjectType.VM
    if "vzdump-lxc-" in name:
        return ProxmoxInventory.ObjectType.CT
    return ""


def _backup_archive_vmid(volid: str) -> int | None:
    match = re.search(r"(?:^|[:/])vzdump-(?:qemu|lxc)-(\d+)-", str(volid))
    return int(match.group(1)) if match else None


def _restore_options() -> tuple[list[dict], list[dict], dict[str, dict[str, list[str]]], str]:
    """Discover restoreable archives and compatible target storages live.

    Archive visibility is deliberately evaluated per node. A local backup on
    pve3 must not be presented as restorable on pve99 just because the storage
    IDs happen to share a name.
    """
    archives: list[dict] = []
    nodes: list[dict] = []
    storage_options: dict[str, dict[str, list[str]]] = {}
    nextid = ""
    seen_nodes: set[str] = set()
    seen_archives: set[tuple[str, str, str]] = set()
    for client in common.configured_clients():
        endpoint = str(getattr(client, "endpoint", ""))
        try:
            client_nodes = client.node_names(fallback="")
            if not nextid:
                nextid = str(client.get("cluster/nextid"))
        except ProxmoxAPIError:
            continue
        for node in client_nodes:
            node_key = f"{endpoint}|{node}"
            if node_key not in seen_nodes:
                seen_nodes.add(node_key)
                nodes.append({"key": node_key, "label": node, "node": node, "endpoint": endpoint})
            try:
                storages = client.get(f"nodes/{quote(node, safe='')}/storage")
            except ProxmoxAPIError:
                continue
            node_types = storage_options.setdefault(node_key, {"vm": [], "ct": []})
            for storage in storages if isinstance(storages, list) else []:
                storage_id = str(storage.get("storage") or "")
                if not storage_id or not storage.get("active", 1):
                    continue
                if _storage_supports_content(storage, "images") and storage_id not in node_types["vm"]:
                    node_types["vm"].append(storage_id)
                if _storage_supports_content(storage, "rootdir") and storage_id not in node_types["ct"]:
                    node_types["ct"].append(storage_id)
                if not _storage_supports_content(storage, "backup"):
                    continue
                try:
                    entries = client.get(
                        f"nodes/{quote(node, safe='')}/storage/{quote(storage_id, safe='')}/content?content=backup"
                    )
                except ProxmoxAPIError:
                    continue
                for entry in entries if isinstance(entries, list) else []:
                    volid = str(entry.get("volid") or "")
                    object_type = _backup_archive_type(volid)
                    # Shared backup storage exposes the same archive through
                    # every cluster node. It is still one archive in the UI.
                    key = (endpoint, storage_id, volid)
                    if not object_type or key in seen_archives:
                        continue
                    seen_archives.add(key)
                    ctime = datetime.fromtimestamp(int(entry["ctime"]), dt_timezone.utc) if entry.get("ctime") else None
                    archive_key = "|".join((endpoint, node, storage_id, volid))
                    archives.append(
                        {
                            "key": archive_key,
                            "endpoint": endpoint,
                            "node": node,
                            "storage": storage_id,
                            "volid": volid,
                            "name": volid.rsplit("/", 1)[-1],
                            "source_vmid": _backup_archive_vmid(volid),
                            "object_type": object_type,
                            "type_label": "VM" if object_type == "vm" else "CT",
                            "ctime": ctime,
                            "size": entry.get("size"),
                            "notes": entry.get("notes", ""),
                        }
                    )
    archives.sort(key=lambda item: item["ctime"] or datetime.min.replace(tzinfo=dt_timezone.utc), reverse=True)
    duplicate_names = {item["node"] for item in nodes if sum(other["node"] == item["node"] for other in nodes) > 1}
    for item in nodes:
        if item["node"] in duplicate_names:
            item["label"] = f"{item['node']} · {item['endpoint']}"
    nodes.sort(key=lambda item: (item["node"].casefold(), item["endpoint"]))
    return archives, nodes, storage_options, nextid


def _restore_archive_from_key(key: str, archives: list[dict]) -> dict | None:
    exact = next((archive for archive in archives if archive["key"] == key), None)
    if exact is not None:
        return exact
    endpoint_parts = key.split("|", 3)
    if len(endpoint_parts) == 4:
        endpoint, _node, storage, volid = endpoint_parts
        return next(
            (
                archive
                for archive in archives
                if archive["endpoint"] == endpoint and archive["storage"] == storage and archive["volid"] == volid
            ),
            None,
        )
    # Archive links from the guest Backup tab intentionally omit the endpoint
    # URL. Resolve them from fresh discovery rather than trusting query data.
    parts = key.split("|", 2)
    if len(parts) == 3:
        node, storage, volid = parts
        matches = [
            archive
            for archive in archives
            if archive["node"] == node and archive["storage"] == storage and archive["volid"] == volid
        ]
        return matches[0] if len(matches) == 1 else None
    return None


def _restore_client(endpoint: str):
    for client in common.configured_clients():
        if str(getattr(client, "endpoint", "")) == endpoint:
            return client
    return None


@app_login_required
def guest_backup_restore(request):
    if not settings.VM_WRITE_ENABLED:
        messages.error(request, "VM/CT restore is disabled (VM_WRITE_ENABLED is off).")
        return redirect("core:vms")
    archives, nodes, storage_options, nextid = _restore_options()
    restore_error = ""
    selected_archive_key = request.POST.get("archive_key", "") if request.method == "POST" else request.GET.get("archive", "")
    source_type = (request.POST.get("source_type", "") if request.method == "POST" else request.GET.get("source_type", "")).strip()
    source_vmid_text = (
        request.POST.get("source_vmid", "") if request.method == "POST" else request.GET.get("source_vmid", "")
    ).strip()
    source_vmid = int(source_vmid_text) if source_vmid_text.isdigit() and int(source_vmid_text) > 0 else None
    if source_type not in {ProxmoxInventory.ObjectType.VM, ProxmoxInventory.ObjectType.CT}:
        source_type = ""
        source_vmid = None
    if request.method != "POST" and not selected_archive_key:
        storage_hint = request.GET.get("storage", "").strip()
        path_hint = request.GET.get("path", "").strip()
        hinted_volid = f"{storage_hint}:{path_hint}" if storage_hint and path_hint else ""
        selected_archive_key = next(
            (
                archive["key"]
                for archive in archives
                if archive["storage"] == storage_hint and archive["volid"] == hinted_volid
            ),
            "",
        )
    selected_archive = _restore_archive_from_key(selected_archive_key, archives) if selected_archive_key else None
    if selected_archive is not None and source_vmid is None:
        source_type = selected_archive["object_type"]
        source_vmid = selected_archive.get("source_vmid")
    if source_type and source_vmid:
        archives = [
            archive
            for archive in archives
            if archive["object_type"] == source_type and archive.get("source_vmid") == source_vmid
        ]
    elif selected_archive is not None:
        archives = [
            archive
            for archive in archives
            if archive["object_type"] == selected_archive["object_type"]
            and archive.get("source_vmid") == selected_archive.get("source_vmid")
        ]
    if request.method == "POST":
        error = _queue_guest_backup_restore(request, archives)
        if error:
            restore_error = error
        else:
            return redirect("core:vms")
    context = {
        **navigation_context("vms"),
        "archives": archives,
        "nodes": nodes,
        "storage_options": storage_options,
        "nextid": nextid,
        "selected_archive_key": selected_archive_key,
        "restore_error": restore_error,
        "source_type": source_type,
        "source_vmid": source_vmid or "",
        "form_values": request.POST
        if request.method == "POST"
        else {"node": nodes[0]["key"] if nodes else "", "vmid": nextid},
    }
    return render(request, "core/guest_backup_restore.html", context)


def _queue_guest_backup_restore(request, archives: list[dict]) -> str:
    archive = _restore_archive_from_key(request.POST.get("archive_key", ""), archives)
    if archive is None:
        return "Select a backup archive that is still available."
    target_node_key = request.POST.get("node", "").strip()
    target_storage = request.POST.get("storage", "").strip()
    vmid_text = request.POST.get("vmid", "").strip()
    overwrite = request.POST.get("overwrite") in {"1", "on", "true"}
    start_after = request.POST.get("start_after") in {"1", "on", "true"}
    if "|" not in target_node_key or not target_storage:
        return "Choose a target node and target storage."
    target_endpoint, target_node = target_node_key.rsplit("|", 1)
    if not vmid_text.isdigit() or int(vmid_text) <= 0:
        return "VMID must be a positive whole number."
    vmid = int(vmid_text)
    if overwrite and request.POST.get("overwrite_confirm", "").strip() != vmid_text:
        return f"Enter {vmid} to confirm replacement of the existing guest."

    client = _restore_client(str(archive.get("key", "")).split("|", 1)[0])
    if client is None:
        return "The Proxmox endpoint that exposes this archive is unavailable."
    if target_endpoint != str(getattr(client, "endpoint", "")):
        return "The target node must belong to the Proxmox endpoint that exposes the backup archive."
    try:
        target_storages = client.get(f"nodes/{quote(target_node, safe='')}/storage")
        target_match = next(
            (item for item in (target_storages if isinstance(target_storages, list) else []) if str(item.get("storage") or "") == target_storage),
            None,
        )
        content_type = "images" if archive["object_type"] == ProxmoxInventory.ObjectType.VM else "rootdir"
        if not target_match or not target_match.get("active", 1) or not _storage_supports_content(target_match, content_type):
            return f"Storage '{target_storage}' cannot hold {archive['type_label']} disks on {target_node}."
        archive_entries = client.get(
            f"nodes/{quote(target_node, safe='')}/storage/{quote(str(archive['storage']), safe='')}/content?content=backup"
        )
        if not any(str(entry.get("volid") or "") == archive["volid"] for entry in archive_entries if isinstance(entry, dict)):
            return f"Archive {archive['volid']} is not accessible from {target_node}."
    except ProxmoxAPIError as exc:
        return f"Restore preflight failed: {exc}"

    live_guests = [guest for guest in common.fetch_live_guest_inventory(use_cache=False) if guest.vmid == vmid]
    existing = next((guest for guest in live_guests if guest.object_type == archive["object_type"] and guest.node == target_node), None)
    if live_guests and not overwrite:
        return f"VMID {vmid} is already in use. Enable overwrite only when replacing the existing {archive['type_label']}."
    if overwrite and existing is None:
        return f"No existing {archive['type_label']} with VMID {vmid} exists on {target_node} to overwrite."
    if existing:
        try:
            existing_config = client.guest_config(node=target_node, object_type=archive["object_type"], vmid=vmid)
            existing_current = client.guest_current(node=target_node, object_type=archive["object_type"], vmid=vmid)
        except ProxmoxAPIError as exc:
            return f"Could not inspect the existing guest before overwrite: {exc}"
        lock = (existing_config or {}).get("lock") or (existing_current or {}).get("lock")
        if lock:
            return f"The existing guest is locked ({lock})."
        if (existing_config or {}).get("protection") in {1, "1", True}:
            return "The existing guest is protected. Disable protection before overwriting it."

    existing_status = str((existing_current or {}).get("status") or "").lower() if existing else ""
    if existing and not existing_status:
        return "Could not confirm the existing guest's current power state. Restore was not queued."

    target_name = getattr(existing, "name", "") or f"Restored {archive['type_label']} {vmid}"
    detail = SimpleNamespace(
        object_type=archive["object_type"], vmid=vmid, node=target_node, name=target_name, config={}, current={}
    )
    audit_details = {
        "archive": archive["volid"],
        "archive_storage": archive["storage"],
        "source_node": archive["node"],
        "target_storage": target_storage,
        "overwrite": overwrite,
        "start_after": start_after,
        "stage": "queued",
        "proxmox_endpoint": getattr(client, "endpoint", ""),
    }
    event = _audit_guest(request, detail, "guest.backup.restore", audit_details, outcome="running")
    task_id = common.async_task(
        "core.tasks.restore_guest_backup_task",
        event.id,
        getattr(client, "endpoint", ""),
        target_node,
        archive["object_type"],
        vmid,
        archive["volid"],
        target_storage,
        overwrite,
        bool(existing and existing_status != "stopped"),
        start_after,
        settings.BACKUP_TASK_TIMEOUT_SECONDS,
    )
    event.details = {**event.details, "worker_task_id": task_id}
    event.save(update_fields=["details"])
    return ""


def _resolve_guest_detail(object_type: str, vmid: int, *, node: str = "") -> SimpleNamespace:
    """Resolve a guest to its current node + live status/config.

    Membership/node come from the live cluster inventory; if the API is
    unreachable, fall back to the latest scan. Never silently pick one guest
    when the same type+VMID is ambiguous across multiple nodes.
    """
    requested_node = node
    matches = [
        g
        for g in common.fetch_live_guest_inventory()
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
        for client in common.configured_clients():
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
    rows, live_available, scan_at = _guest_rows()
    # The sidebar list on every detail/Summary page is the same workspace tree,
    # so it must render the lineage indentation too (not a flat list).
    guest_list = _apply_workspace_lineage(rows)
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
    for client in common.configured_clients():
        try:
            return client.get(
                f"nodes/{quote(detail.node, safe='')}/{kind}/{detail.vmid}/{subpath}",
                timeout=timeout_seconds,
            ), None
        except ProxmoxAPIError as exc:
            err = str(exc)
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


def _guest_pool_label(detail: SimpleNamespace) -> str:
    if not detail.live_ok or not detail.node:
        return ""
    for client in common.configured_clients():
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

    cache_key = f"pve-helper:guest-ha-summary:v1:{detail.node}:{detail.object_type}:{detail.vmid}"
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return cached

    for client in common.configured_clients():
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

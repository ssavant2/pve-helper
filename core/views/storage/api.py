"""Cluster-scoped datastore tab views (`clusters/<key>/datastores/...`)."""

from __future__ import annotations

from core.models import (
    ClusterStorage,
    CurrentGuestInventory,
    ProxmoxStorageConsumer,
)
from core.services.cluster_state_labels import cluster_degraded_context
from core.services.datastore_nav import datastore_url, nav_datastore_key
from core.services.publication_scope import publication_scope
from core.services.request_metadata import client_ip
from core.services.storage_catalog import (
    refresh_storage_catalog,
    storage_view,
    storage_volumes,
)
from core.services.storage_catalog_refresh import (
    StorageCatalogRefreshAlreadyActive,
    StorageCatalogRefreshQueueError,
    queue_storage_catalog_refresh,
)
from core.services.storage_consumers import (
    StorageConsumerReleaseError,
    cluster_storage_consumer_release_preflight,
    release_cluster_storage_consumers,
)
from core.services.storage_paths import (
    storage_mount_root,
)

from .. import common
from ..cluster_scope import managed_cluster_from_path
from ..common import (
    LIVE_GUEST_STATUS_CACHE_SECONDS,
    SPACE_CHART_BUCKET_HOURS,
    SPACE_CHART_DAYS,
    SPACE_CHART_MAX_POINTS,
    AuditEvent,
    FileInventory,
    Http404,
    JsonResponse,
    ProxmoxAPIError,
    ScanRun,
    StorageMount,
    StorageSpaceSnapshot,
    _active_scan,
    _decorate_audit_events,
    _int_or_zero,
    _safe_next_url,
    app_login_required,
    get_permissions,
    json,
    messages,
    navigation_context,
    quote,
    record_audit_event,
    redirect,
    render,
    require_POST,
    settings,
    timedelta,
    tz,
)
from ._shared import (
    STORAGE_CONTENT_ORDER,
    STORAGE_CONTENT_TYPES,
    _classification_counts,
    _decorate_storage_with_space_info,
    _int_request_param,
    _latest_storage_result_scan,
    _ordered_storage_content,
    _recycle_bin_rows,
    _storage_write_disabled_response,
)
from .browser_context import (
    _storage_browser_context,
)

_API_STORAGE_TABS = [
    ("summary", "Summary", "core:api_storage_summary"),
    ("monitor", "Monitor", "core:api_storage_monitor"),
    ("configure", "Configuration", "core:api_storage_configure"),
    ("content", "Content Types", "core:api_storage_content"),
    ("permissions", "Permissions", "core:api_storage_permissions"),
    ("files", "Files", "core:api_storage_files"),
    ("recycle-bin", "Recycle Bin", "core:api_storage_recycle_bin"),
    ("volumes", "Volumes", "core:api_storage_volumes"),
    ("nodes", "Nodes", "core:api_storage_nodes"),
    ("vms", "VMs/CTs", "core:api_storage_vms"),
]

_CLASSIFICATION_CHIP_LABELS = [
    ("referenced", "Referenced"),
    ("likely_orphan", "Likely orphan"),
    ("classification_blocked", "Blocked"),
    ("unknown", "Unknown"),
    ("infrastructure", "Infrastructure"),
    ("proxmox_content", "Proxmox content"),
    ("import_source", "Import source"),
    ("trash", "Trash"),
]


def _resolve_datastore_scope(cluster, storage: str, node: str):
    """Normalize a requested scope, or say where the caller should have gone.

    Returns `(definition, node, moved)`. A shared datastore addressed with a node
    redirects to its cluster-wide URL and a node-local one addressed without a
    node redirects to its only node, so a stale link or a hand-typed URL lands on
    the canonical page instead of a subtly wrong one. `definition` is None when
    the catalog does not carry the storage at all; the page then renders its
    not-found state, which is why this does not raise.
    """
    definition = (
        ClusterStorage.objects.filter(
            cluster=cluster,
            cluster__retired_at__isnull=True,
            unmanaged_at__isnull=True,
            storage_id=storage,
            present=True,
        )
        .select_related("cluster__storage_catalog_state")
        .prefetch_related("node_states", "mount_bindings__mount", "volume_coverages")
        .first()
    )
    if definition is None:
        return None, node, False
    if definition.shared:
        return definition, "", bool(node)
    # Published, not merely present: a node-local datastore on a hidden node has no
    # page, and a typed URL for one is 404 rather than a refusal. From this
    # workspace's point of view the disk is not there — which is what the operator
    # asked for when they hid the node.
    scope = publication_scope(cluster)
    present = sorted(
        state.node for state in definition.node_states.all() if state.present and scope.publishes(state.node)
    )
    if node:
        if node not in present:
            raise Http404("Storage is not present on that node.")
        return definition, node, False
    if len(present) == 1:
        return definition, present[0], True
    # Several instances share the name and nothing in the URL says which disk is
    # meant. Guessing one would show another node's capacity under this node's
    # name, which is the confusion the per-node scope exists to prevent.
    raise Http404("This datastore is node-local; address it through a node.")


def _api_num(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _api_storage_context(cluster, definition, storage: str, node: str, active_tab: str):
    # `view` stays None when the storage is absent from the catalog: the page
    # then renders the not-found state without any catalog-derived context.
    view = None
    scope_nodes: tuple = ()
    if definition is None:
        status, found, error = {}, False, "Storage is not present in the latest catalog."
    else:
        view = storage_view(definition, node=node)
        scope_nodes = view.nodes
        # A shared datastore is one backend behind every node that sees it, so its
        # capacity is read from whichever instance is currently active rather than
        # from a node the URL no longer carries.
        capacity_node = node or next(
            (row.node for row in view.nodes if row.active), view.nodes[0].node if view.nodes else ""
        )
        node_state = next((row for row in view.nodes if row.node == capacity_node), None)
        status = {
            **dict(definition.config or {}),
            "storage": definition.storage_id,
            "type": definition.storage_type,
            "content": ",".join(definition.content),
            "shared": int(definition.shared),
            "enabled": int(not definition.disabled and bool(node_state and node_state.enabled)),
            "active": int(bool(node_state and node_state.active)),
            "total": node_state.total_bytes if node_state else None,
            "used": node_state.used_bytes if node_state else None,
            "avail": node_state.available_bytes if node_state else None,
        }
        found = True
        error = view.coverage_reason if not view.coverage_complete else ""
    total = _api_num(status.get("total"))
    used = _api_num(status.get("used"))
    avail = _api_num(status.get("avail"))
    if used is None and total is not None and avail is not None:
        used = total - avail
    used_pct = round(used / total * 100) if total and used is not None and total > 0 else None
    content_types = [c for c in str(status.get("content") or "").split(",") if c]
    tabs = [
        {
            "key": key,
            "label": label,
            "url": datastore_url(name, cluster.key, storage, node),
            "active": key == active_tab,
        }
        for key, label, name in _API_STORAGE_TABS
    ]
    shared = bool(definition and definition.shared)
    return {
        # Not "dashboard": the datastore page is its own destination now that the
        # sidebar links straight to it, and claiming the dashboard's key lit the
        # Overview leaf as well as the datastore's own.
        #
        # Nine tabs on any number of datastores share this key, so the title says
        # which datastore and which tab — a node-local one carries the node too,
        # because the same storage id on two nodes is two different disks.
        **navigation_context(
            "datastore",
            page_title=(
                f"{storage} on {node}" if node else storage,
                next((label for key, label, _name in _API_STORAGE_TABS if key == active_tab), ""),
            ),
        ),
        **cluster_degraded_context(cluster),
        "node": node,
        "datastore_scope_label": (
            f"Shared datastore in {cluster.display_name}"
            if shared
            else (f"Node-local datastore on {node}" if node else "Datastore")
        ),
        "scope_nodes": scope_nodes,
        # The header offers a file scan whenever pve-helper has a mount to scan:
        # refreshing the catalog and scanning the file tree are the two things an
        # operator starts from a datastore, one per layer.
        "active_scan": _active_scan(),
        "nodes_tab_url": datastore_url("core:api_storage_nodes", cluster.key, storage, node),
        "files_tab_url": datastore_url("core:api_storage_files", cluster.key, storage, node),
        "cluster_key": cluster.key,
        "selected_cluster": cluster,
        "storage": storage,
        "status": status,
        "found": bool(found),
        "error": error,
        "capacity": {"total": total, "used": used, "avail": avail, "used_pct": used_pct},
        "content_types": content_types,
        "storage_type": status.get("type") or "",
        "storage_active": str(status.get("active") or "") in ("1", "True", "true"),
        "storage_enabled": str(status.get("enabled") or "1") in ("1", "True", "true"),
        "api_storage_tabs": tabs,
        "active_api_tab": active_tab,
        "active_api_node": node,
        "active_api_storage": storage,
        "active_nav_datastore": nav_datastore_key(cluster.key, storage, "" if shared else node),
        "catalog_view": view,
        "catalog_view_rendered_at_ms": int(tz.now().timestamp() * 1000),
        "storage_shared": shared,
    }


@require_POST
@app_login_required
def storage_catalog_refresh_view(request, cluster_key: str, storage: str):
    cluster = managed_cluster_from_path(cluster_key)
    if not ClusterStorage.objects.filter(
        cluster=cluster,
        cluster__retired_at__isnull=True,
        unmanaged_at__isnull=True,
        storage_id=storage,
        present=True,
    ).exists():
        raise Http404("Storage is not present in the latest catalog.")
    try:
        event, _task_id = queue_storage_catalog_refresh(cluster=cluster, storage=storage, request=request)
    except StorageCatalogRefreshAlreadyActive:
        # Not an error the operator caused: the refresh they want is already on
        # its way, and Recent Tasks is where it reports.
        return JsonResponse(
            {"ok": True, "status": "already-running", "message": "A catalog refresh is already running."},
            status=200,
        )
    except StorageCatalogRefreshQueueError:
        # Stable domain message; the exception's own text stays in the audit row
        # and the logs rather than crossing the response boundary.
        return JsonResponse({"ok": False, "error": "The catalog refresh could not be queued."}, status=503)
    return JsonResponse({"ok": True, "status": "queued", "task_id": f"catalog:{event.id}"}, status=202)


def _datastore_redirect(request, route_name: str, cluster, storage: str, node: str):
    """Send a non-canonical scope URL to the canonical one, keeping ?vmid."""
    url = datastore_url(route_name, cluster.key, storage, node)
    vmid = request.GET.get("vmid")
    if vmid:
        # The untrusted value is query data appended to a locally reversed URL;
        # it can never control the redirect's scheme, host, or path prefix.
        url = url + "?vmid=" + quote(str(vmid))
    return redirect(url)


def _api_storage_volumes(cluster, definition, node: str, highlight_vmid=None):
    if definition is None:
        return [], False, "Storage is not present in the latest catalog."
    catalog = storage_view(definition, node=node)
    volumes = []
    for entry in storage_volumes(catalog):
        entry_vmid = entry.vmid
        volumes.append(
            {
                "volid": entry.volid,
                "content": entry.content,
                "format": entry.volume_format,
                "size": entry.size_bytes,
                "used": entry.used_bytes,
                "vmid": entry_vmid,
                "importable": entry.content == "import",
                "highlight": highlight_vmid is not None and str(entry_vmid) == str(highlight_vmid),
            }
        )
    volumes.sort(key=lambda item: (str(item["vmid"] or ""), item["volid"]))
    return volumes, catalog.coverage_complete, catalog.coverage_reason


@app_login_required
def storage_api_inventory(request, cluster_key: str, storage: str, node: str = ""):
    """Entry point without a tab; redirects to Summary, keeping the optional ?vmid
    highlight used by the guest Datastores tab."""
    cluster = managed_cluster_from_path(cluster_key)
    _definition, node, _moved = _resolve_datastore_scope(cluster, storage, node)
    return _datastore_redirect(request, "core:api_storage_summary", cluster, storage, node)


def _datastore_metadata(definition) -> list[dict]:
    """The Proxmox definition's headline fields, in a fixed order.

    Read from the catalog rather than from a filesystem scan: the mount page took
    these from scan-derived `StorageMount.details`, which is empty for most
    backends and rendered a row of dashes on exactly the datastores that do have
    a definition to show.
    """
    config = dict((definition.config or {}) if definition is not None else {})

    def value(*names: str) -> str:
        for name in names:
            found = str(config.get(name) or "").strip()
            if found:
                return found
        return ""

    return [
        {"label": "Type", "value": (definition.storage_type if definition else "") or "-"},
        {"label": "Server", "value": value("server", "portal", "monhost") or "-"},
        {"label": "Export", "value": value("export", "share", "volume", "datastore", "pool", "vgname") or "-"},
        {"label": "PVE Path", "value": value("path") or "-"},
        {"label": "Content", "value": ", ".join(definition.content) if definition and definition.content else "-"},
        {"label": "Options", "value": value("options") or "-"},
        {"label": "Preallocation", "value": value("preallocation") or "-"},
        {"label": "Shared", "value": ("Yes" if definition.shared else "No") if definition else "-"},
    ]


def _storage_activity_context(request, mount) -> dict:
    """Recent scans and file actions for one mount, paged.

    Extracted so the datastore page's Monitor tab shows the same activity as the
    registered-mount page rather than only the disk-space chart.
    """
    page_size = 10
    retention_days = 7
    scan_page = max(0, _int_request_param(request, "scan_page", 0))
    event_page = max(0, _int_request_param(request, "event_page", 0))
    cutoff = tz.now() - timedelta(days=retention_days)

    all_scans = ScanRun.objects.filter(target_storage=mount, created_at__gte=cutoff).order_by("-created_at")
    scan_total = all_scans.count()
    scan_start = scan_page * page_size
    scan_end = scan_start + page_size

    all_events = AuditEvent.objects.filter(storage_id=mount.storage_id, timestamp__gte=cutoff).order_by("-timestamp")
    event_total = all_events.count()
    event_start = event_page * page_size
    event_end = event_start + page_size
    recent_events = list(all_events[event_start:event_end])
    _decorate_audit_events(recent_events)

    return {
        "recent_scans": list(all_scans[scan_start:scan_end]),
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
    }


def _classification_chips(counts: dict) -> list[dict]:
    """The scan's classification counts as one row instead of a nine-row table.

    Summary is a page an operator reads at a glance; a table whose rows are almost
    all zero spent most of its height saying nothing. The zero classes stay listed
    — a missing class would read as "not evaluated" rather than "none found" — but
    they no longer each cost a row.
    """
    return [{"key": key, "label": label, "count": counts.get(key, 0)} for key, label in _CLASSIFICATION_CHIP_LABELS]


def _datastore_mount_facts(request, view):
    """Everything the page can only know through pve-helper's own mount.

    Returns the mount, its latest scan and the panels' data, or the single line
    that explains why a datastore has none of it. Keeping the reason beside the
    data is what lets every tab render the same shape for every backend.
    """
    mount = view.mount if view is not None else None
    if mount is None:
        return {
            "mount": None,
            "mount_latest_scan": None,
            "classification_counts": {},
            "total_file_count": 0,
            "gate_status": {},
            "consumers": [],
            "mount_unavailable_reason": _no_mount_reason(view, "Filesystem details"),
        }
    _decorate_storage_with_space_info(mount)
    latest_scan = _latest_storage_result_scan(mount)
    counts = (
        _classification_counts(FileInventory.objects.filter(scan_run=latest_scan, storage=mount)) if latest_scan else {}
    )
    gate_status = {}
    if latest_scan and latest_scan.storage_gate_status:
        gate_status = latest_scan.storage_gate_status.get(mount.storage_id, {})
    return {
        "mount": mount,
        "mount_latest_scan": latest_scan,
        "classification_counts": counts,
        "classification_chips": _classification_chips(counts),
        "total_file_count": sum(counts.values()),
        "gate_status": gate_status,
        "consumers": list(
            mount.consumer_statuses.select_related("cluster").order_by(
                "cluster__display_name",
                "cluster__key",
                "expected_node_name",
            )
        ),
        "mount_unavailable_reason": "",
    }


@app_login_required
def api_storage_summary(request, cluster_key: str, storage: str, node: str = ""):
    cluster = managed_cluster_from_path(cluster_key)
    definition, node, moved = _resolve_datastore_scope(cluster, storage, node)
    if moved:
        return _datastore_redirect(request, "core:api_storage_summary", cluster, storage, node)
    context = _api_storage_context(cluster, definition, storage, node, "summary")
    volumes, _found, _error = _api_storage_volumes(cluster, definition, node)
    vmids = {str(v["vmid"]) for v in volumes if v.get("vmid")}
    mount_facts = _datastore_mount_facts(request, context["catalog_view"])
    consumer_release = cluster_storage_consumer_release_preflight(cluster, actor=request.user)
    context.update(
        {
            "volume_count": len(volumes),
            "guest_count": len(vmids),
            "metadata_cells": _datastore_metadata(definition),
            "consumer_release": consumer_release,
            **mount_facts,
        }
    )
    return render(request, "core/storage_api/summary.html", context)


@require_POST
@app_login_required
def release_cluster_storage_consumers_view(request, cluster_key: str):
    cluster = managed_cluster_from_path(cluster_key)
    redirect_to = _safe_next_url(request)
    if request.POST.get("confirm_release") != "yes":
        messages.error(
            request,
            "Review the exact storage consumer list and confirm that every listed relationship should be released.",
        )
        return redirect(redirect_to)
    try:
        release_cluster_storage_consumers(
            cluster,
            confirmation=request.POST.get("confirmation", ""),
            actor=request.user,
            source_ip=client_ip(request),
        )
    except StorageConsumerReleaseError as exc:
        messages.error(request, str(exc))
    return redirect(redirect_to)


@app_login_required
def api_storage_volumes(request, cluster_key: str, storage: str, node: str = ""):
    cluster = managed_cluster_from_path(cluster_key)
    definition, node, moved = _resolve_datastore_scope(cluster, storage, node)
    if moved:
        return _datastore_redirect(request, "core:api_storage_volumes", cluster, storage, node)
    highlight_vmid = _int_or_zero(request.GET.get("vmid")) or None
    volumes, found, error = _api_storage_volumes(cluster, definition, node, highlight_vmid)
    context = _api_storage_context(cluster, definition, storage, node, "volumes")
    context.update(
        {
            "volumes": volumes,
            "found": found or context["found"],
            "error": error or context["error"],
            "highlight_vmid": highlight_vmid,
        }
    )
    return render(request, "core/storage_api/volumes.html", context)


@app_login_required
def api_storage_vms(request, cluster_key: str, storage: str, node: str = ""):
    cluster = managed_cluster_from_path(cluster_key)
    definition, node, moved = _resolve_datastore_scope(cluster, storage, node)
    if moved:
        return _datastore_redirect(request, "core:api_storage_vms", cluster, storage, node)
    guests = []
    prefix = f"{storage}:"
    lineage = common.stored_guest_lineage(cluster)
    # A shared datastore is consumed from anywhere in the cluster, so its guests
    # are not restricted to one node; a node-local one can only be consumed from
    # the node whose disk it is. Either way the query stays inside this cluster.
    candidates = CurrentGuestInventory.objects.filter(cluster=cluster)
    if node:
        candidates = candidates.filter(node=node)
    for obj in candidates.order_by("object_type", "vmid"):
        matching = [ref for ref in (obj.disk_references or []) if ref.startswith(prefix)]
        if matching:
            obj.matching_disk_references = _display_disk_references(obj.vmid, matching, lineage)
            guests.append(obj)
    context = _api_storage_context(cluster, definition, storage, node, "vms")
    context.update({"guests": guests, "live_status_cache_seconds": LIVE_GUEST_STATUS_CACHE_SECONDS})
    return render(request, "core/storage_api/vms.html", context)


def _api_live_content_values(cluster, storage: str) -> list[str]:
    definition = ClusterStorage.objects.filter(
        cluster=cluster,
        cluster__retired_at__isnull=True,
        unmanaged_at__isnull=True,
        storage_id=storage,
        present=True,
    ).first()
    return list(definition.content) if definition else []


def _api_content_usage(cluster, definition, node: str) -> dict[str, dict]:
    """Count volumes per content type from the API volume list, so we can block
    removing a content type that is still in use (the local analog of the
    filesystem-scan blocker used for mounted storages)."""
    volumes, _found, _error = _api_storage_volumes(cluster, definition, node)
    usage: dict[str, dict] = {}
    for volume in volumes:
        key = str(volume.get("content") or "").strip()
        if not key:
            continue
        bucket = usage.setdefault(key, {"count": 0, "examples": []})
        bucket["count"] += 1
        if len(bucket["examples"]) < 3 and volume.get("volid"):
            bucket["examples"].append(volume["volid"])
    return usage


def _api_content_options(current: list[str], usage: dict[str, dict]) -> list[dict]:
    definitions = list(STORAGE_CONTENT_TYPES)
    for key in sorted(set(current) - set(STORAGE_CONTENT_ORDER)):
        definitions.append(
            {
                "key": key,
                "label": key,
                "description": "Unknown content type preserved from the current Proxmox storage configuration.",
            }
        )
    return [
        {
            **definition,
            "selected": definition["key"] in current,
            "usage_count": usage.get(definition["key"], {}).get("count", 0),
            "usage_examples": usage.get(definition["key"], {}).get("examples", [])[:3],
        }
        for definition in definitions
    ]


def _api_content_blockers(usage: dict[str, dict], removed: list[str]) -> list[dict]:
    labels = {item["key"]: item["label"] for item in STORAGE_CONTENT_TYPES}
    return [
        {
            "key": key,
            "label": labels.get(key, key),
            "count": usage.get(key, {}).get("count", 0),
            "examples": usage.get(key, {}).get("examples", []),
        }
        for key in removed
        if usage.get(key, {}).get("count", 0) > 0
    ]


@app_login_required
def api_storage_content(request, cluster_key: str, storage: str, node: str = ""):
    cluster = managed_cluster_from_path(cluster_key)
    definition, node, moved = _resolve_datastore_scope(cluster, storage, node)
    if moved:
        return _datastore_redirect(request, "core:api_storage_content", cluster, storage, node)
    context = _api_storage_context(cluster, definition, storage, node, "content")
    current = _api_live_content_values(cluster, storage)
    usage = _api_content_usage(cluster, definition, node)
    context.update(
        {
            "content_options": _api_content_options(current, usage),
            "current_content": current,
            "storage_write_enabled": settings.STORAGE_WRITE_ENABLED,
        }
    )
    return render(request, "core/storage_api/content.html", context)


@require_POST
@app_login_required
def update_api_storage_content(request, cluster_key: str, storage: str, node: str = ""):
    cluster = managed_cluster_from_path(cluster_key)
    definition, node, _moved = _resolve_datastore_scope(cluster, storage, node)
    redirect_to = datastore_url("core:api_storage_content", cluster.key, storage, node)
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    current = _api_live_content_values(cluster, storage)
    requested = _ordered_storage_content(request.POST.getlist("content"), current)
    if not requested:
        messages.error(request, "Select at least one content type.")
        return redirect(redirect_to)

    usage = _api_content_usage(cluster, definition, node)
    removed = [key for key in current if key not in requested]
    blockers = _api_content_blockers(usage, removed)
    if blockers:
        for blocker in blockers:
            examples = ", ".join(blocker["examples"][:3])
            suffix = f" Examples: {examples}." if examples else ""
            messages.error(
                request,
                f"Cannot disable {blocker['label']} because {blocker['count']} volume"
                f"{'' if blocker['count'] == 1 else 's'} on this storage use it.{suffix}",
            )
        return redirect(redirect_to)

    updated = False
    err = ""
    for client in common.cluster_scoped_clients(cluster):
        try:
            client.set_storage_content(storage, requested)
            updated = True
            err = ""
            break
        except ProxmoxAPIError as exc:
            err = str(exc)
    if not updated:
        messages.error(request, f"Failed to update storage content: {err or 'No configured Proxmox endpoints.'}")
        return redirect(redirect_to)

    refresh_storage_catalog(cluster)

    record_audit_event(
        request,
        action="storage.content.updated",
        object_type="storage",
        object_id=storage,
        cluster=cluster,
        details={"storage_id": storage, "node": node, "old_content": current, "new_content": requested},
    )
    return redirect(redirect_to)


@app_login_required
def api_storage_monitor(request, cluster_key: str, storage: str, node: str = ""):
    cluster = managed_cluster_from_path(cluster_key)
    definition, node, moved = _resolve_datastore_scope(cluster, storage, node)
    if moved:
        return _datastore_redirect(request, "core:api_storage_monitor", cluster, storage, node)
    context = _api_storage_context(cluster, definition, storage, node, "monitor")
    chart_data = _api_storage_space_chart_data(cluster, node, storage, tz.now())
    facts = _datastore_mount_facts(request, context["catalog_view"])
    context.update(facts)
    if facts["mount"] is not None:
        # Capacity history is sampled twice over: through the Proxmox API keyed on
        # (cluster, node, storage), and through the mount's own filesystem. A
        # mounted datastore only ever gets the second, so preferring it is what
        # keeps the chart from being empty on exactly the datastores pve-helper
        # knows best.
        chart_data = _storage_space_chart_data(facts["mount"], tz.now()) or chart_data
        context.update(_storage_activity_context(request, facts["mount"]))
    context["space_chart_data_json"] = json.dumps(chart_data)
    return render(request, "core/storage_api/monitor.html", context)


@app_login_required
def api_storage_configure(request, cluster_key: str, storage: str, node: str = ""):
    cluster = managed_cluster_from_path(cluster_key)
    definition, node, moved = _resolve_datastore_scope(cluster, storage, node)
    if moved:
        return _datastore_redirect(request, "core:api_storage_configure", cluster, storage, node)
    context = _api_storage_context(cluster, definition, storage, node, "configure")
    # From the catalog, not from a filesystem scan: `ProxmoxInventory` only holds
    # storage rows for scans that ran against a registered mount, so this panel was
    # empty on every datastore pve-helper does not mount — and on several that it
    # does. `ClusterStorage.config` is the definition Proxmox returned.
    config = dict(definition.config or {}) if definition is not None else {}
    # Present the interesting config keys in a stable order; skip the nested
    # node_status blob and empty values.
    # Keys the named rows above already show, plus the noise Proxmox mixes in.
    skip = {
        "node_status",
        "storage",
        "total",
        "used",
        "avail",
        "used_fraction",
        "type",
        "server",
        "portal",
        "monhost",
        "export",
        "share",
        "volume",
        "datastore",
        "pool",
        "vgname",
        "path",
        "content",
        "options",
        "preallocation",
        "shared",
        "digest",
    }
    config_rows = [
        {"key": key, "value": config[key]}
        for key in sorted(config)
        if key not in skip and config[key] not in ("", None, [])
    ]
    context.update(
        {
            "storage_config": config,
            "config_rows": config_rows,
            "metadata_cells": _datastore_metadata(definition),
            **_datastore_mount_facts(request, context["catalog_view"]),
        }
    )
    return render(request, "core/storage_api/configure.html", context)


@app_login_required
def api_storage_nodes(request, cluster_key: str, storage: str, node: str = ""):
    """Which nodes this datastore is attached to, and what each one reports.

    Two independent bodies of evidence live here on purpose. The catalog's node
    states are what Proxmox says; the shared gate is what pve-helper's own scan
    saw. Neither implies the other, and a destructive file action must pass both,
    so showing them in one table with separate columns is the honest rendering.
    """
    cluster = managed_cluster_from_path(cluster_key)
    definition, node, moved = _resolve_datastore_scope(cluster, storage, node)
    if moved:
        return _datastore_redirect(request, "core:api_storage_nodes", cluster, storage, node)
    context = _api_storage_context(cluster, definition, storage, node, "nodes")
    view = context["catalog_view"]

    gate_by_node: dict[str, ProxmoxStorageConsumer] = {}
    gate_note = ""
    if view is None:
        gate_note = "This datastore is not in the latest catalog, so no node state is available."
    elif view.mount is None:
        gate_note = (
            "No host mount is registered for this datastore, so pve-helper's shared-mount gate "
            "does not apply. The node states below come from the Proxmox API alone."
        )
    else:
        gate_by_node = {
            consumer.expected_node_name: consumer for consumer in view.mount.consumer_statuses.filter(cluster=cluster)
        }

    rows = [
        {
            "node": state.node,
            "active": state.active,
            "enabled": state.enabled,
            "present": state.present,
            "unreachable": state.unreachable,
            "total": state.total_bytes,
            "used": state.used_bytes,
            "avail": state.available_bytes,
            "gate": gate_by_node.get(state.node),
            "is_current": bool(node) and state.node == node,
            "url": datastore_url("core:api_storage_nodes", cluster.key, storage, state.node),
        }
        for state in (view.nodes if view is not None else ())
    ]
    # A node-local datastore's siblings carry the same name on other nodes and are
    # different disks. Naming them here is the only place the UI can say so, since
    # Proxmox gives the operator no way to tell them apart by name.
    siblings = [row for row in rows if not row["is_current"]] if node else []
    if node:
        # This datastore lives on exactly one node. Listing the others in the table
        # would read as if it were shared between them; they belong in the footnote
        # below, which says plainly that they are separate disks sharing a name.
        rows = [row for row in rows if row["is_current"]]
    context.update({"node_rows": rows, "gate_note": gate_note, "sibling_rows": siblings})
    return render(request, "core/storage_api/nodes.html", context)


@app_login_required
def api_storage_permissions(request, cluster_key: str, storage: str, node: str = ""):
    cluster = managed_cluster_from_path(cluster_key)
    definition, node, moved = _resolve_datastore_scope(cluster, storage, node)
    if moved:
        return _datastore_redirect(request, "core:api_storage_permissions", cluster, storage, node)
    context = _api_storage_context(cluster, definition, storage, node, "permissions")
    view = context["catalog_view"]
    mount = view.mount if view is not None else None
    context.update(
        {
            "mount": mount,
            "permissions": get_permissions(str(storage_mount_root(mount))) if mount else None,
            "unavailable_reason": ("" if mount else _no_mount_reason(view, "Permissions")),
        }
    )
    return render(request, "core/storage_api/permissions.html", context)


def _no_mount_reason(view, subject: str) -> str:
    """Why a filesystem-backed tab has nothing to show, in one operator sentence."""
    if view is None:
        return f"{subject} are unavailable: this datastore is not in the latest catalog."
    reason = view.capabilities.browse_files_reason
    return f"{subject} are unavailable. {reason}" if reason else f"{subject} are unavailable."


@app_login_required
def api_storage_files(request, cluster_key: str, storage: str, node: str = ""):
    cluster = managed_cluster_from_path(cluster_key)
    definition, node, moved = _resolve_datastore_scope(cluster, storage, node)
    if moved:
        return _datastore_redirect(request, "core:api_storage_files", cluster, storage, node)
    context = _api_storage_context(cluster, definition, storage, node, "files")
    view = context["catalog_view"]
    mount = view.mount if view is not None else None
    if mount is None:
        context.update({"mount": None, "unavailable_reason": _no_mount_reason(view, "Files")})
        return render(request, "core/storage_api/files.html", context)
    result = _storage_browser_context(request, mount)
    if isinstance(result, JsonResponse):
        return result
    context.update({**result, "unavailable_reason": ""})
    return render(request, "core/storage_api/files.html", context)


@app_login_required
def api_storage_recycle_bin(request, cluster_key: str, storage: str, node: str = ""):
    cluster = managed_cluster_from_path(cluster_key)
    definition, node, moved = _resolve_datastore_scope(cluster, storage, node)
    if moved:
        return _datastore_redirect(request, "core:api_storage_recycle_bin", cluster, storage, node)
    context = _api_storage_context(cluster, definition, storage, node, "recycle-bin")
    view = context["catalog_view"]
    mount = view.mount if view is not None else None
    if mount is None:
        context.update(
            {
                "trash_mount": None,
                "items": [],
                "unavailable_reason": _no_mount_reason(view, "Recycle Bin contents"),
            }
        )
    else:
        context.update(
            {
                "trash_mount": mount,
                "items": _recycle_bin_rows(mount),
                "unavailable_reason": "",
            }
        )
    return render(request, "core/storage_api/recycle_bin.html", context)


def _storage_space_chart_data(storage: StorageMount, now) -> list[dict[str, object]]:
    return _space_chart_from_queryset(StorageSpaceSnapshot.objects.filter(storage=storage), now)


def _api_storage_space_chart_data(cluster, node: str, storage_id: str, now) -> list[dict[str, object]]:
    # Snapshots are always recorded per node, because that is how the Proxmox API
    # reports capacity. A shared datastore has no node in its scope, so pinning the
    # query to one would have left its chart permanently empty; every node sees the
    # same backend, so the samples are read across the cluster and bucketed.
    samples = StorageSpaceSnapshot.objects.filter(
        storage__isnull=True,
        cluster=cluster,
        api_storage_id=storage_id,
    )
    if node:
        samples = samples.filter(node=node)
    return _space_chart_from_queryset(samples, now)


def _space_chart_from_queryset(base_qs, now) -> list[dict[str, object]]:
    cutoff = now - timedelta(days=SPACE_CHART_DAYS)
    scheduled_history = list(
        base_qs.filter(
            scan_run__isnull=True,
            recorded_at__gte=cutoff,
        ).order_by("recorded_at")
    )
    history = scheduled_history or list(
        base_qs.filter(
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


def _display_disk_references(vmid: int | None, matching: list[str], lineage: dict[int, int]) -> list[dict]:
    """Clean up a linked clone's disk references for display: show its own overlay
    disks annotated '(backed by base-<templateid>)' and drop the template's base
    volumes (those show on the template's own row). Non-clone guests unchanged."""
    parent = lineage.get(vmid) if vmid is not None else None
    if parent is None:
        return [{"volid": ref, "backed_by": ""} for ref in matching]
    base_marker = f"base-{parent}-disk-"
    return [{"volid": ref, "backed_by": f"base-{parent}"} for ref in matching if base_marker not in ref]

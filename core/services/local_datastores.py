"""Per-node local (non-shared) Proxmox storages for the sidebar tree.

Local storages (local, local-lvm, local-zfs, ...) live on the hypervisor and
cannot be mounted by pve-helper, so they are read-only via the Proxmox API. The
data (type + capacity) is already captured by scans as ProxmoxInventory storage
rows, so the nav tree is built cheaply from the latest scan rather than a live
API call on every page render.
"""
from __future__ import annotations

from django.core.cache import cache

from ..models import ProxmoxInventory, ScanRun
from .cluster_state_identity import cluster_cache_key
from .current_guest_inventory import current_inventory_cluster

_CACHE_NAMESPACE = "nav-local-datastores:v2"
_CACHE_SECONDS = 60

_TRUTHY = {"1", "true", "yes", "on"}


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _is_truthy(value) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def local_datastore_nav(*, use_cache: bool = True, cluster=None):
    """Return [{node, storages: [{storage_id, type, total, used, avail,
    used_pct, active}]}] for local (non-shared) storages from the latest
    completed scan, grouped and sorted by node. Empty list if no scan yet."""
    cluster = current_inventory_cluster(cluster)
    if cluster is None:
        return []
    cache_key = cluster_cache_key(_CACHE_NAMESPACE, cluster)
    if use_cache:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    result = _build(cluster)
    if use_cache:
        cache.set(cache_key, result, _CACHE_SECONDS)
    return result


def _build(cluster):
    scan = (
        ScanRun.objects.filter(
            status=ScanRun.Status.COMPLETED,
            proxmox_objects__cluster=cluster,
        )
        .order_by("-finished_at", "-created_at")
        .distinct()
        .first()
    )
    if scan is None:
        return []

    rows = ProxmoxInventory.objects.filter(
        scan_run=scan,
        cluster=cluster,
        object_type=ProxmoxInventory.ObjectType.STORAGE,
    ).order_by("node", "name")

    nodes: dict[str, list] = {}
    for row in rows:
        config = row.config if isinstance(row.config, dict) else {}
        if _is_truthy(config.get("shared")):
            continue  # shared storages live under "Shared Datastores"
        total = _to_int(config.get("total"))
        used = _to_int(config.get("used"))
        used_pct = round(used / total * 100) if total and used is not None and total > 0 else None
        nodes.setdefault(row.node, []).append(
            {
                "storage_id": row.name,
                "type": str(config.get("type") or ""),
                "total": total,
                "used": used,
                "avail": _to_int(config.get("avail")),
                "used_pct": used_pct,
                "active": _is_truthy(config.get("active")),
            }
        )

    return [{"node": node, "storages": storages} for node, storages in sorted(nodes.items())]

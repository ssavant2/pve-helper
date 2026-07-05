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

_CACHE_KEY = "pve-helper:nav-local-datastores:v1"
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


def local_datastore_nav(*, use_cache: bool = True):
    """Return [{node, storages: [{storage_id, type, total, used, avail,
    used_pct, active}]}] for local (non-shared) storages from the latest
    completed scan, grouped and sorted by node. Empty list if no scan yet."""
    if use_cache:
        cached = cache.get(_CACHE_KEY)
        if cached is not None:
            return cached

    result = _build()
    if use_cache:
        cache.set(_CACHE_KEY, result, _CACHE_SECONDS)
    return result


def _build():
    scan = (
        ScanRun.objects.filter(status=ScanRun.Status.COMPLETED)
        .order_by("-finished_at", "-created_at")
        .first()
    )
    if scan is None:
        return []

    rows = ProxmoxInventory.objects.filter(
        scan_run=scan, object_type=ProxmoxInventory.ObjectType.STORAGE
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

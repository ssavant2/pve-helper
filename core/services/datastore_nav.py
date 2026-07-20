"""Per-cluster datastore navigation from the current storage catalog projection."""

from __future__ import annotations

from django.core.cache import cache

from core.models import ClusterStorageNodeState
from core.services.cluster_state_identity import cluster_cache_key

_CACHE_NAMESPACE = "nav-datastores:v4"
_CACHE_SECONDS = 60


def datastore_nav(*, cluster, use_cache: bool = True):
    """The cluster's published datastores, split into shared and per-node groups.

    Returns ``{"shared": [...], "nodes": [{"node": ..., "storages": [...]}, ...]}``.
    The sidebar's axis is the catalog, not local-vs-shared: a shared storage is a
    cluster-wide object, a node-local one belongs to exactly one node, and a
    registered host mount is a different object entirely that this function never
    reports.
    """
    if cluster is None:
        return {"shared": [], "nodes": []}
    cache_key = cluster_cache_key(_CACHE_NAMESPACE, cluster)
    if use_cache:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    result = _build(cluster)
    if use_cache:
        cache.set(cache_key, result, _CACHE_SECONDS)
    return result


def nav_datastore_key(cluster_key: str, storage_id: str, node: str = "") -> str:
    """The identity a sidebar datastore leaf is highlighted by.

    The cluster is part of it because two clusters routinely publish the same
    `pve1`/`local` pair, and a bare node+storage comparison highlights both. The
    node is empty for a shared storage, which is one cluster-wide object however
    many nodes see it — otherwise arriving via a different node than the sidebar
    linked to would silently highlight nothing.
    """
    return f"{cluster_key}|{node}|{storage_id}"


def _entry(row, *, cluster_key: str, link_node: str, shared: bool) -> dict:
    total = row.total_bytes
    used = row.used_bytes
    return {
        "storage_id": row.cluster_storage.storage_id,
        "type": row.cluster_storage.storage_type,
        "total": total,
        "used": used,
        "avail": row.available_bytes,
        "used_pct": round(used / total * 100) if total and used is not None and total > 0 else None,
        "active": row.active,
        # The storage detail URL is node-qualified even for a shared storage, whose
        # volumes are published once under the shared observation node. The node
        # therefore only selects whose capacity numbers are shown, and it is chosen
        # by the same rule the catalog table uses so both surfaces agree.
        "link_node": link_node,
        "nav_key": nav_datastore_key(cluster_key, row.cluster_storage.storage_id, "" if shared else row.node),
    }


def _build(cluster):
    rows = (
        ClusterStorageNodeState.objects.select_related("cluster_storage")
        .filter(cluster_storage__cluster=cluster, cluster_storage__present=True, present=True)
        .order_by("node", "cluster_storage__storage_id")
    )
    nodes: dict[str, list[dict]] = {}
    shared_rows: dict[int, list] = {}
    for row in rows:
        if row.cluster_storage.shared:
            shared_rows.setdefault(row.cluster_storage_id, []).append(row)
        else:
            nodes.setdefault(row.node, []).append(
                _entry(row, cluster_key=cluster.key, link_node=row.node, shared=False)
            )
    shared = []
    for candidates in shared_rows.values():
        # First active instance, else the first present one — the rule in
        # `_storage_catalog_rows`. `rows` is already node-ordered, so this is stable.
        chosen = next((row for row in candidates if row.active), candidates[0])
        shared.append(_entry(chosen, cluster_key=cluster.key, link_node=chosen.node, shared=True))
    shared.sort(key=lambda entry: entry["storage_id"])
    return {
        "shared": shared,
        "nodes": [{"node": node, "storages": storages} for node, storages in sorted(nodes.items())],
    }

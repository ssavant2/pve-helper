"""Per-node non-shared datastores from the current storage catalog projection."""

from __future__ import annotations

from django.core.cache import cache

from core.models import ClusterStorageNodeState
from core.services.cluster_state_identity import cluster_cache_key

_CACHE_NAMESPACE = "nav-local-datastores:v3"
_CACHE_SECONDS = 60


def local_datastore_nav(*, cluster, use_cache: bool = True):
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
    rows = (
        ClusterStorageNodeState.objects.select_related("cluster_storage")
        .filter(
            cluster_storage__cluster=cluster,
            cluster_storage__present=True,
            cluster_storage__shared=False,
            present=True,
        )
        .order_by("node", "cluster_storage__storage_id")
    )
    nodes: dict[str, list[dict]] = {}
    for row in rows:
        total = row.total_bytes
        used = row.used_bytes
        used_pct = round(used / total * 100) if total and used is not None and total > 0 else None
        nodes.setdefault(row.node, []).append(
            {
                "storage_id": row.cluster_storage.storage_id,
                "type": row.cluster_storage.storage_type,
                "total": total,
                "used": used,
                "avail": row.available_bytes,
                "used_pct": used_pct,
                "active": row.active,
            }
        )
    return [{"node": node, "storages": storages} for node, storages in sorted(nodes.items())]

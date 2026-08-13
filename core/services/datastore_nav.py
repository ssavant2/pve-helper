"""Per-cluster datastore navigation from the current storage catalog projection."""

from __future__ import annotations

from django.core.cache import cache
from django.db import models
from django.urls import reverse

from core.models import ClusterStorageNodeState
from core.services.cluster_state_identity import cluster_cache_key
from core.services.publication_scope import publication_scope

# Bumped for the enrollment filter below: a cached tree built before it would keep
# showing a hidden node's datastores for the rest of its lifetime, which is exactly
# the leak the filter closes.
_CACHE_NAMESPACE = "nav-datastores:v6"
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


def datastore_url(route_name: str, cluster_key: str, storage: str, node: str = "") -> str:
    """Reverse a datastore route in the shape its scope requires.

    The two URL shapes share one route name and are told apart by whether `node`
    is present, so every caller reverses through here instead of deciding which
    pattern to name. It lives beside `nav_datastore_key` because both answer the
    same question: what identifies this datastore.
    """
    kwargs = {"cluster_key": cluster_key, "storage": storage}
    if node:
        kwargs["node"] = node
    return reverse(route_name, kwargs=kwargs)


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
        "unreachable": row.unreachable,
        # Empty for a shared datastore, which has one cluster-wide page. For a
        # node-local one the node is part of the page's identity: `local` is a
        # different disk on every node, so the three leaves must not lead to one
        # page. The capacity above still comes from a specific instance.
        "link_node": link_node,
        "nav_key": nav_datastore_key(cluster_key, row.cluster_storage.storage_id, "" if shared else row.node),
    }


def _build(cluster):
    rows = (
        ClusterStorageNodeState.objects.select_related("cluster_storage")
        # Unreachable instances stay in the tree. A node taken down for patching
        # has not had its disks removed, and a datastore that silently disappears
        # from navigation is indistinguishable from one that was deleted.
        .filter(
            cluster_storage__cluster=cluster,
            cluster_storage__cluster__retired_at__isnull=True,
            cluster_storage__unmanaged_at__isnull=True,
            cluster_storage__present=True,
        )
        .filter(models.Q(present=True) | models.Q(unreachable=True))
        .order_by("node", "cluster_storage__storage_id")
    )
    # One query for the whole tree, and the filter applies to shared instances too.
    # A shared datastore is one cluster-wide object, but the capacity on its leaf is
    # read from one specific instance: sourcing that from a hidden node would put
    # that node's numbers on screen under a name that does not mention it. If no
    # published node sees the datastore, no published node has it mounted, and it
    # does not belong in this cluster's tree.
    scope = publication_scope(cluster)
    nodes: dict[str, list[dict]] = {}
    shared_rows: dict[int, list] = {}
    for row in rows:
        if not scope.publishes(row.node):
            continue
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
        chosen = next(
            (row for row in candidates if row.active),
            next((row for row in candidates if row.present), candidates[0]),
        )
        shared.append(_entry(chosen, cluster_key=cluster.key, link_node="", shared=True))
    shared.sort(key=lambda entry: entry["storage_id"])
    return {
        "shared": shared,
        "nodes": [{"node": node, "storages": storages} for node, storages in sorted(nodes.items())],
    }

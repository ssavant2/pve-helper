"""Namespacing for cluster-derived ephemeral state.

Database identity is not enough for caches and PostgreSQL advisory locks: both
outlive the local call that selected a cluster, and a bare VMID/node or one global
lock therefore lets independent clusters affect each other.  Keep the convention
in one place so new callers cannot invent subtly different key formats.
"""

from __future__ import annotations

import hashlib
from urllib.parse import quote

from django.db.models import F


def cluster_cache_key(namespace: str, cluster, *identity: object) -> str:
    """Return one cache key bound to a cluster and its invalidation generation."""
    namespace = str(namespace or "").strip().strip(":")
    if not namespace:
        raise ValueError("A cache namespace is required.")
    if cluster is None or not getattr(cluster, "key", "") or not getattr(cluster, "pk", None):
        raise ValueError("A persisted cluster is required for cluster-derived cache state.")

    parts = [
        "pve-helper",
        namespace,
        "cluster",
        str(cluster.key),
        str(cluster.pk),
        f"g{int(getattr(cluster, 'cache_generation', 1) or 1)}",
    ]
    parts.extend(quote(str(item), safe="-_.") for item in identity)
    return ":".join(parts)


def invalidate_cluster_cache(cluster) -> None:
    """Make every cache key from this cluster unreachable across all processes.

    Django's default cache is process-local, so deleting known keys in the writer
    would leave sibling web/worker processes stale.  A generation stored on the
    cluster row gives every later resolver the new namespace instead.
    """
    if cluster is None or not getattr(cluster, "pk", None):
        return
    type(cluster).objects.filter(pk=cluster.pk).update(cache_generation=F("cache_generation") + 1)
    # Refresh after the atomic increment: concurrent invalidators may have advanced
    # farther than this caller's stale in-memory value.
    cluster.refresh_from_db(fields=["cache_generation"])


def cluster_advisory_lock_id(base_lock_id: int, cluster) -> int:
    """Derive a stable positive PostgreSQL bigint lock id for one cluster."""
    if cluster is None or not getattr(cluster, "key", ""):
        raise ValueError("A cluster is required for a cluster-scoped advisory lock.")
    material = f"{int(base_lock_id)}:{cluster.key}".encode()
    value = int.from_bytes(hashlib.blake2b(material, digest_size=8).digest(), "big")
    return value & ((1 << 63) - 1)

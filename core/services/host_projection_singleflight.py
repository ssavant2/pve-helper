"""One cluster-wide single-flight shared by periodic and manual host refreshes."""

from __future__ import annotations

from contextlib import contextmanager

from django.db import connection

from core.services.cluster_state_identity import cluster_advisory_lock_id

HOST_PROJECTION_REFRESH_LOCK_ID = 0x50564548505201

#: 5a4B-i's lane, deliberately a **different** lock. Sharing the host-projection
#: lock would mean a slow node-network pass makes the next membership/node-runtime
#: cycle return "refresh already running" and skip entirely -- one domain blanking
#: two others at pass grain, which is the failure this projection's per-node
#: coverage exists to prevent.
NODE_NETWORK_REFRESH_LOCK_ID = 0x50564548505202


@contextmanager
def _cluster_try_lock(namespace: int, cluster):
    if connection.vendor != "postgresql":
        yield True
        return

    lock_id = cluster_advisory_lock_id(namespace, cluster)
    acquired = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock_id])
            acquired = bool(cursor.fetchone()[0])
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])


@contextmanager
def host_projection_refresh_lock(cluster):
    """Try the host-projection lock without waiting and always release it."""
    with _cluster_try_lock(HOST_PROJECTION_REFRESH_LOCK_ID, cluster) as acquired:
        yield acquired


@contextmanager
def node_network_refresh_lock(cluster):
    """Try the node-network lane's own lock. See the lock-id note above."""
    with _cluster_try_lock(NODE_NETWORK_REFRESH_LOCK_ID, cluster) as acquired:
        yield acquired

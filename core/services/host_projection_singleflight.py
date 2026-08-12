"""One cluster-wide single-flight shared by periodic and manual host refreshes."""

from __future__ import annotations

from contextlib import contextmanager

from django.db import connection

from core.services.cluster_state_identity import cluster_advisory_lock_id

HOST_PROJECTION_REFRESH_LOCK_ID = 0x50564548505201


@contextmanager
def host_projection_refresh_lock(cluster):
    """Try the host-projection lock without waiting and always release it."""
    if connection.vendor != "postgresql":
        yield True
        return

    lock_id = cluster_advisory_lock_id(HOST_PROJECTION_REFRESH_LOCK_ID, cluster)
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

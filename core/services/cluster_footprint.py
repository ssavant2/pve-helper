"""Operational-footprint stamping — the durable memory that makes hard-delete
eligibility impossible to recover by waiting for timed retention to run.

``ProxmoxCluster.operational_footprint_at`` is stamped the first time a cluster
acquires any non-configuration footprint: a provider-operation Audit event, a
scan observation, a current-guest or storage projection row, or a console
session. It is monotonic and never cleared — not by a retention purge, cleanup
or retirement — so :func:`unused_connection_deletion_eligibility` can trust a
non-null value as proof the connection was once operational even after every
timed-retention sweep has emptied the relation that first stamped it.

Every stamp is an idempotent, set-based ``UPDATE ... WHERE
operational_footprint_at IS NULL``: the first writer wins the timestamp and
reason, later writers no-op, and concurrent writers cannot race because the
filter is evaluated by the database, not in Python.
"""

from __future__ import annotations

from django.utils import timezone

from core.models import ProxmoxCluster

# Stable reason codes (each well within ProxmoxCluster.operational_footprint_reason,
# max_length=64). They explain *why* a connection is remembered as operational;
# they are never used for a decision, only for accountable display and Audit.
FOOTPRINT_PROVIDER_OPERATION = "provider_operation"
FOOTPRINT_SCAN_OBSERVATION = "scan_observation"
FOOTPRINT_GUEST_PROJECTION = "guest_projection"
FOOTPRINT_STORAGE_PROJECTION = "storage_projection"
FOOTPRINT_CONSOLE_SESSION = "console_session"


def stamp_operational_footprint(cluster, *, reason: str) -> bool:
    """Record that ``cluster`` has acquired operational footprint, once.

    Accepts a :class:`~core.models.ProxmoxCluster` or its primary key. Returns
    ``True`` only for the stamp that actually set the marker, ``False`` when it
    was already set (or the cluster id is missing). Safe to call from inside the
    writer's own transaction: the filtered UPDATE rolls back with the row that
    triggered it and no-ops for every later footprint.
    """
    if isinstance(cluster, ProxmoxCluster):
        # Fast path: a freshly loaded row that already carries the marker needs
        # no query. The UPDATE below stays authoritative if the in-memory value
        # is stale, because the marker only ever goes NULL -> set.
        if cluster.operational_footprint_at is not None:
            return False
        cluster_id = cluster.pk
    else:
        cluster_id = cluster
    if cluster_id is None:
        return False
    updated = ProxmoxCluster.objects.filter(pk=cluster_id, operational_footprint_at__isnull=True).update(
        operational_footprint_at=timezone.now(),
        operational_footprint_reason=reason,
    )
    return bool(updated)

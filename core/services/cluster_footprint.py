"""Operational-footprint stamping — the durable memory that makes hard-delete
eligibility impossible to recover by waiting for timed retention to run.

``ProxmoxCluster.operational_footprint_at`` is stamped the first time a cluster
acquires any non-configuration footprint: a provider-operation Audit event, a
scan observation, a current-guest or storage projection row, or a console
session. It is monotonic and never cleared — not by a retention purge, cleanup
or retirement — so :func:`unused_connection_deletion_eligibility` can trust a
non-null value as proof the connection was once observed after every timed
retention sweep has emptied the relation that first stamped it.

Every stamp is an idempotent, set-based ``UPDATE ... WHERE
operational_footprint_at IS NULL``: the first writer wins the timestamp, later
writers no-op, and concurrent writers cannot race because the filter is
evaluated by the database, not in Python.

**The reason is a decision input, and it escalates.** The marker alone cannot
distinguish "a background refresh touched this connection sixty seconds after it
was added" from "an operator ran something against it" — and treating those the
same is what made ``Delete unused connection`` unreachable in practice, because
the periodic guest/storage refreshes stamp every new connection within a minute.
:data:`OPERATOR_FOOTPRINT_REASONS` is therefore what eligibility blocks on, and
because the timestamp is first-writer-wins, a later operator-grade stamp
*upgrades* the stored reason in place. Without that upgrade a console session on
day one would hide behind a ``guest_projection`` reason stamped a minute earlier
and vanish from the record entirely once console retention purged the row.
"""

from __future__ import annotations

from django.utils import timezone

from core.models import ProxmoxCluster

# Stable reason codes (each well within ProxmoxCluster.operational_footprint_reason,
# max_length=64). They explain *why* a connection is remembered as operational.
FOOTPRINT_PROVIDER_OPERATION = "provider_operation"
FOOTPRINT_SCAN_OBSERVATION = "scan_observation"
FOOTPRINT_GUEST_PROJECTION = "guest_projection"
FOOTPRINT_STORAGE_PROJECTION = "storage_projection"
FOOTPRINT_HOST_PROJECTION = "host_projection"
FOOTPRINT_CONSOLE_SESSION = "console_session"
# The inventory the app collects by itself the moment a connection is added. It
# is provider work, but nobody asked for it as an operation: it is the add
# finishing its own job, and everything it writes is a projection.
FOOTPRINT_INVENTORY_BOOTSTRAP = "inventory_bootstrap"

# Footprint an operator caused. Durable and irreversible for eligibility: it is
# the record that somebody used this connection, and nothing reconstructs it.
OPERATOR_FOOTPRINT_REASONS = frozenset(
    {
        FOOTPRINT_PROVIDER_OPERATION,
        FOOTPRINT_CONSOLE_SESSION,
    }
)

# Footprint a background job caused. Every row behind these reasons is a
# projection or scan snapshot the next refresh rebuilds from Proxmox, so it is
# not evidence that the connection was ever *used*. Membership here is an
# allowlist and the eligibility check fails closed on anything absent from it:
# a new reason code blocks deletion until it is deliberately classified.
RECONSTRUCTIBLE_FOOTPRINT_REASONS = frozenset(
    {
        FOOTPRINT_SCAN_OBSERVATION,
        FOOTPRINT_GUEST_PROJECTION,
        FOOTPRINT_STORAGE_PROJECTION,
        FOOTPRINT_HOST_PROJECTION,
        FOOTPRINT_INVENTORY_BOOTSTRAP,
    }
)


def stamp_operational_footprint(cluster, *, reason: str) -> bool:
    """Record that ``cluster`` has acquired operational footprint.

    Accepts a :class:`~core.models.ProxmoxCluster` or its primary key. Returns
    ``True`` only for the stamp that actually set the marker, ``False`` when it
    was already set (or the cluster id is missing). Safe to call from inside the
    writer's own transaction: the filtered UPDATE rolls back with the row that
    triggered it and no-ops for every later footprint.

    An operator-grade ``reason`` additionally upgrades a stored reconstructible
    reason, without moving the timestamp — ``operational_footprint_at`` keeps
    meaning *first observed*, while the reason keeps meaning *the strongest
    footprint this connection ever acquired*.
    """
    if isinstance(cluster, ProxmoxCluster):
        cluster_id = cluster.pk
        # Fast path: a freshly loaded row already carrying an operator-grade
        # marker needs no query, because nothing upgrades past that. The UPDATE
        # below stays authoritative for every other case if the in-memory value
        # is stale, since the marker only ever moves NULL -> set and
        # reconstructible -> operator.
        already_operator_grade = (
            cluster.operational_footprint_at is not None
            and cluster.operational_footprint_reason in OPERATOR_FOOTPRINT_REASONS
        )
        if already_operator_grade:
            return False
    else:
        cluster_id = cluster
    if cluster_id is None:
        return False
    updated = ProxmoxCluster.objects.filter(pk=cluster_id, operational_footprint_at__isnull=True).update(
        operational_footprint_at=timezone.now(),
        operational_footprint_reason=reason,
    )
    if not updated and reason in OPERATOR_FOOTPRINT_REASONS:
        # Set-based and idempotent for the same reason the stamp above is: the
        # filter names exactly the reasons this may overwrite, so two concurrent
        # operator stamps cannot corrupt each other and neither can downgrade.
        ProxmoxCluster.objects.filter(
            pk=cluster_id,
            operational_footprint_reason__in=sorted(RECONSTRUCTIBLE_FOOTPRINT_REASONS),
        ).update(operational_footprint_reason=reason)
    return bool(updated)

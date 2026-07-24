"""The cluster lifecycle acquisition barrier.

Provider-operation acquisition and retirement race each other: acquisition reads
``retired_at``/``enabled`` and then contacts Proxmox; retirement flips those same
fields and tears the connection down. Left unsynchronised that is a classic TOCTOU
— acquisition passes its check a microsecond before retirement commits, then talks
to a cluster the operator just retired. The database constraints keep the *row*
consistent, but they cannot serialise a decision that spans a check and a later
side effect. A shared advisory lock does.

Two lock classes live here, and their order is the whole safety argument:

1. :func:`scan_admission_lock` — one installation-wide lock. The full scan is
   installation-wide (it snapshots every enabled endpoint), so a per-cluster lock
   cannot close its acquisition race; admission is serialised globally instead.
2. :func:`cluster_lifecycle_lock` — one lock per cluster, derived from the cluster
   key via :func:`cluster_advisory_lock_id`. Shared by provider-operation
   acquisition, ``disable_cluster``/``remove_stored_credential`` and (from R3)
   retirement.

**The documented lock order is: installation-wide scan admission -> cluster
lifecycle locks in ascending primary-key order -> operation-specific locks -> row
locks.** No caller may take these in the reverse order; that is what the
PostgreSQL concurrency tests prove. The advisory lock ids themselves are
blake2b-hashed and therefore unordered with respect to cluster identity, so the
deadlock argument is made over ``(lock class, cluster primary key)`` pairs, never
over the numeric ids.

Both context managers take PostgreSQL *transaction* advisory locks, so a caller
must already be inside ``transaction.atomic()``; the lock releases on commit or
rollback with no explicit unlock to leak. On SQLite (the Playwright/e2e path)
there are no advisory locks, so both are no-ops and serialisation falls back to
the row locks the callers also take — the real serialisation is only asserted
under PostgreSQL.
"""

from __future__ import annotations

from contextlib import contextmanager

from django.db import connection, transaction

from core.services.cluster_state_identity import cluster_advisory_lock_id

# Installation-wide scan admission lock. A fixed positive bigint (not cluster
# derived): there is exactly one global scan admission decision.
SCAN_ADMISSION_LOCK_ID = 0x50565343414E

# Base id for the per-cluster lifecycle lock, hashed with the cluster key by
# ``cluster_advisory_lock_id``. Distinct from every other base id in the codebase,
# which is all that is required for collision safety.
LIFECYCLE_LOCK_BASE = 0x50564C494643


class ClusterRetiredError(RuntimeError):
    """A cluster was already retired when acquisition reloaded it under the lock."""


class ClusterNotEnabledError(RuntimeError):
    """A cluster was disabled when a provider operation tried to acquire it."""


def _advisory_xact_lock(lock_id: int) -> None:
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_id])


@contextmanager
def scan_admission_lock():
    """Hold the installation-wide scan admission lock for the current transaction.

    Acquired before any per-cluster lifecycle lock. A queued/running global scan
    conservatively blocks retirement of every cluster it may have captured until
    scan scope is persisted more narrowly, and scan admission cannot snapshot
    endpoints while a retirement holds this lock.
    """
    _advisory_xact_lock(SCAN_ADMISSION_LOCK_ID)
    yield


@contextmanager
def cluster_lifecycle_lock(cluster):
    """Hold one cluster's lifecycle lock for the current transaction.

    Must be entered before the operation-specific locks and the row locks a caller
    then takes, and — when more than one cluster is locked in the same transaction
    — in ascending primary-key order.
    """
    _advisory_xact_lock(cluster_advisory_lock_id(LIFECYCLE_LOCK_BASE, cluster))
    yield


def acquire_operable_cluster(cluster, *, require_enabled: bool = True):
    """Take the lifecycle lock, reload the cluster under it, and assert operability.

    The acquisition half of the barrier: a provider operation calls this inside its
    own ``transaction.atomic()`` before contacting Proxmox, so the ``retired_at`` /
    ``enabled`` it acts on cannot change under it until it commits. Returns the
    row-locked cluster. Raises :class:`ClusterRetiredError` if the cluster was
    retired and, when ``require_enabled`` is set, :class:`ClusterNotEnabledError`
    if it was disabled — both as terminal results, not provider round-trips.
    """
    from core.models import ProxmoxCluster

    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("acquire_operable_cluster must run inside transaction.atomic().")
    with cluster_lifecycle_lock(cluster):
        locked = ProxmoxCluster.objects.select_for_update().get(pk=cluster.pk)
        if locked.retired_at is not None:
            raise ClusterRetiredError(f"Cluster '{locked.key}' has been retired and cannot accept new operations.")
        if require_enabled and not locked.enabled:
            raise ClusterNotEnabledError(f"Cluster '{locked.key}' is disabled.")
        return locked

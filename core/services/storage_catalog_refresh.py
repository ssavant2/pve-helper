"""Durable queueing for the datastore Refresh button.

The button used to be fire-and-forget: it enqueued
`refresh_storage_catalog_for_cluster` and answered `202 queued`, so nothing
recorded that the refresh had been asked for, nothing said whether it worked, and
the page never showed the result. A background job with no durable row is exactly
what AGENTS.md forbids, and it is why the button appeared to do nothing.

The shape here is the one `tag_inventory_refresh` already established: the audit
event is the durable operation record, it exists before the job is enqueued, an
enqueue failure is a terminal state rather than a lost job, and Recent Tasks
reads the same row — which is what lets the page soft-refresh when the refresh
lands.
"""

from __future__ import annotations

from contextlib import contextmanager

from django.db import connection, transaction
from django.utils import timezone
from django_q.tasks import async_task

from core.models import AuditEvent, ProxmoxCluster
from core.services.audit_events import record_audit_event
from core.services.cluster_state_identity import cluster_advisory_lock_id
from core.services.public_errors import public_exception_message
from core.services.storage_catalog import refresh_storage_metadata, refresh_storage_volumes
from core.services.task_queues import BULK_QUEUE_NAME

STORAGE_CATALOG_REFRESH_ACTION = "storage.catalog.refresh"
_QUEUE_LOCK_ID = 0x5056455343521
_WORKER_LOCK_ID = 0x5056455343522


class StorageCatalogRefreshAlreadyActive(RuntimeError):
    pass


class StorageCatalogRefreshQueueError(RuntimeError):
    pass


def _advisory_xact_lock(lock_id: int) -> None:
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_id])


@contextmanager
def _worker_lock(cluster):
    if connection.vendor != "postgresql":
        yield True
        return
    acquired = False
    lock_id = cluster_advisory_lock_id(_WORKER_LOCK_ID, cluster)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock_id])
            acquired = bool(cursor.fetchone()[0])
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])


def queue_storage_catalog_refresh(
    *,
    cluster: ProxmoxCluster,
    storage: str = "",
    request=None,
    user=None,
    username: str = "",
) -> tuple[AuditEvent, str]:
    """Record the refresh, then enqueue it. Never the other way round."""
    with transaction.atomic():
        _advisory_xact_lock(cluster_advisory_lock_id(_QUEUE_LOCK_ID, cluster))
        active = AuditEvent.objects.filter(
            action=STORAGE_CATALOG_REFRESH_ACTION,
            outcome__in=("queued", "running"),
            details__cluster_key=cluster.key,
        ).exists()
        if active:
            raise StorageCatalogRefreshAlreadyActive("A catalog refresh is already queued or running for this cluster.")
        event = record_audit_event(
            request,
            user=user,
            username=username,
            system_username="system",
            action=STORAGE_CATALOG_REFRESH_ACTION,
            object_type="storage_catalog",
            object_id=cluster.key,
            outcome="queued",
            cluster=cluster,
            details={
                "cluster_key": cluster.key,
                # Which datastore the operator was looking at. The refresh itself is
                # cluster-wide — this only names the target in Recent Tasks.
                "storage_id": storage,
                "stage": "queued",
                "queued_at": timezone.now().isoformat(),
            },
        )
    # Enqueueing happens after the record is committed, not inside the same
    # transaction. Recording the failure *and* raising inside one atomic block
    # rolls the record back with it, which is how "the queue is down" becomes "the
    # operator pressed a button and nothing anywhere says so". The cost is a row
    # that can be left claiming "queued" if the process dies right here — visible,
    # reapable, and strictly better than an invisible loss.
    try:
        task_id = async_task(
            "core.services.storage_catalog_refresh.execute_storage_catalog_refresh",
            event.id,
            q_options={"cluster": BULK_QUEUE_NAME},
        )
    except Exception as exc:
        details = {
            **event.details,
            "stage": "enqueue failed",
            "error": "The catalog refresh could not be queued.",
            "queue_error_type": exc.__class__.__name__,
            "finished_at": timezone.now().isoformat(),
        }
        event.outcome = "failed"
        event.details = details
        event.save(update_fields=["outcome", "details"])
        raise StorageCatalogRefreshQueueError(details["error"]) from exc
    event.details = {**event.details, "worker_task_id": task_id}
    event.save(update_fields=["details"])
    return event, task_id


def _finish(event: AuditEvent, *, outcome: str, stage: str, **updates) -> None:
    event.outcome = outcome
    event.details = {
        **(event.details if isinstance(event.details, dict) else {}),
        **updates,
        "stage": stage,
        "finished_at": timezone.now().isoformat(),
    }
    event.save(update_fields=["outcome", "details"])


def execute_storage_catalog_refresh(event_id: int) -> None:
    event = AuditEvent.objects.filter(pk=event_id, action=STORAGE_CATALOG_REFRESH_ACTION).first()
    if event is None or event.outcome != "queued":
        return
    cluster_key = str((event.details or {}).get("cluster_key") or "")
    cluster = ProxmoxCluster.objects.filter(key=cluster_key, enabled=True).first()
    if cluster is None:
        _finish(
            event,
            outcome="failed",
            stage="failed",
            error="The selected Proxmox cluster is no longer available.",
        )
        return

    with _worker_lock(cluster) as acquired:
        if not acquired:
            _finish(
                event,
                outcome="failed",
                stage="blocked",
                error="Another catalog refresh worker is still active; retry after it finishes.",
            )
            return

        with transaction.atomic():
            event = AuditEvent.objects.select_for_update().get(pk=event_id)
            if event.outcome != "queued":
                return
            event.outcome = "running"
            event.details = {
                **(event.details if isinstance(event.details, dict) else {}),
                "stage": "reading storage definitions",
                "started_at": timezone.now().isoformat(),
                "heartbeat_at": timezone.now().isoformat(),
            }
            event.save(update_fields=["outcome", "details"])

        try:
            state = refresh_storage_metadata(cluster)
        except Exception as exc:
            _finish(
                event,
                outcome="failed",
                stage="failed",
                error=public_exception_message(
                    exc,
                    operation="storage_catalog_refresh",
                    fallback="The storage catalog could not be read from Proxmox.",
                ),
            )
            return

        # Volume listing is the long half, and it only runs on complete metadata.
        # The heartbeat here is what distinguishes a working refresh from a dead
        # worker for the reaper and for the operator watching Recent Tasks.
        if state.metadata_complete:
            event.refresh_from_db(fields=["outcome", "details"])
            if event.outcome != "running":
                return
            event.details = {
                **(event.details if isinstance(event.details, dict) else {}),
                "stage": "listing volumes",
                "heartbeat_at": timezone.now().isoformat(),
            }
            event.save(update_fields=["details"])
            try:
                state = refresh_storage_volumes(cluster)
            except Exception as exc:
                _finish(
                    event,
                    outcome="failed",
                    stage="failed",
                    error=public_exception_message(
                        exc,
                        operation="storage_catalog_refresh",
                        fallback="The storage volume catalog could not be read from Proxmox.",
                    ),
                )
                return

        complete = state.metadata_complete and state.volume_complete
        _finish(
            event,
            outcome="success" if complete else "warning",
            stage="completed" if complete else "completed with incomplete coverage",
            metadata_complete=state.metadata_complete,
            volume_complete=state.volume_complete,
            # Node names, not exception text: these are the nodes whose answer is
            # missing, which is what "incomplete coverage" means to the operator.
            incomplete_nodes=sorted({*(state.metadata_errors or {}), *(state.volume_errors or {})}),
        )

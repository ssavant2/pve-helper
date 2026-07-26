"""The first inventory of a freshly added host or cluster, as a visible task.

Adding a connection used to end at the redirect: the periodic refreshes own
guests, storage and tags, so the new connection sat empty until whichever of them
came round next. The operator saw a working connection with no datastores, no
tags and no guests, and nothing anywhere said the app was about to fill them in.

So the add enqueues this, and it reports in Recent Tasks like any other bulk
operation. The shape is the one ``tag_inventory_refresh`` and
``storage_catalog_refresh`` already share: the audit event is the durable record,
it exists before the job is enqueued, an enqueue failure is a terminal state
rather than a lost job, and the row carries a heartbeat so a dead worker is
distinguishable from a slow one.

The one thing it does differently is its footprint reason. This is provider work
the *app* started, not something an operator ran, and every row it writes is a
projection the next refresh rebuilds — so it stamps
:data:`~core.services.cluster_footprint.FOOTPRINT_INVENTORY_BOOTSTRAP`, which is
reconstructible. Stamping ``provider_operation`` here would have made every newly
added connection permanently undeletable within seconds of being added, which is
precisely the failure ``Delete unused connection`` was just rescued from.
"""

from __future__ import annotations

from contextlib import contextmanager

from django.db import connection, transaction
from django.utils import timezone
from django_q.tasks import async_task

from core.models import AuditEvent, ProxmoxCluster
from core.services.audit_events import record_audit_event
from core.services.cluster_scopes import provider_acquirable_clusters
from core.services.cluster_state_identity import cluster_advisory_lock_id
from core.services.current_guest_inventory import reconcile_live_guest_inventory
from core.services.proxmox import fetch_verified_guest_inventory
from core.services.public_errors import public_exception_message
from core.services.storage_catalog import refresh_storage_catalog
from core.services.tag_registry import refresh_registered_tags
from core.services.task_queues import BULK_QUEUE_NAME

CLUSTER_INVENTORY_BOOTSTRAP_ACTION = "cluster.inventory.bootstrap"
_QUEUE_LOCK_ID = 0x50564543494201
_WORKER_LOCK_ID = 0x50564543494202


class ClusterInventoryBootstrapAlreadyActive(RuntimeError):
    pass


class ClusterInventoryBootstrapQueueError(RuntimeError):
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


def queue_cluster_inventory_bootstrap(
    *,
    cluster: ProxmoxCluster,
    request=None,
    user=None,
    username: str = "",
) -> tuple[AuditEvent, str]:
    """Record the bootstrap, then enqueue it. Never the other way round."""
    with transaction.atomic():
        _advisory_xact_lock(cluster_advisory_lock_id(_QUEUE_LOCK_ID, cluster))
        active = AuditEvent.objects.filter(
            action=CLUSTER_INVENTORY_BOOTSTRAP_ACTION,
            outcome__in=("queued", "running"),
            details__cluster_key=cluster.key,
        ).exists()
        if active:
            raise ClusterInventoryBootstrapAlreadyActive(
                "An inventory bootstrap is already queued or running for this connection."
            )
        event = record_audit_event(
            request,
            user=user,
            username=username,
            system_username="system",
            action=CLUSTER_INVENTORY_BOOTSTRAP_ACTION,
            object_type="cluster",
            object_id=cluster.key,
            outcome="queued",
            cluster=cluster,
            details={
                "cluster_key": cluster.key,
                "display_name": cluster.display_name,
                "stage": "queued",
                "queued_at": timezone.now().isoformat(),
            },
        )
    # Outside the transaction, for the reason spelled out in
    # `storage_catalog_refresh.queue_storage_catalog_refresh`: recording the
    # failure inside the same atomic block rolls the record back with it.
    try:
        task_id = async_task(
            "core.services.cluster_inventory_bootstrap.execute_cluster_inventory_bootstrap",
            event.id,
            q_options={"cluster": BULK_QUEUE_NAME},
        )
    except Exception as exc:
        details = {
            **event.details,
            "stage": "enqueue failed",
            "error": "The first inventory could not be queued.",
            "queue_error_type": exc.__class__.__name__,
            "finished_at": timezone.now().isoformat(),
        }
        event.outcome = "failed"
        event.details = details
        event.save(update_fields=["outcome", "details"])
        raise ClusterInventoryBootstrapQueueError(details["error"]) from exc
    event.details = {**event.details, "worker_task_id": task_id}
    event.save(update_fields=["details"])
    return event, task_id


def _save_progress(event: AuditEvent, *, stage: str, **updates) -> bool:
    """Advance the heartbeat, unless someone else already finalized the row."""
    event.refresh_from_db(fields=["outcome", "details"])
    if event.outcome != "running":
        return False
    event.details = {
        **(event.details if isinstance(event.details, dict) else {}),
        **updates,
        "stage": stage,
        "heartbeat_at": timezone.now().isoformat(),
    }
    event.save(update_fields=["details"])
    return True


def _finish(event: AuditEvent, *, outcome: str, stage: str, **updates) -> None:
    event.outcome = outcome
    event.details = {
        **(event.details if isinstance(event.details, dict) else {}),
        **updates,
        "stage": stage,
        "finished_at": timezone.now().isoformat(),
    }
    event.save(update_fields=["outcome", "details"])


def execute_cluster_inventory_bootstrap(event_id: int) -> None:
    event = AuditEvent.objects.filter(pk=event_id, action=CLUSTER_INVENTORY_BOOTSTRAP_ACTION).first()
    if event is None or event.outcome != "queued":
        return
    cluster_key = str((event.details or {}).get("cluster_key") or "")
    cluster = provider_acquirable_clusters().filter(key=cluster_key).first()
    if cluster is None:
        _finish(
            event,
            outcome="failed",
            stage="failed",
            error="The connection can no longer be contacted, so its first inventory was not collected.",
        )
        return

    with _worker_lock(cluster) as acquired:
        if not acquired:
            _finish(
                event,
                outcome="failed",
                stage="blocked",
                error="Another inventory bootstrap worker is still active for this connection.",
            )
            return

        with transaction.atomic():
            event = AuditEvent.objects.select_for_update().get(pk=event_id)
            if event.outcome != "queued":
                return
            event.outcome = "running"
            event.details = {
                **(event.details if isinstance(event.details, dict) else {}),
                "stage": "reading guests",
                "started_at": timezone.now().isoformat(),
                "heartbeat_at": timezone.now().isoformat(),
            }
            event.save(update_fields=["outcome", "details"])

        # Guests first: the inventory tree, the overview and the tag membership all
        # read the guest projection, so it is the stage that makes the new
        # connection stop looking empty.
        try:
            inventory = fetch_verified_guest_inventory(cluster=cluster)
            guest_state = reconcile_live_guest_inventory(inventory)
        except Exception as exc:
            _finish(
                event,
                outcome="failed",
                stage="failed",
                error=public_exception_message(
                    exc,
                    operation="cluster_inventory_bootstrap",
                    fallback="The guest inventory could not be read from Proxmox.",
                ),
            )
            return
        if not _save_progress(
            event,
            stage="reading storage catalog",
            guests=len(inventory.guests),
            guests_complete=guest_state.complete,
        ):
            return

        try:
            catalog_state = refresh_storage_catalog(cluster)
        except Exception as exc:
            _finish(
                event,
                outcome="failed",
                stage="failed",
                error=public_exception_message(
                    exc,
                    operation="cluster_inventory_bootstrap",
                    fallback="The storage catalog could not be read from Proxmox.",
                ),
            )
            return
        if not _save_progress(
            event,
            stage="reading tags",
            metadata_complete=catalog_state.metadata_complete,
            volume_complete=catalog_state.volume_complete,
        ):
            return

        # Tags last and non-fatally: the registry reports its own error rather
        # than raising, and a connection whose guests and datastores are in place
        # is usable even if the tag registry answered badly.
        registered, registry_error = refresh_registered_tags(cluster=cluster)

        complete = (
            guest_state.complete
            and catalog_state.metadata_complete
            and catalog_state.volume_complete
            and not registry_error
        )
        _finish(
            event,
            outcome="success" if complete else "warning",
            stage="completed" if complete else "completed with incomplete coverage",
            guests=len(inventory.guests),
            guests_complete=guest_state.complete,
            metadata_complete=catalog_state.metadata_complete,
            volume_complete=catalog_state.volume_complete,
            tags=len(registered),
            registry_error=registry_error,
            # Node names, not exception text: these are the nodes whose answer is
            # missing, which is what "incomplete coverage" means to the operator.
            incomplete_nodes=sorted(
                {*(catalog_state.metadata_errors or {}), *(catalog_state.volume_errors or {})},
            ),
        )

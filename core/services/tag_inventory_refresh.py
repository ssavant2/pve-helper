from __future__ import annotations

from contextlib import contextmanager

from django.db import connection, transaction
from django.utils import timezone
from django_q.tasks import async_task

from core.models import AuditEvent, ProxmoxCluster
from core.services.audit_events import record_audit_event
from core.services.cluster_state_identity import cluster_advisory_lock_id
from core.services.current_guest_inventory import reconcile_live_guest_inventory
from core.services.proxmox import fetch_verified_guest_inventory
from core.services.public_errors import public_exception_message
from core.services.tag_registry import refresh_registered_tags, resolve_tag_registry_cluster
from core.services.task_queues import BULK_QUEUE_NAME

TAG_INVENTORY_REFRESH_ACTION = "tag.inventory.refresh"
_QUEUE_LOCK_ID = 0x50564554414701
_WORKER_LOCK_ID = 0x50564554414702


class TagInventoryRefreshAlreadyActive(RuntimeError):
    pass


class TagInventoryRefreshQueueError(RuntimeError):
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


def queue_tag_inventory_refresh(*, cluster, request=None, user=None, username: str = "") -> tuple[AuditEvent, str]:
    cluster, cluster_error = resolve_tag_registry_cluster(cluster)
    if cluster is None:
        raise TagInventoryRefreshQueueError(cluster_error)
    with transaction.atomic():
        _advisory_xact_lock(cluster_advisory_lock_id(_QUEUE_LOCK_ID, cluster))
        active = (
            AuditEvent.objects.filter(
                action=TAG_INVENTORY_REFRESH_ACTION,
                outcome__in=("queued", "running"),
                details__cluster_key=cluster.key,
            )
            .order_by("-timestamp")
            .first()
        )
        if active is not None:
            raise TagInventoryRefreshAlreadyActive("A tag inventory refresh is already queued or running.")
        event = record_audit_event(
            request,
            user=user,
            username=username,
            system_username="system",
            action=TAG_INVENTORY_REFRESH_ACTION,
            object_type="tag_inventory",
            object_id=cluster.key,
            outcome="queued",
            cluster=cluster,
            details={
                "cluster_key": cluster.key,
                "stage": "queued",
                "queued_at": timezone.now().isoformat(),
            },
        )
    # Outside the atomic block on purpose. Writing the failure state *and* raising
    # inside the same transaction rolls the record back with the exception, so a
    # broker outage left no trace that the operator had ever asked for a refresh —
    # the opposite of what a durable operation record is for. A row left claiming
    # "queued" because the process died right here is visible and reapable; a lost
    # one is not.
    try:
        task_id = async_task(
            "core.services.tag_inventory_refresh.execute_tag_inventory_refresh",
            event.id,
            q_options={"cluster": BULK_QUEUE_NAME},
        )
    except Exception as exc:
        details = {
            **event.details,
            "stage": "enqueue failed",
            "error": "The tag inventory refresh could not be queued.",
            "queue_error_type": exc.__class__.__name__,
            "finished_at": timezone.now().isoformat(),
        }
        event.outcome = "failed"
        event.details = details
        event.save(update_fields=["outcome", "details"])
        raise TagInventoryRefreshQueueError(details["error"]) from exc
    event.details = {**event.details, "worker_task_id": task_id}
    event.save(update_fields=["details"])
    return event, task_id


def _save_progress(event: AuditEvent, *, stage: str, **updates) -> bool:
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


def execute_tag_inventory_refresh(event_id: int) -> None:
    event = AuditEvent.objects.filter(pk=event_id, action=TAG_INVENTORY_REFRESH_ACTION).first()
    if event is None or event.outcome != "queued":
        return
    cluster_key = str((event.details or {}).get("cluster_key") or "")
    cluster = ProxmoxCluster.objects.filter(key=cluster_key).first()
    if cluster is None:
        event.outcome = "failed"
        event.details = {
            **(event.details if isinstance(event.details, dict) else {}),
            "stage": "failed",
            "error": "The selected Proxmox cluster no longer exists.",
            "finished_at": timezone.now().isoformat(),
        }
        event.save(update_fields=["outcome", "details"])
        return
    with _worker_lock(cluster) as acquired:
        if not acquired:
            event.outcome = "failed"
            event.details = {
                **(event.details if isinstance(event.details, dict) else {}),
                "stage": "blocked",
                "error": "Another tag inventory refresh worker is still active; retry after it finishes.",
                "finished_at": timezone.now().isoformat(),
            }
            event.save(update_fields=["outcome", "details"])
            return

        with transaction.atomic():
            event = AuditEvent.objects.select_for_update().get(pk=event_id)
            if event.outcome != "queued":
                return
            event.outcome = "running"
            event.details = {
                **(event.details if isinstance(event.details, dict) else {}),
                "stage": "refreshing tag registry",
                "started_at": timezone.now().isoformat(),
                "heartbeat_at": timezone.now().isoformat(),
            }
            event.save(update_fields=["outcome", "details"])

        registered, registry_error = refresh_registered_tags(cluster=cluster)
        if not _save_progress(
            event,
            stage="refreshing guest membership",
            registry_count=len(registered),
            registry_error=registry_error,
        ):
            return

        try:
            inventory = fetch_verified_guest_inventory(cluster=cluster)
        except Exception as exc:
            error = public_exception_message(
                exc,
                operation="tag_inventory_refresh",
                fallback="The current guest inventory could not be read from Proxmox.",
            )
            event.refresh_from_db(fields=["outcome", "details"])
            if event.outcome != "running":
                return
            event.outcome = "failed"
            event.details = {
                **(event.details if isinstance(event.details, dict) else {}),
                "stage": "failed",
                "error": error,
                "finished_at": timezone.now().isoformat(),
            }
            event.save(update_fields=["outcome", "details"])
            return

        event.refresh_from_db(fields=["outcome", "details"])
        if event.outcome != "running":
            return
        membership_reconciled = bool(inventory.successful_endpoints)
        state = reconcile_live_guest_inventory(inventory) if membership_reconciled else None
        membership_errors = list(inventory.errors)
        warnings = ([registry_error] if registry_error else []) + membership_errors
        any_success = not registry_error or membership_reconciled
        complete = not registry_error and inventory.complete
        # A degraded endpoint is surfaced as a warning without making the cluster's
        # coverage partial: membership is still fully reconciled from the
        # authoritative answer, but the operator should see the degradation.
        clean = complete and not warnings
        event.outcome = "success" if clean else ("warning" if any_success else "failed")
        details = {
            **(event.details if isinstance(event.details, dict) else {}),
            "stage": "completed" if clean else ("completed with warnings" if any_success else "failed"),
            "registry_count": len(registered),
            "registry_error": registry_error,
            "membership_reconciled": membership_reconciled,
            "membership_complete": inventory.complete,
            "endpoints_attempted": list(inventory.attempted_endpoints),
            "endpoints_succeeded": list(inventory.successful_endpoints),
            "membership_errors": membership_errors,
            "warnings": warnings,
            "refreshed_at": state.refreshed_at.isoformat() if state and state.refreshed_at else "",
            "finished_at": timezone.now().isoformat(),
        }
        if not any_success:
            details["error"] = "Neither the tag registry nor guest membership could be refreshed."
        event.details = details
        event.save(update_fields=["outcome", "details"])

"""Durable manual refreshes for one host-projection scope."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager

from django.db import connection, transaction
from django.utils import timezone
from django_q.tasks import async_task

from core.models import AuditEvent, ProxmoxCluster
from core.services.audit_events import record_audit_event
from core.services.cluster_lifecycle_lock import cluster_lifecycle_lock
from core.services.cluster_membership import refresh_cluster_membership
from core.services.cluster_node_runtime import refresh_node_runtime
from core.services.cluster_scopes import historical_clusters
from core.services.cluster_state_identity import cluster_advisory_lock_id
from core.services.host_projection_singleflight import host_projection_refresh_lock
from core.services.public_errors import public_exception_message
from core.services.refs import NodeRef, RefParseError
from core.services.task_queues import BULK_QUEUE_NAME

CLUSTER_HOST_REFRESH_ACTION = "cluster.host_projection.refresh"
HOST_REFRESH_SCOPE_MEMBERSHIP = "membership"
HOST_REFRESH_SCOPE_NODE_RUNTIME = "node_runtime"
HOST_REFRESH_SCOPES = frozenset({HOST_REFRESH_SCOPE_MEMBERSHIP, HOST_REFRESH_SCOPE_NODE_RUNTIME})

_QUEUE_LOCK_ID = 0x50564548525101


class ClusterHostRefreshAlreadyActive(RuntimeError):
    pass


class ClusterHostRefreshQueueError(RuntimeError):
    pass


class ClusterHostRefreshRetryError(RuntimeError):
    pass


def _advisory_xact_lock(lock_id: int) -> None:
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_id])


def _scope_identity(cluster: ProxmoxCluster, scope: str, node_name: str) -> tuple[str, str]:
    scope = str(scope or "").strip()
    node_name = str(node_name or "").strip()
    if scope not in HOST_REFRESH_SCOPES:
        raise ClusterHostRefreshQueueError("This host projection scope cannot be refreshed.")
    if scope == HOST_REFRESH_SCOPE_MEMBERSHIP:
        if node_name:
            raise ClusterHostRefreshQueueError("A membership refresh cannot target one node.")
        return scope, ""
    try:
        node_ref = NodeRef(cluster_key=cluster.key, node=node_name)
    except RefParseError as exc:
        raise ClusterHostRefreshQueueError("Select a valid node to refresh.") from exc
    return scope, node_ref.serialize()


def _locked_operable_cluster(cluster: ProxmoxCluster) -> ProxmoxCluster:
    locked = historical_clusters().select_for_update().get(pk=cluster.pk)
    if locked.retired_at is not None:
        raise ClusterHostRefreshQueueError("The selected Proxmox cluster has been retired.")
    if not locked.enabled:
        raise ClusterHostRefreshQueueError("The selected Proxmox cluster is disabled.")
    return locked


def _active_scope_exists(*, cluster: ProxmoxCluster, scope: str, node_ref: str, exclude_id: int | None = None) -> bool:
    active = AuditEvent.objects.filter(
        action=CLUSTER_HOST_REFRESH_ACTION,
        outcome__in=("queued", "running"),
        cluster=cluster,
        details__scope=scope,
        details__node_ref=node_ref,
    )
    if exclude_id is not None:
        active = active.exclude(pk=exclude_id)
    return active.exists()


def _enqueue(event: AuditEvent, *, attempt: int) -> str:
    try:
        task_id = async_task(
            "core.services.cluster_host_refresh.execute_cluster_host_refresh",
            event.id,
            attempt,
            q_options={"cluster": BULK_QUEUE_NAME},
        )
    except Exception as exc:
        with transaction.atomic():
            current = AuditEvent.objects.select_for_update().get(pk=event.pk)
            details = dict(current.details) if isinstance(current.details, dict) else {}
            if current.outcome == "queued" and int(details.get("attempt") or 0) == attempt:
                details.update(
                    {
                        "stage": "enqueue failed",
                        "error": "The host projection refresh could not be queued.",
                        "queue_error_type": exc.__class__.__name__,
                        "retryable": True,
                        "finished_at": timezone.now().isoformat(),
                    }
                )
                current.outcome = "failed"
                current.details = details
                current.save(update_fields=["outcome", "details"])
        raise ClusterHostRefreshQueueError("The host projection refresh could not be queued; retry is safe.") from exc

    with transaction.atomic():
        current = AuditEvent.objects.select_for_update().get(pk=event.pk)
        details = dict(current.details) if isinstance(current.details, dict) else {}
        if int(details.get("attempt") or 0) == attempt:
            details["worker_task_id"] = task_id
            current.details = details
            current.save(update_fields=["details"])
    return task_id


def queue_cluster_host_refresh(
    *,
    cluster: ProxmoxCluster,
    scope: str,
    node_name: str = "",
    request=None,
    user=None,
    username: str = "",
) -> tuple[AuditEvent, str]:
    """Commit one exact-scope operation before handing it to the broker."""
    scope, node_ref = _scope_identity(cluster, scope, node_name)
    with transaction.atomic():
        with cluster_lifecycle_lock(cluster):
            _advisory_xact_lock(cluster_advisory_lock_id(_QUEUE_LOCK_ID, cluster))
            cluster = _locked_operable_cluster(cluster)
            if _active_scope_exists(cluster=cluster, scope=scope, node_ref=node_ref):
                raise ClusterHostRefreshAlreadyActive("This host projection scope is already queued or running.")
            now = timezone.now().isoformat()
            event = record_audit_event(
                request,
                user=user,
                username=username,
                system_username="system",
                action=CLUSTER_HOST_REFRESH_ACTION,
                object_type="cluster_host_projection",
                object_id=node_ref or cluster.key,
                outcome="queued",
                cluster=cluster,
                details={
                    "cluster_key": cluster.key,
                    "scope": scope,
                    "node_ref": node_ref,
                    "attempt": 0,
                    "stage": "queued",
                    "queued_at": now,
                },
            )
    task_id = _enqueue(event, attempt=0)
    event.refresh_from_db()
    return event, task_id


def retry_cluster_host_refresh(event_id: int) -> str:
    """Requeue one failed attempt without creating a second audit identity."""
    event = AuditEvent.objects.filter(pk=event_id, action=CLUSTER_HOST_REFRESH_ACTION).select_related("cluster").first()
    if event is None or event.cluster_id is None:
        raise ClusterHostRefreshRetryError("This host projection refresh is not available for retry.")
    details = dict(event.details) if isinstance(event.details, dict) else {}
    try:
        node_name = NodeRef.parse(str(details.get("node_ref"))).node if details.get("node_ref") else ""
        scope, node_ref = _scope_identity(event.cluster, str(details.get("scope") or ""), node_name)
    except (ClusterHostRefreshQueueError, RefParseError) as exc:
        raise ClusterHostRefreshRetryError("This host projection refresh is not available for retry.") from exc
    with transaction.atomic():
        with cluster_lifecycle_lock(event.cluster):
            _advisory_xact_lock(cluster_advisory_lock_id(_QUEUE_LOCK_ID, event.cluster))
            cluster = _locked_operable_cluster(event.cluster)
            event = AuditEvent.objects.select_for_update().get(pk=event_id)
            details = dict(event.details) if isinstance(event.details, dict) else {}
            if event.outcome != "failed" or details.get("retryable") is not True:
                raise ClusterHostRefreshRetryError("This host projection refresh is not available for retry.")
            if _active_scope_exists(cluster=cluster, scope=scope, node_ref=node_ref, exclude_id=event.id):
                raise ClusterHostRefreshAlreadyActive("This host projection scope is already queued or running.")
            attempt = int(details.get("attempt") or 0) + 1
            for key in (
                "coverage_error",
                "error",
                "finished_at",
                "heartbeat_at",
                "interrupted_at",
                "queue_error_type",
                "retryable",
                "started_at",
                "worker_task_id",
            ):
                details.pop(key, None)
            details.update(
                {
                    "attempt": attempt,
                    "stage": "queued",
                    "queued_at": timezone.now().isoformat(),
                }
            )
            event.outcome = "queued"
            event.details = details
            event.save(update_fields=["outcome", "details"])
    return _enqueue(event, attempt=attempt)


def _finish(event_id: int, attempt: int, *, outcome: str, stage: str, **updates) -> bool:
    with transaction.atomic():
        event = AuditEvent.objects.select_for_update().get(pk=event_id)
        details = dict(event.details) if isinstance(event.details, dict) else {}
        if event.outcome != "running" or int(details.get("attempt") or 0) != attempt:
            return False
        details.update(updates)
        details.update({"stage": stage, "finished_at": timezone.now().isoformat()})
        event.outcome = outcome
        event.details = details
        event.save(update_fields=["outcome", "details"])
        return True


@contextmanager
def _worker_acquisition(event_id: int, attempt: int, cluster: ProxmoxCluster):
    """Commit ``running`` while retaining the session single-flight lock."""
    ready = None
    lock_stack = ExitStack()
    try:
        with transaction.atomic():
            with cluster_lifecycle_lock(cluster):
                acquired = lock_stack.enter_context(host_projection_refresh_lock(cluster))
                cluster = historical_clusters().select_for_update().get(pk=cluster.pk)
                event = AuditEvent.objects.select_for_update().get(pk=event_id)
                details = dict(event.details) if isinstance(event.details, dict) else {}
                if event.outcome != "queued" or int(details.get("attempt") or 0) != attempt:
                    pass
                elif cluster.retired_at is not None or not cluster.enabled:
                    details.update(
                        {
                            "stage": "failed",
                            "error": "The selected Proxmox cluster has been retired."
                            if cluster.retired_at is not None
                            else "The selected Proxmox cluster is disabled.",
                            "retryable": False,
                            "finished_at": timezone.now().isoformat(),
                        }
                    )
                    event.outcome = "failed"
                    event.details = details
                    event.save(update_fields=["outcome", "details"])
                elif not acquired:
                    details.update(
                        {
                            "stage": "blocked",
                            "error": "Another host projection refresh is already running; retry is safe.",
                            "retryable": True,
                            "finished_at": timezone.now().isoformat(),
                        }
                    )
                    event.outcome = "failed"
                    event.details = details
                    event.save(update_fields=["outcome", "details"])
                else:
                    details.update(
                        {
                            "stage": "refreshing membership"
                            if details.get("scope") == HOST_REFRESH_SCOPE_MEMBERSHIP
                            else "refreshing node runtime",
                            "started_at": timezone.now().isoformat(),
                            "heartbeat_at": timezone.now().isoformat(),
                        }
                    )
                    event.outcome = "running"
                    event.details = details
                    event.save(update_fields=["outcome", "details"])
                    ready = (cluster, details)
        yield ready
    finally:
        lock_stack.close()


def execute_cluster_host_refresh(event_id: int, attempt: int = 0) -> None:
    """Run exactly one publisher under the lifecycle and single-flight barriers."""
    event = AuditEvent.objects.filter(pk=event_id, action=CLUSTER_HOST_REFRESH_ACTION).select_related("cluster").first()
    if event is None or event.cluster_id is None:
        return
    cluster = event.cluster

    with _worker_acquisition(event_id, attempt, cluster) as acquired:
        if acquired is None:
            return
        cluster, details = acquired
        try:
            if details.get("scope") == HOST_REFRESH_SCOPE_MEMBERSHIP:
                result = refresh_cluster_membership(cluster)
                result_updates = {"generation": result.generation}
            else:
                node_ref = NodeRef.parse(str(details.get("node_ref") or ""))
                result = refresh_node_runtime(cluster, node_ref.node)
                result_updates = {
                    "generation": result.generation,
                    "based_on_generation": result.based_on_generation,
                }
        except Exception as exc:
            _finish(
                event_id,
                attempt,
                outcome="failed",
                stage="failed",
                error=public_exception_message(
                    exc,
                    operation="cluster_host_projection_refresh",
                    fallback="The host projection scope could not be refreshed.",
                ),
                retryable=True,
            )
            return

        if result.complete:
            _finish(event_id, attempt, outcome="success", stage="completed", **result_updates)
        else:
            _finish(
                event_id,
                attempt,
                outcome="failed",
                stage="incomplete",
                coverage_error=result.error_code,
                error="The host projection scope could not be refreshed.",
                retryable=True,
                **result_updates,
            )

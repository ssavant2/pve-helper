from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django_q.models import Schedule
from django_q.tasks import async_task

from core.models import ProxmoxEndpoint, ScheduledAction, ScheduledActionRun
from core.services.audit_events import record_audit_event
from core.services.cluster_resolver import cluster_clients
from core.services.current_guest_inventory import refresh_current_guest_from_client
from core.services.proxmox import ProxmoxAPIError, ProxmoxClient, ProxmoxTaskTimeout, clear_live_guest_caches
from core.services.public_errors import (
    ERROR_CODE_PROVIDER,
    ERROR_CODE_TASK_TIMEOUT,
    ERROR_CODE_UNEXPECTED,
    PROVIDER_FAILURE_MESSAGE,
    TASK_TIMEOUT_MESSAGE,
    UNEXPECTED_FAILURE_MESSAGE,
    PublicFailure,
    PublicMessageError,
    proxmox_task_failure,
    public_exception_message,
    public_failure,
)
from core.services.scheduled_recurrence import RecurrenceError, next_run_after

SCHEDULED_ACTION_DISPATCH_SCHEDULE_NAME = "pve-helper scheduled action dispatcher"
SCHEDULED_ACTION_DISPATCH_FUNC = "core.tasks.dispatch_scheduled_actions"
SCHEDULED_ACTION_EXECUTION_FUNC = "core.tasks.run_scheduled_action"
SCHEDULED_ACTION_DISPATCH_INTERVAL_MINUTES = 1
DISPATCH_GRACE = timedelta(seconds=120)
STALE_RUN_GRACE = timedelta(minutes=5)

logger = logging.getLogger(__name__)

IN_FLIGHT_RUN_STATUSES = {
    ScheduledActionRun.Status.QUEUED,
    ScheduledActionRun.Status.PREFLIGHT,
    ScheduledActionRun.Status.SUBMITTED,
    ScheduledActionRun.Status.POLLING,
}


@dataclass(frozen=True)
class DispatchResult:
    queued: int = 0
    missed: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class GuestTarget:
    endpoint: ProxmoxEndpoint
    client: ProxmoxClient
    node: str
    current: dict[str, Any]
    config: dict[str, Any]


class ScheduledActionExecutionError(PublicMessageError, Exception):
    def __init__(self, message: str, *, preflight: dict[str, Any] | None = None):
        super().__init__(message)
        self.preflight = preflight or {}


class ScheduledActionQueueError(Exception):
    pass


def ensure_scheduled_action_dispatch_schedule() -> Schedule:
    defaults = {
        "func": SCHEDULED_ACTION_DISPATCH_FUNC,
        "schedule_type": Schedule.MINUTES,
        "minutes": SCHEDULED_ACTION_DISPATCH_INTERVAL_MINUTES,
        "repeats": -1,
        "next_run": timezone.now() + timedelta(minutes=1),
        "cluster": settings.Q_CLUSTER.get("name"),
    }
    schedule, created = Schedule.objects.get_or_create(
        name=SCHEDULED_ACTION_DISPATCH_SCHEDULE_NAME,
        defaults=defaults,
    )
    if created:
        return schedule

    updates = {key: value for key, value in defaults.items() if key != "next_run" and getattr(schedule, key) != value}
    if updates:
        for field, value in updates.items():
            setattr(schedule, field, value)
        schedule.save(update_fields=[*updates.keys()])
    return schedule


def dispatch_due_scheduled_actions(
    *,
    limit: int = 100,
    now=None,
    enqueue_func: Callable[..., Any] = async_task,
) -> DispatchResult:
    reap_stale_scheduled_action_runs(now=now)
    prune_scheduled_action_runs(retention_days=settings.SCHEDULED_ACTION_RUN_RETENTION_DAYS, now=now)

    now = now or timezone.now()
    queued_runs: list[ScheduledActionRun] = []
    missed_runs: list[ScheduledActionRun] = []
    skipped = 0

    with transaction.atomic():
        actions = list(
            ScheduledAction.objects.select_for_update(skip_locked=True)
            .filter(
                enabled=True,
                deleted_at__isnull=True,
                next_run_at__isnull=False,
                next_run_at__lte=now,
            )
            .order_by("next_run_at", "id")[:limit]
        )

        for action in actions:
            planned_for = action.next_run_at
            if planned_for is None:
                skipped += 1
                continue

            if _has_in_flight_run(action):
                skipped += 1
                continue

            occurrence_key = _occurrence_key(planned_for)
            if ScheduledActionRun.objects.filter(
                scheduled_action=action,
                occurrence_key=occurrence_key,
            ).exists():
                _advance_action_after_claim(action, now, ScheduledAction.LastStatus.SKIPPED)
                skipped += 1
                continue

            if _is_missed(action, planned_for, now):
                advance_error = _advance_action_after_claim(action, now, ScheduledAction.LastStatus.MISSED)
                run = ScheduledActionRun.objects.create(
                    scheduled_action=action,
                    planned_for=planned_for,
                    occurrence_key=occurrence_key,
                    status=ScheduledActionRun.Status.MISSED,
                    outcome=ScheduledActionRun.Outcome.MISSED,
                    finished_at=now,
                    error=advance_error or "Scheduled run was outside its allowed dispatch window.",
                )
                missed_runs.append(run)
                continue

            advance_error = _advance_action_after_claim(action, now, ScheduledAction.LastStatus.QUEUED)
            run = ScheduledActionRun.objects.create(
                scheduled_action=action,
                planned_for=planned_for,
                occurrence_key=occurrence_key,
                status=ScheduledActionRun.Status.QUEUED,
                error=advance_error,
            )
            queued_runs.append(run)

    for run in queued_runs:
        task_id = enqueue_func(SCHEDULED_ACTION_EXECUTION_FUNC, run.id)
        _audit_run(
            run,
            action="scheduled_action.run_queued",
            outcome="success",
            details={"task_id": task_id},
        )

    for run in missed_runs:
        _audit_run(
            run,
            action="scheduled_action.run_missed",
            outcome="missed",
            details={"reason": run.error},
        )

    return DispatchResult(queued=len(queued_runs), missed=len(missed_runs), skipped=skipped)


def queue_manual_scheduled_action_run(
    action: ScheduledAction,
    *,
    triggered_by=None,
    now=None,
    enqueue_func: Callable[..., Any] | None = None,
) -> ScheduledActionRun:
    now = now or timezone.now()
    with transaction.atomic():
        action = ScheduledAction.objects.select_for_update().get(pk=action.pk)
        if action.deleted_at is not None:
            raise ScheduledActionQueueError("This scheduled action has been deleted.")
        if _has_in_flight_run(action):
            raise ScheduledActionQueueError("This scheduled action already has a run in progress.")

        run = ScheduledActionRun.objects.create(
            scheduled_action=action,
            planned_for=now,
            occurrence_key=_manual_occurrence_key(action, now, triggered_by),
            status=ScheduledActionRun.Status.QUEUED,
            triggered_by=triggered_by if getattr(triggered_by, "is_authenticated", False) else None,
        )
        action.last_status = ScheduledAction.LastStatus.QUEUED
        action.last_run_at = now
        action.save(update_fields=["last_status", "last_run_at", "updated_at"])

    enqueue = enqueue_func or async_task
    task_id = enqueue(SCHEDULED_ACTION_EXECUTION_FUNC, run.id)
    _audit_run(
        run,
        action="scheduled_action.run_queued",
        outcome="success",
        details={"task_id": task_id, "source": "manual"},
    )
    return run


def reap_stale_scheduled_action_runs(*, now=None) -> int:
    now = now or timezone.now()
    stale_count = 0
    runs = (
        ScheduledActionRun.objects.select_related("scheduled_action")
        .filter(status__in=IN_FLIGHT_RUN_STATUSES)
        .order_by("created_at")
    )
    for run in runs:
        reference_time = run.started_at or run.created_at
        timeout = max(run.scheduled_action.action_timeout_seconds, settings.SCHEDULED_ACTION_TIMEOUT_SECONDS)
        stale_after = reference_time + timedelta(seconds=timeout) + STALE_RUN_GRACE
        if stale_after > now:
            continue

        _finish_run(
            run,
            status=ScheduledActionRun.Status.STALE,
            outcome=ScheduledActionRun.Outcome.STALE,
            action_status=ScheduledAction.LastStatus.FAILED,
            error="Scheduled action run was marked stale after worker timeout.",
        )
        stale_count += 1
    return stale_count


def prune_scheduled_action_runs(*, retention_days: int | None = None, now=None) -> int:
    retention_days = retention_days if retention_days is not None else settings.SCHEDULED_ACTION_RUN_RETENTION_DAYS
    now = now or timezone.now()
    cutoff = now - timedelta(days=retention_days)
    deleted_count, _deleted_by_model = (
        ScheduledActionRun.objects.exclude(status__in=IN_FLIGHT_RUN_STATUSES).filter(finished_at__lt=cutoff).delete()
    )

    if deleted_count:
        record_audit_event(
            username="system",
            action="scheduled_action.run_retention.purge",
            object_type="scheduled_action_run",
            outcome="success",
            details={
                "retention_days": retention_days,
                "purged": deleted_count,
            },
        )
    return deleted_count


def execute_scheduled_action_run(
    run_id: int,
    *,
    client_factory: Callable[[str], ProxmoxClient] | None = None,
) -> None:
    run = _start_run(run_id)
    if run is None:
        return

    action = run.scheduled_action
    cluster = _scheduled_action_cluster(action)
    try:
        target = _find_guest(action, client_factory=client_factory)
        preflight = _preflight_snapshot(action, target)
        _store_preflight(run, preflight)

        no_op_outcome = _no_op_outcome(action, preflight)
        if no_op_outcome:
            _finish_run(
                run,
                status=ScheduledActionRun.Status.COMPLETED,
                outcome=ScheduledActionRun.Outcome.SUCCESS_NOOP,
                action_status=ScheduledAction.LastStatus.COMPLETED,
                result={"message": no_op_outcome},
            )
            clear_live_guest_caches(cluster=cluster)
            return

        skip_reason = _skip_reason(action, preflight)
        if skip_reason:
            _finish_run(
                run,
                status=ScheduledActionRun.Status.SKIPPED,
                outcome=ScheduledActionRun.Outcome.SKIPPED,
                action_status=ScheduledAction.LastStatus.SKIPPED,
                error=skip_reason,
            )
            return

        upid = target.client.power_action(
            node=target.node,
            object_type=action.target_type,
            vmid=action.target_vmid,
            action=action.action_type,
            parameters=action.parameters,
        )
        _mark_submitted(run, target.node, upid)
        result = target.client.wait_for_task(
            node=target.node,
            upid=upid,
            timeout_seconds=action.action_timeout_seconds,
        )
    except ProxmoxTaskTimeout as exc:
        _finish_run(
            run,
            status=ScheduledActionRun.Status.TIMEOUT,
            outcome=ScheduledActionRun.Outcome.TIMEOUT,
            action_status=ScheduledAction.LastStatus.TIMEOUT,
            failure=public_failure(
                exc,
                operation="execute_scheduled_action_run",
                fallback=TASK_TIMEOUT_MESSAGE,
                code=ERROR_CODE_TASK_TIMEOUT,
            ),
        )
        return
    except ScheduledActionExecutionError as exc:
        _store_preflight(run, exc.preflight)
        _finish_run(
            run,
            status=ScheduledActionRun.Status.FAILED,
            outcome=ScheduledActionRun.Outcome.FAILURE,
            action_status=ScheduledAction.LastStatus.FAILED,
            failure=public_failure(exc, operation="execute_scheduled_action_run"),
        )
        return
    except Exception as exc:
        _finish_run(
            run,
            status=ScheduledActionRun.Status.FAILED,
            outcome=ScheduledActionRun.Outcome.FAILURE,
            action_status=ScheduledAction.LastStatus.FAILED,
            failure=public_failure(
                exc,
                operation="execute_scheduled_action_run",
                fallback=PROVIDER_FAILURE_MESSAGE if isinstance(exc, ProxmoxAPIError) else UNEXPECTED_FAILURE_MESSAGE,
                code=ERROR_CODE_PROVIDER if isinstance(exc, ProxmoxAPIError) else ERROR_CODE_UNEXPECTED,
            ),
        )
        if not isinstance(exc, ProxmoxAPIError):
            raise
        return

    if result.success:
        if _run_was_cancelled(run):
            clear_live_guest_caches(cluster=cluster)
            return
        result_details = {"proxmox_task": result.raw}
        try:
            projection = refresh_current_guest_from_client(
                target.client,
                node=target.node,
                object_type=action.target_type,
                vmid=action.target_vmid,
                cluster=cluster,
            )
            if projection.error:
                result_details["projection_refresh_error"] = projection.error
        except Exception as exc:
            logger.exception("Scheduled action succeeded but targeted projection refresh failed")
            result_details["projection_refresh_error"] = public_exception_message(
                exc,
                operation="execute_scheduled_action_run.projection_refresh",
                fallback="The action succeeded, but refreshing this guest's projection failed.",
            )
        _finish_run(
            run,
            status=ScheduledActionRun.Status.COMPLETED,
            outcome=ScheduledActionRun.Outcome.SUCCESS,
            action_status=ScheduledAction.LastStatus.COMPLETED,
            result=result_details,
        )
        clear_live_guest_caches(cluster=cluster)
    else:
        if _run_was_cancelled(run):
            clear_live_guest_caches(cluster=cluster)
            return
        _finish_run(
            run,
            status=ScheduledActionRun.Status.FAILED,
            outcome=ScheduledActionRun.Outcome.FAILURE,
            action_status=ScheduledAction.LastStatus.FAILED,
            result={"proxmox_task": result.raw},
            failure=proxmox_task_failure(result.exitstatus, result.status),
        )


def _start_run(run_id: int) -> ScheduledActionRun | None:
    now = timezone.now()
    with transaction.atomic():
        run = (
            ScheduledActionRun.objects.select_for_update().select_related("scheduled_action").filter(pk=run_id).first()
        )
        if run is None or run.status != ScheduledActionRun.Status.QUEUED:
            return None
        run.status = ScheduledActionRun.Status.PREFLIGHT
        run.started_at = now
        run.save(update_fields=["status", "started_at", "updated_at"])

    _audit_run(run, action="scheduled_action.run_started", outcome="success")
    return run


def _lookup_error(exc: ProxmoxAPIError) -> str:
    """Why one endpoint could not answer, without repeating the provider's text.

    These land in the preflight snapshot an operator reads after a failed run,
    so they follow the same boundary as every other stored failure.
    """
    return public_exception_message(
        exc,
        operation="scheduled_actions.find_guest",
        fallback=PROVIDER_FAILURE_MESSAGE,
    )


def _find_guest(
    action: ScheduledAction,
    *,
    client_factory: Callable[[str], ProxmoxClient] | None,
) -> GuestTarget:
    errors: list[dict[str, Any]] = []
    cluster = _scheduled_action_cluster(action)
    endpoints = list(ProxmoxEndpoint.objects.filter(cluster=cluster, enabled=True).order_by("name"))
    clients = (
        [client_factory(endpoint.url) for endpoint in endpoints]
        if client_factory is not None
        else cluster_clients(cluster)
    )
    for endpoint, client in zip(endpoints, clients, strict=True):
        try:
            node_names = client.node_names(fallback=endpoint.name)
        except ProxmoxAPIError as exc:
            errors.append({"endpoint": endpoint.name, "error": _lookup_error(exc)})
            continue

        for node in node_names:
            try:
                current = client.guest_current(
                    node=node,
                    object_type=action.target_type,
                    vmid=action.target_vmid,
                )
                config = client.guest_config(
                    node=node,
                    object_type=action.target_type,
                    vmid=action.target_vmid,
                )
            except ProxmoxAPIError as exc:
                errors.append({"endpoint": endpoint.name, "node": node, "error": _lookup_error(exc)})
                continue
            return GuestTarget(endpoint=endpoint, client=client, node=node, current=current, config=config)

    raise ScheduledActionExecutionError(
        f"Could not find {action.target_type} {action.target_vmid} in cluster '{cluster.key}'.",
        preflight={"lookup_errors": errors, "endpoint_count": len(endpoints), "cluster_key": cluster.key},
    )


def _scheduled_action_cluster(action: ScheduledAction):
    """Return the durable cluster relation; executable rows must be qualified."""
    if action.cluster_id is not None:
        return action.cluster
    raise ScheduledActionExecutionError(
        "Scheduled action has no cluster identity and cannot run safely.",
        preflight={"target_type": action.target_type, "target_vmid": action.target_vmid},
    )


def _preflight_snapshot(action: ScheduledAction, target: GuestTarget) -> dict[str, Any]:
    status = str(target.current.get("status") or "")
    lock = target.config.get("lock") or target.current.get("lock") or ""
    return {
        "guest_ref": action.guest_ref().serialize() if action.guest_ref() else "",
        "cluster_key": _scheduled_action_cluster(action).key,
        "endpoint": target.endpoint.name,
        "node": target.node,
        "target_type": action.target_type,
        "target_vmid": action.target_vmid,
        "target_name": target.current.get("name") or target.config.get("name") or target.config.get("hostname") or "",
        "action": action.action_type,
        "status": status,
        "lock": str(lock),
    }


def _no_op_outcome(action: ScheduledAction, preflight: dict[str, Any]) -> str:
    status = str(preflight.get("status") or "")
    if action.action_type == ScheduledAction.ActionType.START and status == "running":
        return "Guest is already running."
    if (
        action.action_type in {ScheduledAction.ActionType.SHUTDOWN, ScheduledAction.ActionType.STOP}
        and status == "stopped"
    ):
        return "Guest is already stopped."
    return ""


def _skip_reason(action: ScheduledAction, preflight: dict[str, Any]) -> str:
    lock = str(preflight.get("lock") or "")
    if lock:
        return f"Guest is locked by Proxmox operation: {lock}."
    status = str(preflight.get("status") or "")
    if action.action_type == ScheduledAction.ActionType.REBOOT and status == "stopped":
        return "Cannot reboot a stopped guest."
    return ""


def _store_preflight(run: ScheduledActionRun, preflight: dict[str, Any]) -> None:
    if not preflight:
        return
    node = str(preflight.get("node") or "")
    ScheduledActionRun.objects.filter(pk=run.pk).update(
        preflight_snapshot=preflight,
        proxmox_task_node=node,
        updated_at=timezone.now(),
    )
    run.preflight_snapshot = preflight
    run.proxmox_task_node = node


def _mark_submitted(run: ScheduledActionRun, node: str, upid: str) -> None:
    now = timezone.now()
    ScheduledActionRun.objects.filter(pk=run.pk).update(
        status=ScheduledActionRun.Status.POLLING,
        proxmox_task_node=node,
        proxmox_task_upid=upid,
        updated_at=now,
    )
    run.status = ScheduledActionRun.Status.POLLING
    run.proxmox_task_node = node
    run.proxmox_task_upid = upid


def _finish_run(
    run: ScheduledActionRun,
    *,
    status: str,
    outcome: str,
    action_status: str,
    error: str = "",
    failure: PublicFailure | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    """Close a run row and audit it.

    `failure` is the boundary-checked form and carries a decision code; `error`
    remains for the outcomes whose text is a plain statement of what happened
    (stale, skipped) and never came from an exception.
    """
    if failure is not None:
        error = failure.message
    error_code = failure.code if failure is not None else ""
    now = timezone.now()
    with transaction.atomic():
        ScheduledActionRun.objects.filter(pk=run.pk).update(
            status=status,
            outcome=outcome,
            error=error,
            result=result or {},
            finished_at=now,
            updated_at=now,
        )
        ScheduledAction.objects.filter(pk=run.scheduled_action_id).update(
            last_status=action_status,
            last_run_at=now,
            updated_at=now,
        )

    audit_action = {
        ScheduledActionRun.Status.COMPLETED: "scheduled_action.run_completed",
        ScheduledActionRun.Status.FAILED: "scheduled_action.run_failed",
        ScheduledActionRun.Status.SKIPPED: "scheduled_action.run_skipped",
        ScheduledActionRun.Status.TIMEOUT: "scheduled_action.run_failed",
        ScheduledActionRun.Status.STALE: "scheduled_action.run_failed",
        ScheduledActionRun.Status.CANCELLED: "scheduled_action.run_cancelled",
    }.get(status, "scheduled_action.run_completed")
    audit_outcome = (
        "success"
        if outcome in {ScheduledActionRun.Outcome.SUCCESS, ScheduledActionRun.Outcome.SUCCESS_NOOP}
        else outcome
    )
    _audit_run(
        run,
        action=audit_action,
        outcome=audit_outcome,
        details={
            "status": status,
            "outcome": outcome,
            "error": error,
            "error_code": error_code,
            "result": result or {},
        },
    )


def _advance_action_after_claim(action: ScheduledAction, now, status: str) -> str:
    error = ""
    if action.schedule_type == ScheduledAction.ScheduleType.RECURRING:
        try:
            action.next_run_at = next_run_after(action, after=now)
        except RecurrenceError as exc:
            action.next_run_at = None
            action.enabled = False
            status = ScheduledAction.LastStatus.FAILED
            error = public_failure(exc, operation="advance_scheduled_action").message
        else:
            action.enabled = action.next_run_at is not None
    else:
        action.enabled = False
        action.next_run_at = None
    action.last_run_at = now
    action.last_status = status
    action.save(update_fields=["enabled", "next_run_at", "last_run_at", "last_status", "updated_at"])
    return error


def _run_was_cancelled(run: ScheduledActionRun) -> bool:
    return ScheduledActionRun.objects.filter(pk=run.pk, status=ScheduledActionRun.Status.CANCELLED).exists()


def _has_in_flight_run(action: ScheduledAction) -> bool:
    return action.runs.filter(status__in=IN_FLIGHT_RUN_STATUSES).exists()


def _is_missed(action: ScheduledAction, planned_for, now) -> bool:
    if planned_for >= now:
        return False
    lateness = now - planned_for
    if action.catch_up_policy == ScheduledAction.CatchUpPolicy.RUN_ONCE_LATE:
        allowed_lateness = max(timedelta(minutes=action.max_lateness_minutes), DISPATCH_GRACE)
    else:
        allowed_lateness = DISPATCH_GRACE
    return lateness > allowed_lateness


def _occurrence_key(planned_for) -> str:
    return planned_for.astimezone(timezone.UTC).isoformat().replace("+00:00", "Z")


def _manual_occurrence_key(action: ScheduledAction, planned_for, triggered_by) -> str:
    username = triggered_by.get_username() if getattr(triggered_by, "is_authenticated", False) else "system"
    key = f"manual:{action.id}:{_occurrence_key(planned_for)}:{username}"
    return key[:160]


def _audit_run(
    run: ScheduledActionRun,
    *,
    action: str,
    outcome: str,
    details: dict[str, Any] | None = None,
) -> None:
    scheduled_action = run.scheduled_action
    cluster = _scheduled_action_cluster(scheduled_action)
    ref = scheduled_action.guest_ref()
    if ref is None:
        from core.services.refs import GuestRef

        ref = GuestRef(
            cluster.key,
            scheduled_action.target_type,
            scheduled_action.target_vmid,
            scheduled_action.target_node,
        )
    user = run.triggered_by if run.triggered_by_id else None
    record_audit_event(
        user=user,
        username=user.get_username() if user else "system",
        action=action,
        object_type="scheduled_action",
        object_id=str(scheduled_action.id),
        outcome=outcome,
        cluster=cluster,
        guest_ref=ref,
        details={
            "scheduled_action_id": scheduled_action.id,
            "scheduled_action_name": scheduled_action.name,
            "run_id": run.id,
            "target_type": scheduled_action.target_type,
            "target_vmid": scheduled_action.target_vmid,
            "target_node": scheduled_action.target_node,
            "action_type": scheduled_action.action_type,
            "planned_for": run.planned_for.isoformat(),
            "proxmox_task_upid": run.proxmox_task_upid,
            **(details or {}),
        },
    )

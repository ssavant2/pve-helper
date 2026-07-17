from __future__ import annotations

from django.db import transaction

from .common import *  # noqa: F401,F403
from . import common
from ..services.proxmox import ProxmoxClient
from ..services.public_errors import public_exception_message
from ..services.scheduled_actions import IN_FLIGHT_RUN_STATUSES
from ..services.tag_actions import TagOperationQueueError, TagOperationRetryError, retry_tag_operation


@app_login_required
def scheduled_tasks(request):
    target_filter = request.GET.get("target", "")
    target_type, target_vmid = _parse_scheduled_target(target_filter)
    target_filter_value = f"{target_type}:{target_vmid}" if target_type and target_vmid else ""

    actions_query = ScheduledAction.objects.select_related("created_by").filter(deleted_at__isnull=True)
    if target_filter_value:
        actions_query = actions_query.filter(target_type=target_type, target_vmid=target_vmid)

    actions = list(actions_query.order_by("-enabled", "next_run_at", "name"))
    latest_runs = _latest_scheduled_runs(target_type=target_type, target_vmid=target_vmid)

    for action in actions:
        action.display_target = _scheduled_action_target_label(action)
        action.guest_identity = guest_identity_from_scheduled_action(action)
        action.display_schedule = _scheduled_action_schedule_label(action)
        action.display_status_class = _scheduled_action_status_class(action.last_status)
        action.display_creator = action.created_by.get_username() if action.created_by else "system"

    scheduled_runs_url = reverse("core:scheduled_task_runs")
    if target_filter_value:
        scheduled_runs_url = f"{scheduled_runs_url}?{urlencode({'target': target_filter_value})}"

    context = {
        **navigation_context("scheduled_tasks"),
        "scheduled_actions": actions,
        "latest_runs": latest_runs,
        "scheduled_actions_enabled": settings.SCHEDULED_ACTIONS_ENABLED,
        "schedule_timezone": settings.TIME_ZONE,
        "run_retention_days": settings.SCHEDULED_ACTION_RUN_RETENTION_DAYS,
        "target_filter": target_filter_value,
        "target_filter_label": _scheduled_target_label(target_type, target_vmid) if target_filter_value else "",
        "scheduled_task_create_query": urlencode({"target": target_filter_value}) if target_filter_value else "",
        "scheduled_runs_url": scheduled_runs_url,
    }
    return render(request, "core/scheduled_tasks.html", context)


@app_login_required
def scheduled_task_runs(request):
    target_filter = request.GET.get("target", "")
    target_type, target_vmid = _parse_scheduled_target(target_filter)
    return JsonResponse(
        {
            "runs": [
                _serialize_scheduled_run(run)
                for run in _latest_scheduled_runs(target_type=target_type, target_vmid=target_vmid, limit=10)
            ]
        }
    )


@app_login_required
def scheduled_task_create(request):
    action = ScheduledAction()
    if request.method == "POST":
        errors = _apply_scheduled_action_form(action, request.POST, request.user)
        if not errors:
            _audit_scheduled_action_definition(request, "scheduled_action.created", action)
            return redirect("core:scheduled_tasks")
    else:
        target_type, target_vmid = _parse_scheduled_target(request.GET.get("target", ""))
        if target_type and target_vmid:
            action.target_type = target_type
            action.target_vmid = target_vmid
            _apply_target_snapshot(action)
        errors = []

    context = _scheduled_action_form_context(
        action,
        form_values=_scheduled_action_form_values(action, request.POST if request.method == "POST" else None),
        errors=errors,
        mode="create",
    )
    return render(request, "core/scheduled_task_form.html", context, status=400 if errors else 200)


@app_login_required
def scheduled_task_edit(request, action_id: int):
    action = get_object_or_404(ScheduledAction, pk=action_id, deleted_at__isnull=True)
    if request.method == "POST":
        errors = _apply_scheduled_action_form(action, request.POST, request.user)
        if not errors:
            _audit_scheduled_action_definition(request, "scheduled_action.updated", action)
            return redirect("core:scheduled_tasks")
    else:
        errors = []

    context = _scheduled_action_form_context(
        action,
        form_values=_scheduled_action_form_values(action, request.POST if request.method == "POST" else None),
        errors=errors,
        mode="edit",
    )
    return render(request, "core/scheduled_task_form.html", context, status=400 if errors else 200)


@require_POST
@app_login_required
def scheduled_task_toggle(request, action_id: int):
    action = get_object_or_404(ScheduledAction, pk=action_id, deleted_at__isnull=True)
    enabled = request.POST.get("enabled") == "1"
    if enabled:
        try:
            _refresh_scheduled_action_next_run(action)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("core:scheduled_tasks")
    action.enabled = enabled
    action.save(update_fields=["enabled", "next_run_at", "updated_at"])
    _audit_scheduled_action_definition(
        request,
        "scheduled_action.enabled" if enabled else "scheduled_action.disabled",
        action,
    )
    return redirect("core:scheduled_tasks")


@require_POST
@app_login_required
def scheduled_task_delete(request, action_id: int):
    with transaction.atomic():
        action = get_object_or_404(
            ScheduledAction.objects.select_for_update(),
            pk=action_id,
            deleted_at__isnull=True,
        )
        if action.runs.filter(status__in=IN_FLIGHT_RUN_STATUSES).exists():
            messages.error(request, "This scheduled task has a run in progress and cannot be deleted yet.")
            return redirect("core:scheduled_tasks")
        action.enabled = False
        action.next_run_at = None
        action.deleted_at = tz.now()
        action.save(update_fields=["enabled", "next_run_at", "deleted_at", "updated_at"])
    _audit_scheduled_action_definition(request, "scheduled_action.deleted", action)
    messages.success(request, "Scheduled task removed. Completed run history is retained.")
    return redirect("core:scheduled_tasks")


@require_POST
@app_login_required
def scheduled_task_run_now(request, action_id: int):
    action = get_object_or_404(ScheduledAction, pk=action_id, deleted_at__isnull=True)
    try:
        queue_manual_scheduled_action_run(action, triggered_by=request.user)
    except ScheduledActionQueueError as exc:
        messages.error(request, str(exc))
    return redirect("core:scheduled_tasks")


@app_login_required
def recent_tasks(request):
    try:
        page = int(request.GET.get("page", "0"))
    except ValueError:
        page = 0

    return JsonResponse(serialize_task_page(recent_task_page(page=page)))


@require_POST
@app_login_required
def cancel_recent_task(request):
    task_id = request.POST.get("task_id", "").strip()
    if not task_id:
        return JsonResponse({"ok": False, "error": "Missing task id."}, status=400)

    try:
        if task_id.startswith("guest:"):
            _cancel_guest_recent_task(request, int(task_id.split(":", 1)[1]))
        elif task_id.startswith("scheduled_action:"):
            _cancel_scheduled_action_recent_task(request, int(task_id.split(":", 1)[1]))
        else:
            return JsonResponse({"ok": False, "error": "This task type cannot be cancelled."}, status=409)
    except ValueError:
        return JsonResponse({"ok": False, "error": "Invalid task id."}, status=400)
    except Http404:
        return JsonResponse({"ok": False, "error": "Task not found."}, status=404)
    except ProxmoxAPIError as exc:
        error = public_exception_message(
            exc,
            operation="recent_task_cancel",
            fallback="Proxmox could not cancel the task.",
        )
        return JsonResponse(
            {"ok": False, "error": error},
            status=409,
        )

    return JsonResponse({"ok": True})


@require_POST
@app_login_required
def retry_recent_task(request):
    task_id = request.POST.get("task_id", "").strip()
    if not task_id.startswith("guest:"):
        return JsonResponse({"ok": False, "error": "This task type cannot be retried."}, status=409)
    try:
        event_id = int(task_id.split(":", 1)[1])
        queued_task_id = retry_tag_operation(event_id)
    except ValueError:
        return JsonResponse({"ok": False, "error": "Invalid task id."}, status=400)
    except TagOperationRetryError:
        return JsonResponse(
            {"ok": False, "error": "This tag operation is not available for retry."},
            status=409,
        )
    except TagOperationQueueError:
        return JsonResponse(
            {"ok": False, "error": "The tag operation could not be queued; retry is safe."},
            status=503,
        )
    return JsonResponse({"ok": True, "queued_task_id": queued_task_id}, status=202)


@require_POST
@app_login_required
def dismiss_task_question(request):
    """Mark a task's actionable question (e.g. a timed-out shutdown's force-stop
    offer) as answered so it stops pulsing/pinning — the user either acted on it
    or actively chose to ignore it."""
    task_id = request.POST.get("task_id", "").strip()
    if not task_id.startswith("guest:"):
        return JsonResponse({"ok": False, "error": "Unsupported task."}, status=400)
    try:
        event_id = int(task_id.split(":", 1)[1])
    except ValueError:
        return JsonResponse({"ok": False, "error": "Invalid task id."}, status=400)
    event = AuditEvent.objects.filter(pk=event_id, action="guest.power.shutdown").first()
    if event is None:
        return JsonResponse({"ok": False, "error": "Task not found."}, status=404)
    details = dict(event.details) if isinstance(event.details, dict) else {}
    details["force_stop_dismissed"] = True
    event.details = details
    event.save(update_fields=["details"])
    return JsonResponse({"ok": True})


def _cancel_guest_recent_task(request, event_id: int) -> None:
    event = get_object_or_404(AuditEvent, pk=event_id, action__startswith="guest.")
    details = dict(event.details) if isinstance(event.details, dict) else {}
    if event.outcome not in {"queued", "running"}:
        raise ProxmoxAPIError("Only queued or running tasks can be cancelled.")
    upid = str(details.get("proxmox_task_upid") or "")
    node = str(details.get("proxmox_task_node") or details.get("node") or "")
    if not upid or not node:
        raise ProxmoxAPIError("This task has no Proxmox task id to cancel.")

    _cancel_proxmox_task(node=node, upid=upid, endpoint_url=str(details.get("proxmox_endpoint") or ""))
    now = tz.now()
    username = request.user.get_username()
    details.update(
        {
            "cancelled_at": now.isoformat(),
            "cancelled_by": username,
            "finished_at": now.isoformat(),
            "error": f"Cancelled by {username}.",
        }
    )
    event.outcome = "cancelled"
    event.details = details
    event.save(update_fields=["outcome", "details"])
    record_audit_event(
        request,
        action="task.cancelled",
        object_type="recent_task",
        object_id=f"guest:{event.id}",
        details={"task_id": f"guest:{event.id}", "proxmox_task_upid": upid, "proxmox_task_node": node},
    )
    clear_live_guest_caches()


def _cancel_scheduled_action_recent_task(request, run_id: int) -> None:
    run = get_object_or_404(ScheduledActionRun.objects.select_related("scheduled_action"), pk=run_id)
    if run.status not in {
        ScheduledActionRun.Status.SUBMITTED,
        ScheduledActionRun.Status.POLLING,
    }:
        raise ProxmoxAPIError("Only scheduled tasks already submitted to Proxmox can be cancelled.")
    if not run.proxmox_task_upid or not run.proxmox_task_node:
        raise ProxmoxAPIError("This task has no Proxmox task id to cancel.")

    _cancel_proxmox_task(node=run.proxmox_task_node, upid=run.proxmox_task_upid)
    now = tz.now()
    username = request.user.get_username()
    run.status = ScheduledActionRun.Status.CANCELLED
    run.outcome = ScheduledActionRun.Outcome.CANCELLED
    run.error = f"Cancelled by {username}."
    run.finished_at = now
    run.result = {"cancelled": True, "cancelled_by": username, "cancelled_at": now.isoformat()}
    run.save(update_fields=["status", "outcome", "error", "finished_at", "result", "updated_at"])
    ScheduledAction.objects.filter(pk=run.scheduled_action_id).update(
        last_status=ScheduledAction.LastStatus.CANCELLED,
        last_run_at=now,
        updated_at=now,
    )
    record_audit_event(
        request,
        action="scheduled_action.run_cancelled",
        object_type="scheduled_action",
        object_id=str(run.scheduled_action_id),
        outcome="cancelled",
        details={
            "scheduled_action_id": run.scheduled_action_id,
            "scheduled_action_name": run.scheduled_action.name,
            "run_id": run.id,
            "target_type": run.scheduled_action.target_type,
            "target_vmid": run.scheduled_action.target_vmid,
            "target_node": run.scheduled_action.target_node,
            "action_type": run.scheduled_action.action_type,
            "planned_for": run.planned_for.isoformat(),
            "proxmox_task_upid": run.proxmox_task_upid,
            "proxmox_task_node": run.proxmox_task_node,
            "cancelled_by": username,
        },
    )
    clear_live_guest_caches()


def _cancel_proxmox_task(*, node: str, upid: str, endpoint_url: str = "") -> None:
    """Stop a Proxmox task, preferring the endpoint that submitted it.

    The recorded endpoint is tried first because it is the one that owns the task.
    The remaining candidates are bounded to a single cluster: a UPID names a node,
    and two clusters may each have a node called pve1, so an unscoped fallback
    could stop the wrong cluster's task.
    """
    from ..views.common import cluster_scoped_clients

    errors: list[str] = []
    clients = [ProxmoxClient(endpoint_url)] if endpoint_url else []
    clients.extend(cluster_scoped_clients())
    seen: set[str] = set()
    for client in clients:
        endpoint = getattr(client, "endpoint", "")
        if endpoint in seen:
            continue
        seen.add(endpoint)
        try:
            client.stop_task(node=node, upid=upid)
            return
        except ProxmoxAPIError as exc:
            errors.append(str(exc))
    raise ProxmoxAPIError("; ".join(errors) or "Could not cancel the Proxmox task.")


@app_login_required
def scan_status(request):
    active_scan = _active_scan()
    latest_scan = active_scan or ScanRun.objects.order_by("-created_at").first()
    return JsonResponse(
        {
            "active": active_scan is not None,
            "status": latest_scan.status if latest_scan else "",
            "status_label": latest_scan.get_status_display() if latest_scan else "",
            "button_label": _scan_button_label(active_scan),
            "progress": latest_scan.progress_message if latest_scan else "",
        }
    )


@require_POST
@app_login_required
def update_scan_schedule_view(request):
    enabled = request.POST.get("enabled") == "on"
    try:
        interval_minutes = int(request.POST.get("interval_minutes", "60"))
        state = update_scan_schedule(enabled=enabled, interval_minutes=interval_minutes)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("core:dashboard")

    record_audit_event(
        request,
        action="scan.schedule.updated",
        object_type="scan_schedule",
        object_id="automatic-storage-scan",
        details={
            "enabled": state.enabled,
            "interval_minutes": state.interval_minutes,
            "next_run": state.next_run.isoformat() if state.next_run else "",
        },
    )

    return redirect("core:dashboard")


@require_POST
@app_login_required
def start_scan(request):
    redirect_to = _safe_next_url(request)
    active_scan = _active_scan()
    if active_scan:
        record_audit_event(
            request,
            action="scan.manual.skipped",
            object_type="scan_run",
            object_id=str(active_scan.id),
            outcome="skipped",
            details={"reason": "A scan is already queued or running."},
        )
        return redirect(redirect_to)

    target_storage = _requested_scan_storage(request)
    scan = ScanRun.objects.create(
        progress_message="Queued from UI",
        target_storage=target_storage,
        target_label=target_storage.display_name if target_storage else "",
    )
    task_id = common.enqueue_bulk_task("core.tasks.run_scan", scan.id)
    scan.queued_task_id = task_id
    scan.save(update_fields=["queued_task_id", "updated_at"])

    record_audit_event(
        request,
        action="scan.queued",
        object_type="scan_run",
        object_id=str(scan.id),
        details={
            "task_id": task_id,
            "target_storage": target_storage.storage_id if target_storage else "",
            "target_label": target_storage.display_name if target_storage else "All storages",
        },
    )
    return redirect(redirect_to)


def _scan_button_label(active_scan: ScanRun | None) -> str:
    if not active_scan:
        return "Start scan"
    if active_scan.status == ScanRun.Status.QUEUED:
        return "Scan queued"
    return "Scanning"


def _requested_scan_storage(request) -> StorageMount | None:
    storage_id = request.POST.get("storage_id", "").strip()
    if not storage_id:
        return None
    return get_object_or_404(StorageMount, storage_id=storage_id, enabled=True)


def _scheduled_action_form_context(action: ScheduledAction, *, form_values: dict, errors: list[str], mode: str) -> dict:
    target_choices = _scheduled_action_target_choices(action)
    return {
        **navigation_context("scheduled_tasks"),
        "scheduled_action": action,
        "form_values": form_values,
        "form_errors": errors,
        "form_mode": mode,
        "target_choices": target_choices,
        "action_type_choices": ScheduledAction.ActionType.choices,
        "recurrence_kind_choices": [
            (SCHEDULED_ACTION_RECURRENCE_ONCE, "Once"),
            (ScheduledAction.RecurrenceKind.DAILY, "Daily"),
            (ScheduledAction.RecurrenceKind.WEEKLY, "Weekly"),
            (ScheduledAction.RecurrenceKind.MONTHLY_ORDINAL, "Monthly by weekday"),
            (ScheduledAction.RecurrenceKind.MONTHLY_DAY, "Monthly by date"),
        ],
        "weekday_choices": SCHEDULED_ACTION_WEEKDAYS,
        "ordinal_choices": SCHEDULED_ACTION_ORDINALS,
        "month_choices": SCHEDULED_ACTION_MONTHS,
        "scheduled_actions_enabled": settings.SCHEDULED_ACTIONS_ENABLED,
        "schedule_timezone": settings.TIME_ZONE,
    }


def _scheduled_action_form_values(action: ScheduledAction, post=None) -> dict:
    if post is not None:
        return {
            "name": post.get("name", ""),
            "enabled": post.get("enabled") == "on",
            "action_type": post.get("action_type", ScheduledAction.ActionType.SHUTDOWN),
            "target": post.get("target", ""),
            **_posted_datetime_parts(post, "run"),
            "recurrence_kind": post.get("recurrence_kind", SCHEDULED_ACTION_RECURRENCE_ONCE),
            "weekdays": _posted_values(post, "weekdays", fallback_names=["weekday"], default=["6"]),
            "ordinals": _posted_values(post, "ordinals", fallback_names=["ordinal"], default=["first"]),
            "days_of_month": post.get("days_of_month", post.get("day_of_month", "1")),
            "months": _posted_values(post, "months", default=SCHEDULED_ACTION_DEFAULT_MONTHS),
            "catch_up_enabled": post.get("catch_up_enabled") == "on",
            "max_lateness_hours": post.get("max_lateness_hours", "1"),
            "action_timeout_seconds": post.get("action_timeout_seconds", "1800"),
        }

    recurrence = action.recurrence if isinstance(action.recurrence, dict) else {}
    target = f"{action.target_type}:{action.target_vmid}" if action.target_type and action.target_vmid else ""
    run_at = None
    if action.schedule_type == ScheduledAction.ScheduleType.ONCE:
        run_at = action.run_at or action.next_run_at
    run_date, run_hour, run_minute = _datetime_parts(run_at)
    if action.schedule_type == ScheduledAction.ScheduleType.RECURRING:
        run_hour, run_minute = _time_parts(_recurrence_time_label(recurrence) if action.pk else "22:00")
    recurrence_kind = (
        SCHEDULED_ACTION_RECURRENCE_ONCE
        if action.schedule_type != ScheduledAction.ScheduleType.RECURRING
        else action.recurrence_kind or ScheduledAction.RecurrenceKind.DAILY
    )
    return {
        "name": action.name or "",
        "enabled": action.enabled if action.pk else True,
        "action_type": action.action_type or ScheduledAction.ActionType.SHUTDOWN,
        "target": target,
        "run_date": run_date,
        "run_hour": run_hour,
        "run_minute": run_minute,
        "recurrence_kind": recurrence_kind,
        "weekdays": _recurrence_values(recurrence, "weekdays", "weekday", default=["6"]),
        "ordinals": _recurrence_values(recurrence, "ordinals", "ordinal", "week", default=["first"]),
        "days_of_month": _recurrence_days_label(recurrence),
        "months": _recurrence_values(recurrence, "months", default=SCHEDULED_ACTION_DEFAULT_MONTHS),
        "catch_up_enabled": action.catch_up_policy == ScheduledAction.CatchUpPolicy.RUN_ONCE_LATE,
        "max_lateness_hours": str(max(1, action.max_lateness_minutes // 60) if action.max_lateness_minutes else 1),
        "action_timeout_seconds": str(action.action_timeout_seconds or 1800),
    }


def _scheduled_action_target_choices(action: ScheduledAction | None = None) -> list[dict]:
    choices = []
    seen = set()
    for guest in common.fetch_live_guest_inventory(use_cache=False):
        key = (guest.object_type, guest.vmid)
        if key in seen:
            continue
        seen.add(key)
        choices.append(
            {
                "value": f"{guest.object_type}:{guest.vmid}",
                "label": _live_guest_target_label(guest),
                "node": guest.node,
            }
        )

    for obj in CurrentGuestInventory.objects.order_by("object_type", "vmid", "node"):
        key = (obj.object_type, obj.vmid)
        if key in seen:
            continue
        seen.add(key)
        choices.append(
            {
                "value": f"{obj.object_type}:{obj.vmid}",
                "label": _inventory_target_label(obj),
                "node": obj.node,
            }
        )

    if action and action.target_type and action.target_vmid:
        value = f"{action.target_type}:{action.target_vmid}"
        if (action.target_type, action.target_vmid) not in seen:
            choices.append(
                {
                    "value": value,
                    "label": _scheduled_action_target_label(action),
                    "node": action.target_node,
                }
            )
    return choices


def _inventory_target_label(obj) -> str:
    return guest_identity_from_inventory(obj).full_label_with_type


def _live_guest_target_label(guest) -> str:
    return guest_identity_from_inventory(guest).full_label_with_type


def _scheduled_target_label(target_type: str | None, target_vmid: int | None) -> str:
    if target_type is None or target_vmid is None:
        return ""
    obj = (
        CurrentGuestInventory.objects.filter(object_type=target_type, vmid=target_vmid).first()
    )
    if obj:
        return _inventory_target_label(obj)
    type_label = "VM" if target_type == ScheduledAction.TargetType.VM else "Container"
    return f"{type_label} {target_vmid}"


def _live_guest_for_target(
    target_type: str | None,
    target_vmid: int | None,
    *,
    use_cache: bool,
):
    if target_type is None or target_vmid is None:
        return None
    for guest in common.fetch_live_guest_inventory(use_cache=use_cache):
        if guest.object_type == target_type and guest.vmid == target_vmid:
            return guest
    return None


def _apply_scheduled_action_form(action: ScheduledAction, post, user) -> list[str]:
    errors: list[str] = []
    # Truncate to the model limit before the uniqueness check so two names that
    # only collide after truncation surface as a form error, not an IntegrityError.
    name = post.get("name", "").strip()[:160]
    if not name:
        errors.append("Name is required.")
    elif ScheduledAction.objects.filter(name=name, deleted_at__isnull=True).exclude(pk=action.pk).exists():
        errors.append("A scheduled task with this name already exists.")
    target_type, target_vmid = _parse_scheduled_target(post.get("target", ""))
    if target_type is None or target_vmid is None:
        errors.append("Target is required.")

    action_type = post.get("action_type", "")
    if action_type not in ScheduledAction.ActionType.values:
        errors.append("Unknown action.")

    try:
        timeout_seconds = _bounded_int(post.get("action_timeout_seconds", "1800"), 30, 86400, "Timeout")
    except ValueError as exc:
        errors.append(str(exc))
        timeout_seconds = 1800

    selected_recurrence = post.get("recurrence_kind", SCHEDULED_ACTION_RECURRENCE_ONCE)
    catch_up_enabled = post.get("catch_up_enabled") == "on" and selected_recurrence != SCHEDULED_ACTION_RECURRENCE_ONCE
    try:
        max_lateness_hours = _bounded_int(post.get("max_lateness_hours", "1"), 1, 24, "Retry window")
    except ValueError as exc:
        errors.append(str(exc))
        max_lateness_hours = 1

    run_at = None
    recurrence = {}
    if selected_recurrence == SCHEDULED_ACTION_RECURRENCE_ONCE:
        schedule_type = ScheduledAction.ScheduleType.ONCE
        try:
            run_at = _parse_local_datetime_from_post(post)
        except ValueError as exc:
            errors.append(str(exc))
        recurrence_kind = ScheduledAction.RecurrenceKind.ADVANCED
    else:
        schedule_type = ScheduledAction.ScheduleType.RECURRING
        recurrence_kind = selected_recurrence
        if recurrence_kind not in ScheduledAction.RecurrenceKind.values:
            errors.append("Unknown recurrence type.")
        try:
            recurrence = _recurrence_from_post(post, recurrence_kind)
        except ValueError as exc:
            errors.append(str(exc))

    if errors:
        return errors

    action.name = name
    action.enabled = post.get("enabled") == "on"
    action.action_type = action_type
    action.action_timeout_seconds = timeout_seconds
    action.target_type = target_type
    action.target_vmid = target_vmid
    _apply_target_snapshot(action)
    action.schedule_type = schedule_type
    action.run_at = run_at
    action.recurrence = recurrence
    action.recurrence_kind = recurrence_kind
    action.timezone = settings.TIME_ZONE
    action.catch_up_policy = (
        ScheduledAction.CatchUpPolicy.RUN_ONCE_LATE
        if catch_up_enabled
        else ScheduledAction.CatchUpPolicy.SKIP_MISSED
    )
    action.max_lateness_minutes = max_lateness_hours * 60 if catch_up_enabled else 0
    action.parameters = {}
    if not action.pk:
        action.created_by = user if user.is_authenticated else None
        action.last_status = ScheduledAction.LastStatus.NEVER_RUN

    try:
        _refresh_scheduled_action_next_run(action)
    except ValueError as exc:
        return [str(exc)]

    action.save()
    return []


def _parse_scheduled_target(value: str) -> tuple[str | None, int | None]:
    if ":" not in value:
        return None, None
    target_type, raw_vmid = value.split(":", 1)
    if target_type not in ScheduledAction.TargetType.values:
        return None, None
    try:
        vmid = int(raw_vmid)
    except ValueError:
        return None, None
    return target_type, vmid if vmid > 0 else None


def _apply_target_snapshot(action: ScheduledAction) -> None:
    action.target_node = ""
    action.target_name_snapshot = ""
    live_guest = _live_guest_for_target(action.target_type, action.target_vmid, use_cache=False)
    if live_guest:
        action.target_node = live_guest.node
        action.target_name_snapshot = live_guest.name
        return

    obj = CurrentGuestInventory.objects.filter(
        object_type=action.target_type,
        vmid=action.target_vmid,
    ).first()
    if obj:
        action.target_node = obj.node
        action.target_name_snapshot = obj.name


def _recurrence_from_post(post, recurrence_kind: str) -> dict:
    time_value = _time_value_from_post(post, "run", fallback_name="recurrence_time", label="Run time")
    months = _choice_list_from_post(
        post,
        "months",
        valid_values={value for value, _label in SCHEDULED_ACTION_MONTHS},
        label="Month",
        default=SCHEDULED_ACTION_DEFAULT_MONTHS,
    )
    month_filter = _month_filter(months)
    if recurrence_kind == ScheduledAction.RecurrenceKind.DAILY:
        return {"time": time_value, **month_filter}
    if recurrence_kind == ScheduledAction.RecurrenceKind.WEEKLY:
        weekdays = _choice_list_from_post(
            post,
            "weekdays",
            fallback_names=["weekday"],
            valid_values={value for value, _label in SCHEDULED_ACTION_WEEKDAYS},
            label="Weekday",
        )
        return {"weekdays": weekdays, "time": time_value, **month_filter}
    if recurrence_kind == ScheduledAction.RecurrenceKind.MONTHLY_ORDINAL:
        ordinals = _choice_list_from_post(
            post,
            "ordinals",
            fallback_names=["ordinal"],
            valid_values={value for value, _label in SCHEDULED_ACTION_ORDINALS},
            label="Week",
        )
        weekdays = _choice_list_from_post(
            post,
            "weekdays",
            fallback_names=["weekday"],
            valid_values={value for value, _label in SCHEDULED_ACTION_WEEKDAYS},
            label="Weekday",
        )
        return {
            "ordinals": ordinals,
            "weekdays": weekdays,
            "time": time_value,
            **month_filter,
        }
    if recurrence_kind == ScheduledAction.RecurrenceKind.MONTHLY_DAY:
        return {
            "days_of_month": _days_of_month_from_post(post),
            "time": time_value,
            **month_filter,
        }
    if recurrence_kind == ScheduledAction.RecurrenceKind.ADVANCED:
        return {"rrule": post.get("rrule", "").strip()}
    return {}


def _month_filter(months: list[str]) -> dict:
    if months == SCHEDULED_ACTION_DEFAULT_MONTHS:
        return {}
    return {"months": months}


def _choice_list_from_post(
    post,
    name: str,
    *,
    valid_values: set[str],
    label: str,
    fallback_names: list[str] | None = None,
    default: list[str] | None = None,
) -> list[str]:
    values = _posted_values(post, name, fallback_names=fallback_names)
    if not values and post.get(f"{name}_present") != "1":
        values = list(default or [])
    if not values:
        raise ValueError(f"Select at least one {label.lower()}.")
    invalid = [value for value in values if value not in valid_values]
    if invalid:
        raise ValueError(f"Unknown {label.lower()}: {', '.join(invalid)}.")
    return values


def _days_of_month_from_post(post) -> list[int]:
    raw_value = post.get("days_of_month", post.get("day_of_month", "")).strip()
    days = []
    for part in raw_value.split(","):
        value = part.strip()
        if not value:
            continue
        try:
            day = int(value)
        except ValueError as exc:
            raise ValueError("Days of month must be comma-separated numbers.") from exc
        if day < 1 or day > 31:
            raise ValueError("Days of month must be between 1 and 31.")
        days.append(day)
    if not days:
        raise ValueError("Enter at least one day of month.")
    return days


def _refresh_scheduled_action_next_run(action: ScheduledAction) -> None:
    if action.schedule_type == ScheduledAction.ScheduleType.ONCE:
        if action.run_at is None:
            raise ValueError("One-time schedules require a run time.")
        action.next_run_at = action.run_at
        return

    try:
        action.next_run_at = next_run_after(action, after=tz.now())
    except RecurrenceError as exc:
        raise ValueError(str(exc)) from exc
    if action.next_run_at is None:
        raise ValueError("Could not calculate the next run time.")


def _parse_local_datetime(value: str):
    value = value.strip()
    if not value:
        raise ValueError("Run time is required.")
    parsed = parse_datetime(value)
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Run time must be a valid date and time.") from exc
    if tz.is_naive(parsed):
        return tz.make_aware(parsed, ZoneInfo(settings.TIME_ZONE))
    return parsed


def _parse_local_datetime_from_post(post):
    legacy_value = post.get("run_at", "").strip()
    if legacy_value:
        return _parse_local_datetime(legacy_value)

    run_date = post.get("run_date", "").strip()
    if not run_date:
        raise ValueError("Run date is required.")
    try:
        parsed_date = datetime.strptime(run_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("Run date must use YYYY-MM-DD format.") from exc

    hour = _bounded_int(post.get("run_hour", "0"), 0, 23, "Run hour")
    minute = _bounded_int(post.get("run_minute", "0"), 0, 59, "Run minute")
    return tz.make_aware(datetime.combine(parsed_date, time(hour, minute)), ZoneInfo(settings.TIME_ZONE))


def _time_value_from_post(post, prefix: str, *, fallback_name: str, label: str) -> str:
    hour_value = post.get(f"{prefix}_hour")
    minute_value = post.get(f"{prefix}_minute")
    if hour_value is None and minute_value is None:
        fallback = post.get(fallback_name, "").strip()
        if fallback:
            hour_value, minute_value = _time_parts(fallback)
    hour = _bounded_int(str(hour_value if hour_value is not None else "0"), 0, 23, f"{label} hour")
    minute = _bounded_int(str(minute_value if minute_value is not None else "0"), 0, 59, f"{label} minute")
    return f"{hour:02d}:{minute:02d}"


def _bounded_int(value: str, minimum: int, maximum: int, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number.") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}.")
    return parsed


def _datetime_parts(value) -> tuple[str, str, str]:
    if value is None:
        return "", "22", "00"
    local_value = tz.localtime(value)
    return local_value.strftime("%Y-%m-%d"), local_value.strftime("%H"), local_value.strftime("%M")


def _time_parts(value: str) -> tuple[str, str]:
    parts = str(value or "22:00").split(":")
    if len(parts) < 2:
        return "22", "00"
    try:
        hour = max(0, min(23, int(parts[0])))
        minute = max(0, min(59, int(parts[1])))
    except ValueError:
        return "22", "00"
    return f"{hour:02d}", f"{minute:02d}"


def _posted_values(post, name: str, *, fallback_names: list[str] | None = None, default: list[str] | None = None) -> list[str]:
    values = []
    if hasattr(post, "getlist"):
        values = [str(value) for value in post.getlist(name) if str(value) != ""]
    else:
        raw_value = post.get(name)
        if isinstance(raw_value, list):
            values = [str(value) for value in raw_value if str(value) != ""]
        elif raw_value not in (None, ""):
            values = [str(raw_value)]

    for fallback_name in fallback_names or []:
        if values:
            break
        fallback_value = post.get(fallback_name)
        if fallback_value not in (None, ""):
            values = [str(fallback_value)]

    return values or list(default or [])


def _recurrence_days_label(recurrence: dict) -> str:
    days = _recurrence_values(recurrence, "days_of_month", "day", "day_of_month", default=["1"])
    return ",".join(days)


def _posted_datetime_parts(post, prefix: str) -> dict[str, str]:
    legacy_value = post.get(f"{prefix}_at", "").strip()
    if legacy_value:
        try:
            parsed = _parse_local_datetime(legacy_value)
        except ValueError:
            return {
                f"{prefix}_date": post.get(f"{prefix}_date", ""),
                f"{prefix}_hour": post.get(f"{prefix}_hour", "22"),
                f"{prefix}_minute": post.get(f"{prefix}_minute", "00"),
            }
        date_value, hour_value, minute_value = _datetime_parts(parsed)
        return {
            f"{prefix}_date": post.get(f"{prefix}_date", date_value),
            f"{prefix}_hour": post.get(f"{prefix}_hour", hour_value),
            f"{prefix}_minute": post.get(f"{prefix}_minute", minute_value),
        }
    return {
        f"{prefix}_date": post.get(f"{prefix}_date", ""),
        f"{prefix}_hour": post.get(f"{prefix}_hour", "22"),
        f"{prefix}_minute": post.get(f"{prefix}_minute", "00"),
    }


def _audit_scheduled_action_definition(request, action: str, scheduled_action: ScheduledAction) -> None:
    record_audit_event(
        request,
        action=action,
        object_type="scheduled_action",
        object_id=str(scheduled_action.id),
        details={
            "scheduled_action_id": scheduled_action.id,
            "scheduled_action_name": scheduled_action.name,
            "enabled": scheduled_action.enabled,
            "action_type": scheduled_action.action_type,
            "target_type": scheduled_action.target_type,
            "target_vmid": scheduled_action.target_vmid,
            "target_node": scheduled_action.target_node,
            "schedule_type": scheduled_action.schedule_type,
            "recurrence_kind": scheduled_action.recurrence_kind,
            "next_run_at": scheduled_action.next_run_at.isoformat() if scheduled_action.next_run_at else "",
        },
    )


def _scheduled_action_target_label(action: ScheduledAction) -> str:
    return guest_identity_from_scheduled_action(action).full_label_with_type


def _latest_scheduled_runs(
    *,
    target_type: str | None = None,
    target_vmid: int | None = None,
    limit: int = 10,
) -> list[ScheduledActionRun]:
    runs_query = ScheduledActionRun.objects.select_related("scheduled_action")
    if target_type and target_vmid is not None:
        runs_query = runs_query.filter(scheduled_action__target_type=target_type, scheduled_action__target_vmid=target_vmid)

    runs = list(runs_query.order_by("-created_at")[:limit])
    for run in runs:
        run.display_target = _scheduled_action_target_label(run.scheduled_action)
        run.guest_identity = guest_identity_from_scheduled_action(run.scheduled_action)
        run.display_status_class = _scheduled_run_status_class(run.status)
        run.display_outcome = run.get_outcome_display() if run.outcome else "-"
        run.display_node = _scheduled_run_node_label(run)
    return runs


def _serialize_scheduled_run(run: ScheduledActionRun) -> dict[str, object]:
    return {
        "planned_for": _format_local_datetime(run.planned_for),
        "task": run.scheduled_action.name,
        "target": run.display_target,
        "target_guest": guest_identity_from_scheduled_action(run.scheduled_action).as_dict(),
        "status": run.get_status_display(),
        "status_class": run.display_status_class,
        "outcome": run.display_outcome,
        "started_at": _format_local_datetime(run.started_at),
        "finished_at": _format_local_datetime(run.finished_at),
        "node": run.display_node,
        "message": run.error or "-",
    }


def _scheduled_run_node_label(run: ScheduledActionRun) -> str:
    preflight = run.preflight_snapshot if isinstance(run.preflight_snapshot, dict) else {}
    return run.proxmox_task_node or str(preflight.get("node") or "") or run.scheduled_action.target_node or "-"


def _format_local_datetime(value: datetime | None) -> str:
    if value is None:
        return "-"
    return tz.localtime(value).strftime("%Y-%m-%d %H:%M:%S")


def _scheduled_run_status_class(status: str) -> str:
    return {
        ScheduledActionRun.Status.COMPLETED: "completed",
        ScheduledActionRun.Status.QUEUED: "queued",
        ScheduledActionRun.Status.PREFLIGHT: "running",
        ScheduledActionRun.Status.SUBMITTED: "running",
        ScheduledActionRun.Status.POLLING: "running",
        ScheduledActionRun.Status.FAILED: "failed",
        ScheduledActionRun.Status.TIMEOUT: "failed",
        ScheduledActionRun.Status.STALE: "failed",
        ScheduledActionRun.Status.SKIPPED: "warning",
        ScheduledActionRun.Status.MISSED: "warning",
    }.get(status, "")

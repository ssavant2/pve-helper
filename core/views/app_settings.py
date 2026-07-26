"""Installation-wide PVE-helper settings."""

from __future__ import annotations

from core.services.audit_events import record_audit_event
from core.services.scheduled_action_settings import (
    MAX_RUN_HISTORY_RETENTION_DAYS,
    MIN_RUN_HISTORY_RETENTION_DAYS,
    settings_record,
    update_run_history_retention,
)

from .common import (
    app_login_required,
    navigation_context,
    redirect,
    render,
)


@app_login_required
def pve_helper_settings(request):
    return redirect("core:settings_storage")


@app_login_required
def scheduled_task_settings(request):
    errors: list[str] = []
    config = settings_record()

    if request.method == "POST":
        try:
            days = int(str(request.POST.get("run_history_retention_days") or ""))
            config = update_run_history_retention(days=days)
        except (TypeError, ValueError):
            errors.append(
                f"Run history retention must be between {MIN_RUN_HISTORY_RETENTION_DAYS} "
                f"and {MAX_RUN_HISTORY_RETENTION_DAYS} days."
            )
        else:
            record_audit_event(
                request=request,
                action="scheduled_action.run_retention.updated",
                object_type="scheduled_action_settings",
                object_id="run-history",
                details={"retention_days": config.run_history_retention_days},
            )
            return redirect("core:settings_scheduled_tasks")

    return render(
        request,
        "core/settings_scheduled_tasks.html",
        {
            **navigation_context("pve_settings", page_title=("Scheduled Tasks", "Settings")),
            "active_settings_tab": "scheduled_tasks",
            "config": config,
            "errors": errors,
            "min_retention_days": MIN_RUN_HISTORY_RETENTION_DAYS,
            "max_retention_days": MAX_RUN_HISTORY_RETENTION_DAYS,
        },
    )

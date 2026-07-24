from __future__ import annotations

import logging

from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django_q.tasks import async_task

from core.models import LogForwardingDelivery
from core.services.audit_events import record_audit_event
from core.services.log_forwarding import (
    LOG_FORWARDER_FUNC,
    LogForwarderConfigurationError,
    configuration,
    update_configuration,
)

from .common import app_login_required, navigation_context

logger = logging.getLogger(__name__)


TEST_FEEDBACK = {
    "disabled": ("error", "Enable and save log forwarding before sending a test event."),
    "queued": ("success", "Test event queued for delivery."),
    "retry": ("warning", "The test event was queued and will be retried by the background worker."),
}


@app_login_required
def log_forwarder_settings(request):
    errors: list[str] = []
    config = configuration()
    if request.method == "POST":
        try:
            port = int(str(request.POST.get("port") or ""))
        except ValueError:
            port = 0
        enabled = request.POST.get("enabled") == "on"
        host = str(request.POST.get("host") or "")
        transport = str(request.POST.get("transport") or "")
        try:
            config = update_configuration(enabled=enabled, host=host, port=port, transport=transport)
        except LogForwarderConfigurationError as exc:
            errors.append(exc.public_message)
        else:
            record_audit_event(
                request=request,
                action="log_forwarder.configuration.updated",
                object_type="log_forwarder",
                object_id="rfc5424",
                details={
                    "enabled": config.enabled,
                    "host": config.host,
                    "port": config.port,
                    "transport": config.transport,
                },
            )
            return redirect("core:settings_log_forwarder")

    delivery_status = _delivery_status(config)
    return render(
        request,
        "core/settings_log_forwarder.html",
        {
            **navigation_context("pve_settings", page_title=("Log Forwarder", "Settings")),
            "active_settings_tab": "log_forwarder",
            "config": config,
            "transport_choices": config.Transport.choices,
            "errors": errors,
            "delivery_status": delivery_status,
            "test_feedback": TEST_FEEDBACK.get(request.GET.get("test", "")),
        },
    )


@require_GET
@app_login_required
def log_forwarder_status(request):
    return JsonResponse(_delivery_status(configuration()))


@require_POST
@app_login_required
def log_forwarder_test(request):
    config = configuration()
    if not config.enabled:
        return redirect(f"{reverse('core:settings_log_forwarder')}?test=disabled")

    record_audit_event(
        request=request,
        action="log_forwarder.test_requested",
        object_type="log_forwarder",
        object_id="rfc5424",
        details={"host": config.host, "port": config.port, "transport": config.transport},
    )
    try:
        async_task(LOG_FORWARDER_FUNC)
    except Exception:
        # The durable delivery remains pending for the minute scheduler. Queue
        # diagnostics are protected logs; the browser receives caller-owned text.
        logger.exception("Could not enqueue immediate log-forwarding test delivery")
        outcome = "retry"
    else:
        outcome = "queued"
    return redirect(f"{reverse('core:settings_log_forwarder')}?test={outcome}")


def _delivery_status(config) -> dict[str, object]:
    counts = {
        row["status"]: row["total"]
        for row in LogForwardingDelivery.objects.values("status").annotate(total=Count("id"))
    }
    pending = counts.get(LogForwardingDelivery.Status.PENDING, 0) + counts.get(LogForwardingDelivery.Status.SENDING, 0)
    paused = bool(pending and not config.enabled)
    if config.last_error_code:
        last_error = "Connection failed"
        if config.last_error_at:
            last_error += f" at {_display_time(config.last_error_at)}"
    else:
        last_error = "None"
    return {
        "state": "Enabled" if config.enabled else "Disabled",
        "pending": pending,
        "pending_label": f"{pending} (paused)" if paused else str(pending),
        "paused": paused,
        "last_delivery": _display_time(config.last_success_at) if config.last_success_at else "Never",
        "last_error": last_error,
    }


def _display_time(value) -> str:
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M:%S")

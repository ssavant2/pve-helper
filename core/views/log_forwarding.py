from __future__ import annotations

import logging

from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django_q.tasks import async_task

from core.models import LogForwarderTransportTrust, LogForwardingDelivery
from core.services.audit_events import record_audit_event
from core.services.certificates import expiry_warning_days, expiry_warnings_enabled
from core.services.log_forwarder_certificate_watch import (
    CERTIFICATE_ANSWERED_ACTION,
    open_certificate_questions,
)
from core.services.log_forwarder_trust import (
    InspectedCertificate,
    LogForwarderTrustError,
    approve_destination,
    inspect_destination,
    trust,
    trust_applies_to,
)
from core.services.log_forwarding import (
    LOG_FORWARDER_FUNC,
    LogForwarderConfigurationError,
    configuration,
    destination_requires_approval,
    update_configuration,
)

from .common import app_login_required, navigation_context

logger = logging.getLogger(__name__)


TEST_FEEDBACK = {
    "disabled": ("error", "Enable and save log forwarding before sending a test event."),
    "queued": ("success", "Test event queued for delivery."),
    "retry": ("warning", "The test event was queued and will be retried by the background worker."),
    "untrusted": ("error", "Approve the destination's TLS certificate before sending a test event."),
}

# "Unreachable" and "I do not trust that certificate" need opposite responses from
# the operator, so the status panel names them apart instead of collapsing both
# into one connection failure.
ERROR_CODE_LABELS = {
    "syslog_connection_failed": "Connection failed",
    "syslog_tls_verification_failed": "Certificate verification failed",
    "syslog_tls_failed": "TLS handshake failed",
    "syslog_tls_not_approved": "Certificate not approved",
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
            "trust_summary": _trust_summary(config),
            "expiry_warning_days": expiry_warning_days(),
        },
    )


@require_POST
@app_login_required
def log_forwarder_inspect(request):
    """Show what the destination is serving, for a human to accept or reject.

    Driven from the Save and Test buttons rather than a separate "inspect" step:
    the moment an operator commits to a destination is the moment they should be
    looking at its certificate, and a step they can skip is one they will skip.
    """
    host = str(request.POST.get("host") or "").strip()
    try:
        port = int(str(request.POST.get("port") or ""))
    except ValueError:
        port = 0
    try:
        inspection = inspect_destination(host, port)
    except LogForwarderTrustError as exc:
        return JsonResponse({"ok": False, "error": exc.public_message}, status=502)

    record = trust()
    return JsonResponse(
        {
            "ok": True,
            "host": host,
            "port": port,
            "certificate": _inspection_payload(inspection),
            "current": _trust_payload(record) if trust_applies_to(record, host, port) else None,
            "expiry_warning_days": expiry_warning_days(),
        }
    )


@require_POST
@app_login_required
def log_forwarder_approve(request):
    """Persist the operator's answer, then re-verify by inspecting once more.

    The certificate is re-fetched here instead of trusting what the browser posted
    back: a fingerprint round-tripping through a form is a claim about the past,
    and the thing being approved must be the thing currently on the wire.
    """
    host = str(request.POST.get("host") or "").strip()
    try:
        port = int(str(request.POST.get("port") or ""))
    except ValueError:
        port = 0
    mode = str(request.POST.get("mode") or "").strip()
    confirmed = str(request.POST.get("fingerprint") or "").strip().lower()

    try:
        inspection = inspect_destination(host, port)
        if confirmed and confirmed != inspection.sha256_fingerprint:
            raise LogForwarderTrustError(
                "The destination is now presenting a different certificate than the one shown. Nothing was approved."
            )
        record = approve_destination(
            mode=mode,
            host=host,
            port=port,
            inspection=inspection,
            approved_by=request.user.get_username() if request.user.is_authenticated else "",
        )
    except LogForwarderTrustError as exc:
        return JsonResponse({"ok": False, "error": exc.public_message}, status=400)

    record_audit_event(
        request=request,
        action="log_forwarder.certificate.approved",
        object_type="log_forwarder",
        object_id="rfc5424",
        details={
            "host": host,
            "port": port,
            "mode": record.mode,
            "fingerprint": record.sha256_fingerprint,
            "served_fingerprint": inspection.sha256_fingerprint,
            "subject": record.subject,
            "issuer": record.issuer,
        },
    )
    _answer_open_questions(request, record)
    return JsonResponse({"ok": True, "trust": _trust_payload(record)})


def _answer_open_questions(request, record) -> None:
    """Approving the new certificate *is* the answer to the pending question.

    Requiring a second click on the badge afterwards would leave it pulsing over a
    problem the operator just solved, which is how a question badge stops being
    believed.
    """
    for event in open_certificate_questions():
        details = dict(event.details) if isinstance(event.details, dict) else {}
        details["question_dismissed"] = True
        event.details = details
        event.save(update_fields=["details"])
        record_audit_event(
            request=request,
            action=CERTIFICATE_ANSWERED_ACTION,
            object_type="log_forwarder",
            object_id="rfc5424",
            details={
                "answer": "approved",
                "condition": details.get("condition") or "",
                "question_event_id": event.id,
                "mode": record.mode,
                "fingerprint": record.sha256_fingerprint,
            },
        )


def _inspection_payload(inspection: InspectedCertificate) -> dict[str, object]:
    return {
        "subject": inspection.subject,
        "issuer": inspection.issuer,
        "sha256_fingerprint": inspection.sha256_fingerprint,
        "not_before": timezone.localtime(inspection.not_before).strftime("%Y-%m-%d %H:%M"),
        "not_after": timezone.localtime(inspection.not_after).strftime("%Y-%m-%d %H:%M"),
        "expires_in_days": inspection.expires_in_days,
        "self_signed": inspection.self_signed,
        "system_trusted": inspection.system_trusted,
        "verification_error": inspection.verification_error,
        "ca_available": inspection.ca_available,
    }


def _trust_payload(record) -> dict[str, object]:
    return {
        "mode": record.mode,
        "mode_label": record.get_mode_display(),
        "fingerprint": record.sha256_fingerprint,
        "subject": record.subject,
        "issuer": record.issuer,
        "approved_at": _display_time(record.approved_at) if record.approved_at else "",
        "approved_by": record.approved_by,
    }


def _trust_summary(config) -> dict[str, object]:
    """What the settings page says about trust before anything is clicked."""
    record = trust()
    applies = trust_applies_to(record, config.host, config.port)
    tls = config.transport == config.Transport.TLS
    observed_not_after = record.observed_not_after
    expiring = bool(
        tls
        and applies
        and expiry_warnings_enabled()
        and record.mode != LogForwarderTransportTrust.Mode.INSECURE
        and observed_not_after
        and (observed_not_after - timezone.now()).days <= expiry_warning_days()
    )
    return {
        "applies": applies,
        "tls": tls,
        "required": destination_requires_approval(config),
        "mode": record.mode if applies else LogForwarderTransportTrust.Mode.UNSET,
        "mode_label": record.get_mode_display() if applies else "Not approved yet",
        "fingerprint": record.sha256_fingerprint if applies else "",
        "subject": record.subject if applies else "",
        "issuer": record.issuer if applies else "",
        "approved_at": _display_time(record.approved_at) if applies and record.approved_at else "",
        "approved_by": record.approved_by if applies else "",
        "last_checked_at": _display_time(record.last_checked_at) if applies and record.last_checked_at else "Never",
        "observed_not_after": _display_time(observed_not_after) if applies and observed_not_after else "",
        "expiring": expiring,
        "open_questions": open_certificate_questions().count(),
    }


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
    if destination_requires_approval(config):
        # The delivery would fail closed anyway; saying so here keeps the operator
        # from reading an unapproved certificate as a broken collector.
        return redirect(f"{reverse('core:settings_log_forwarder')}?test=untrusted")

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
        last_error = ERROR_CODE_LABELS.get(config.last_error_code, "Connection failed")
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

"""The daily certificate check behind the syslog destination's trust decision.

A pinned or CA-anchored certificate is a decision made once and then left alone,
which is exactly why it needs a watcher: the two ways it stops being true —
the collector is re-keyed, or the certificate simply expires — are both silent
until delivery starts failing, and by then the Audit stream has already stopped.

The check therefore runs on its own daily schedule rather than off delivery, so it
also reports on an installation whose Audit stream happens to be idle. It raises
its finding as an ordinary Recent Tasks *question*, reusing the pulsing
"click to answer" affordance the force-stop and partial-fan-out cases already use:
an operator who is not looking at Settings still sees it.

Deduplication is by condition *and* fingerprint, not by day. A daily job that filed
a fresh row every run would turn one unanswered question into a stream of them and
train the operator to ignore the badge — the failure mode the pulsing badge exists
to avoid.
"""

from __future__ import annotations

import logging
import socket
import ssl

from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from django_q.models import Schedule

from core.models import AuditEvent, LogForwarderConfiguration, LogForwarderTransportTrust
from core.services.certificates import expiry_warning_days, expiry_warnings_enabled
from core.services.log_forwarder_trust import (
    LogForwarderTrustError,
    assert_pinned_match,
    delivery_context,
    expiry_threshold,
    inspect_destination,
    trust,
    trust_applies_to,
)

logger = logging.getLogger(__name__)

CERTIFICATE_QUESTION_ACTION = "log_forwarder.certificate.attention"
CERTIFICATE_ANSWERED_ACTION = "log_forwarder.certificate.answered"
CERTIFICATE_WATCH_SCHEDULE_NAME = "pve-helper log forwarder certificate watch"
CERTIFICATE_WATCH_FUNC = "core.services.log_forwarder_certificate_watch.check_destination_certificate"

CONDITION_CHANGED = "changed"
CONDITION_EXPIRING = "expiring"
CONDITION_UNTRUSTED = "untrusted"

CONDITION_LABELS = {
    CONDITION_CHANGED: "The syslog destination presented a new certificate",
    CONDITION_EXPIRING: "The syslog destination's certificate is about to expire",
    CONDITION_UNTRUSTED: "The syslog destination's certificate no longer verifies",
}


def check_destination_certificate() -> dict[str, object]:
    """Probe the destination and raise a question when the approval no longer holds.

    Runs for every verifying mode. `insecure` is skipped deliberately: an operator
    who chose to verify nothing has already answered the change question, and
    expiry is meaningless to a context that does not check dates.
    """
    config = LogForwarderConfiguration.objects.filter(pk=1).first()
    if config is None or not config.enabled or config.transport != LogForwarderConfiguration.Transport.TLS:
        return {"checked": False, "reason": "not_applicable"}

    record = trust()
    if not trust_applies_to(record, config.host, config.port):
        return {"checked": False, "reason": "not_approved"}
    if record.mode == LogForwarderTransportTrust.Mode.INSECURE:
        return {"checked": False, "reason": "insecure"}

    try:
        inspection = inspect_destination(config.host, config.port)
    except LogForwarderTrustError:
        # Unreachable is not a trust finding. Delivery failures already report it,
        # and filing a question here would blame the certificate for a network
        # outage — the one thing an operator must not be taught to do.
        logger.info("Syslog certificate watch could not reach the destination", exc_info=True)
        return {"checked": False, "reason": "unreachable"}

    now = timezone.now()
    LogForwarderTransportTrust.objects.filter(pk=record.pk).update(
        last_checked_at=now,
        observed_sha256_fingerprint=inspection.sha256_fingerprint,
        observed_subject=inspection.subject[:500],
        observed_not_after=inspection.not_after,
        updated_at=now,
    )

    condition, detail = _evaluate(record, config, inspection)
    if not condition:
        _resolve_open_questions()
        return {"checked": True, "condition": ""}

    created = _raise_question(
        condition=condition,
        detail=detail,
        config=config,
        inspection=inspection,
    )
    return {"checked": True, "condition": condition, "created": created}


def _evaluate(record, config, inspection) -> tuple[str, str]:
    """Which of the three findings applies, most serious first.

    A changed certificate that still verifies under a CA approval is not a finding:
    that silent-renewal case is precisely what the operator asked for by choosing
    "any certificate from this issuer" over pinning.
    """
    verified, reason = _verifies_under_trust(record, config)
    if not verified:
        condition = (
            CONDITION_CHANGED
            if inspection.sha256_fingerprint != record.observed_sha256_fingerprint
            else (CONDITION_UNTRUSTED)
        )
        return condition, reason
    if expiry_warnings_enabled() and inspection.not_after <= expiry_threshold():
        days = max((inspection.not_after - timezone.now()).days, 0)
        return CONDITION_EXPIRING, f"Expires in {days} day{'s' if days != 1 else ''}."
    return "", ""


def _verifies_under_trust(record, config) -> tuple[bool, str]:
    """Does the approved trust still accept what is being served?"""
    try:
        context = delivery_context(record)
    except LogForwarderTrustError as exc:
        return False, exc.public_message
    try:
        with socket.create_connection((config.host, config.port), timeout=8) as connection:
            with context.wrap_socket(connection, server_hostname=config.host) as tls:
                assert_pinned_match(record, tls.getpeercert(binary_form=True) or b"")
    except LogForwarderTrustError as exc:
        return False, exc.public_message
    except ssl.SSLCertVerificationError as exc:
        return False, str(exc.verify_message or exc.reason or "certificate verification failed")
    except (OSError, ssl.SSLError):
        # Reachability was already proven by the inspection probe moments ago, so a
        # failure here is a trust failure in all but name. Report it as unverified
        # rather than guessing at a network cause.
        return False, "The approved trust could not complete a verified handshake."
    return True, ""


def _open_question_q() -> Q:
    """An unanswered certificate question.

    The `has_key` half is what makes the negation sound, for the same reason
    `recent_tasks._dismissed_flag_q` spells it out: a bare `details__flag=True`
    over a row that lacks the key is SQL NULL, not false, so `~Q(...)` would drop
    exactly the never-answered rows this is meant to find.
    """
    dismissed = Q(details__has_key="question_dismissed") & Q(details__question_dismissed=True)
    return Q(action=CERTIFICATE_QUESTION_ACTION, details__question=True) & ~dismissed


def open_certificate_questions():
    return AuditEvent.objects.filter(_open_question_q())


def _raise_question(*, condition: str, detail: str, config, inspection) -> bool:
    """File the question, unless the same one is already unanswered.

    Same condition *and* same fingerprint: a certificate that changes twice before
    anyone looks is genuinely a new question, while the same certificate seen again
    tomorrow is not.
    """
    existing = open_certificate_questions().filter(
        details__condition=condition,
        details__fingerprint=inspection.sha256_fingerprint,
    )
    if existing.exists():
        return False

    from core.services.audit_events import record_audit_event

    record_audit_event(
        request=None,
        action=CERTIFICATE_QUESTION_ACTION,
        object_type="log_forwarder",
        object_id="rfc5424",
        outcome="warning",
        system_username="system",
        username="system",
        details={
            "question": True,
            "condition": condition,
            "label": CONDITION_LABELS.get(condition, "The syslog destination needs attention"),
            "detail": detail,
            "host": config.host,
            "port": config.port,
            "fingerprint": inspection.sha256_fingerprint,
            "subject": inspection.subject,
            "issuer": inspection.issuer,
            "not_after": inspection.not_after.isoformat(),
            "self_signed": inspection.self_signed,
            "expiry_warning_days": expiry_warning_days(),
        },
    )
    return True


def _resolve_open_questions() -> None:
    """A healthy check answers its own outstanding questions.

    An operator who re-approved the new certificate has already resolved the
    finding; leaving the badge pulsing until they also click it would make the
    badge mean "something happened once", not "something needs you".
    """
    for event in open_certificate_questions():
        details = dict(event.details) if isinstance(event.details, dict) else {}
        details["question_dismissed"] = True
        details["resolved_by_check_at"] = timezone.now().isoformat()
        event.details = details
        event.save(update_fields=["details"])


def ensure_certificate_watch_schedule(enabled: bool) -> None:
    """One run a day. The two failure modes move on the scale of days, not minutes."""
    if not enabled:
        Schedule.objects.filter(name=CERTIFICATE_WATCH_SCHEDULE_NAME).delete()
        return
    defaults = {
        "func": CERTIFICATE_WATCH_FUNC,
        "schedule_type": Schedule.DAILY,
        "next_run": timezone.now(),
        "repeats": -1,
        "cluster": settings.Q_CLUSTER.get("name"),
    }
    schedule, created = Schedule.objects.get_or_create(name=CERTIFICATE_WATCH_SCHEDULE_NAME, defaults=defaults)
    if not created:
        for field, value in defaults.items():
            setattr(schedule, field, value)
        schedule.save(update_fields=[*defaults.keys()])

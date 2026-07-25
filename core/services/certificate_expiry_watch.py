"""The daily expiry check for the certificates this installation stores itself.

The syslog destination's certificate is watched by probing it, because it belongs to
someone else. These do not need probing: the application holds the certificate, so
the check is a date comparison and the only interesting question is where the answer
is delivered.

It is delivered as a Recent Tasks *question*, the same pulsing "click to answer"
affordance the syslog watch and the force-stop offer already use, because the
Certificates page is not a page anyone visits. An HTTPS certificate that lapses takes
the UI down with it — including the page that would have replaced it — so the warning
has to reach an operator who is doing something else entirely.

Unlike a syslog certificate, this one cannot be *answered* by approving anything: the
fix is uploading a replacement. The question is therefore acknowledgeable, and it
closes on its own as soon as a check finds the certificate replaced or removed.
"""

from __future__ import annotations

from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from django_q.models import Schedule

from core.models import AuditEvent, ManagedCertificate
from core.services.certificates import days_until_expiry, expiry_warning_days, expiry_warnings_enabled

EXPIRY_QUESTION_ACTION = "certificate.expiry.attention"
EXPIRY_ANSWERED_ACTION = "certificate.expiry.answered"
EXPIRY_WATCH_SCHEDULE_NAME = "pve-helper certificate expiry watch"
EXPIRY_WATCH_FUNC = "core.services.certificate_expiry_watch.check_stored_certificates"

CONDITION_EXPIRING = "expiring"
CONDITION_EXPIRED = "expired"

CONDITION_LABELS = {
    CONDITION_EXPIRING: "A stored certificate is about to expire",
    CONDITION_EXPIRED: "A stored certificate has expired",
}

USAGE_LABELS = {
    ManagedCertificate.Usage.SERVER: "HTTPS certificate",
    ManagedCertificate.Usage.AUTHORITY: "Trusted authority",
}


def check_stored_certificates() -> dict[str, object]:
    """Warn about every stored certificate at or past the configured threshold."""
    if not expiry_warnings_enabled():
        # Turning warnings off closes what they raised. Leaving stale questions
        # pulsing after the operator switched the policy off would make the badge
        # mean "this was true once".
        resolved = _resolve_questions(open_expiry_questions())
        return {"checked": False, "reason": "disabled", "resolved": resolved}

    now = timezone.now()
    threshold = expiry_warning_days()
    raised = 0
    still_relevant: set[str] = set()

    for record in ManagedCertificate.objects.all():
        remaining = days_until_expiry(record, now=now)
        if remaining is None or remaining > threshold:
            continue
        condition = CONDITION_EXPIRED if remaining < 0 else CONDITION_EXPIRING
        still_relevant.add(record.sha256_fingerprint)
        if _raise_question(record=record, condition=condition, remaining=remaining, threshold=threshold):
            raised += 1

    # A certificate that was replaced, removed or renewed since the last run no
    # longer has a finding, so its question is answered by the facts rather than by
    # waiting for someone to click a badge about a certificate that is already gone.
    stale = open_expiry_questions()
    if still_relevant:
        stale = stale.exclude(details__fingerprint__in=sorted(still_relevant))
    return {"checked": True, "raised": raised, "resolved": _resolve_questions(stale)}


def _open_question_q() -> Q:
    """An unanswered expiry question.

    The `has_key` pairing is what makes the negation sound: over a row that never
    had the key, `details__question_dismissed=True` is SQL NULL rather than false,
    so a bare `~Q(...)` would exclude exactly the rows this needs to find.
    """
    dismissed = Q(details__has_key="question_dismissed") & Q(details__question_dismissed=True)
    return Q(action=EXPIRY_QUESTION_ACTION, details__question=True) & ~dismissed


def open_expiry_questions():
    return AuditEvent.objects.filter(_open_question_q())


def _raise_question(*, record: ManagedCertificate, condition: str, remaining: int, threshold: int) -> bool:
    """File the question unless the same one is already unanswered.

    Keyed on fingerprint and condition, so the daily run does not refile the same
    warning every day — and so a certificate that crosses from expiring to expired
    does produce the second, more serious question.
    """
    already_open = open_expiry_questions().filter(
        details__fingerprint=record.sha256_fingerprint,
        details__condition=condition,
    )
    if already_open.exists():
        return False

    from core.services.audit_events import record_audit_event

    if remaining < 0:
        detail = f"Expired {abs(remaining)} day{'s' if abs(remaining) != 1 else ''} ago."
    else:
        detail = f"Expires in {remaining} day{'s' if remaining != 1 else ''}."

    record_audit_event(
        request=None,
        action=EXPIRY_QUESTION_ACTION,
        object_type="certificate",
        object_id=str(record.pk),
        outcome="warning",
        system_username="system",
        username="system",
        details={
            "question": True,
            "condition": condition,
            "label": CONDITION_LABELS.get(condition, "A stored certificate needs attention"),
            "detail": detail,
            "usage": record.usage,
            "usage_label": USAGE_LABELS.get(record.usage, record.usage),
            "certificate_label": record.label,
            "subject": record.subject,
            "issuer": record.issuer,
            "fingerprint": record.sha256_fingerprint,
            "not_after": record.not_after.isoformat() if record.not_after else "",
            "days_remaining": remaining,
            "expiry_warning_days": threshold,
        },
    )
    return True


def _resolve_questions(queryset) -> int:
    resolved = 0
    for event in queryset:
        details = dict(event.details) if isinstance(event.details, dict) else {}
        details["question_dismissed"] = True
        details["resolved_by_check_at"] = timezone.now().isoformat()
        event.details = details
        event.save(update_fields=["details"])
        resolved += 1
    return resolved


def ensure_certificate_expiry_schedule() -> None:
    """Always on. Certificates expire whether or not anything is configured."""
    defaults = {
        "func": EXPIRY_WATCH_FUNC,
        "schedule_type": Schedule.DAILY,
        "next_run": timezone.now(),
        "repeats": -1,
        "cluster": settings.Q_CLUSTER.get("name"),
    }
    schedule, created = Schedule.objects.get_or_create(name=EXPIRY_WATCH_SCHEDULE_NAME, defaults=defaults)
    if not created:
        for field, value in defaults.items():
            setattr(schedule, field, value)
        schedule.save(update_fields=[*defaults.keys()])

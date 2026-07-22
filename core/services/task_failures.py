"""One way to write a durable failure onto an audit row."""

from __future__ import annotations

from django.utils import timezone

from core.models import AuditEvent
from core.services.public_errors import (
    UNEXPECTED_FAILURE_MESSAGE,
    PublicFailure,
    public_failure,
)


def failure_fields(
    exc: BaseException,
    *,
    operation: str,
    fallback: str = UNEXPECTED_FAILURE_MESSAGE,
    code: str = "",
) -> dict[str, str]:
    """The `error`/`error_code` pair, for a payload the caller finalises itself.

    Same boundary as `record_event_failure`, for the handful of paths that keep
    building the payload after the failure (a partial-success report, a run row
    that is not an audit event).
    """
    failure = public_failure(exc, operation=operation, fallback=fallback, code=code)
    return {"error": failure.message, "error_code": failure.code}


def record_event_failure(
    event: AuditEvent,
    failure: PublicFailure,
    *,
    details: dict | None = None,
    save: bool = True,
) -> dict:
    """Mark `event` failed with public prose and a machine-readable code.

    Every worker failure path goes through here.  Hand-rolling the payload is
    what let `details["error"]` drift back into raw exception text nine separate
    times, and what let `details["error_code"]` be forgotten on the paths Recent
    Tasks has to make a decision about.  One writer, one shape.
    """
    payload = dict(details) if isinstance(details, dict) else {}
    if not payload and isinstance(event.details, dict):
        payload = dict(event.details)
    payload["error"] = failure.message
    payload["error_code"] = failure.code
    payload["finished_at"] = timezone.now().isoformat()
    event.outcome = "failed"
    event.details = payload
    if save:
        event.save(update_fields=["outcome", "details"])
    return payload


def record_event_exception(
    event: AuditEvent,
    exc: BaseException,
    *,
    operation: str,
    fallback: str,
    code: str = "",
    details: dict | None = None,
    save: bool = True,
) -> dict:
    """`record_event_failure` for the common case of an exception in hand."""
    return record_event_failure(
        event,
        public_failure(exc, operation=operation, fallback=fallback, code=code),
        details=details,
        save=save,
    )

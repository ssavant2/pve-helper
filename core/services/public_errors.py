"""Keep diagnostic exception details server-side and responses predictable."""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Stable machine codes for durable failure payloads.  `details["error_code"]` is
# what product decisions read; `details["error"]` is operator prose and nothing
# else.  Sniffing the prose is how a wording change silently removes a follow-up
# action, so a decision that depends on *why* something failed belongs here.
ERROR_CODE_DOMAIN = "domain_error"
ERROR_CODE_INCOMPLETE = "incomplete_target"
ERROR_CODE_POWERDOWN_FAILED = "powerdown_failed"
ERROR_CODE_PROVIDER = "provider_error"
ERROR_CODE_TASK_FAILED = "task_failed"
ERROR_CODE_TASK_TIMEOUT = "task_timeout"
ERROR_CODE_UNEXPECTED = "unexpected_error"

# The codes that mean "the guest was asked to shut down and is still running",
# and so justify offering a force-stop follow-up.
SHUTDOWN_INCOMPLETE_CODES = (ERROR_CODE_TASK_TIMEOUT, ERROR_CODE_POWERDOWN_FAILED)

PROVIDER_FAILURE_MESSAGE = "The Proxmox API request failed."
TASK_FAILED_MESSAGE = "The Proxmox task did not complete successfully."
TASK_TIMEOUT_MESSAGE = "The Proxmox task did not finish before its timeout."
POWERDOWN_FAILED_MESSAGE = "The guest did not shut down. It may have no ACPI handler or no running QEMU guest agent."
UNEXPECTED_FAILURE_MESSAGE = "The operation failed unexpectedly."


class PublicMessageError(Exception):
    """Marker: this exception class owns its operator-facing text.

    A class may claim this only if *every* one of its raise sites composes the
    message itself.  Interpolating another exception into the message is what
    turns a domain error back into a provider leak — and because the marker
    classifies the type rather than the string, one such raise site would launder
    every message the class carries.  `tests_source_invariants` rejects it.

    The diagnostic cause is not lost: `raise ... from exc` keeps it on
    `__cause__`, and `public_failure()` logs the whole chain with `exc_info`.
    Rich text in `str(exc)` would be worse than useless here — dozens of view
    paths render `str(exc)` straight into a flash message.
    """

    error_code: str = ERROR_CODE_DOMAIN

    @property
    def public_message(self) -> str:
        return str(self)


@dataclass(frozen=True)
class PublicFailure:
    """The two things a durable failure row needs: prose and a decision code."""

    message: str
    code: str = ERROR_CODE_UNEXPECTED


def public_exception_message(
    exc: BaseException,
    *,
    operation: str,
    fallback: str,
    level: int = logging.WARNING,
) -> str:
    """Log the active exception and return caller-owned, non-sensitive text.

    Provider responses and Python exception strings can include request paths,
    hostnames, implementation details, or upstream diagnostics.  They belong in
    protected logs, not HTML/JSON responses or operator-facing task payloads.
    """
    logger.log(
        level,
        "Operation failed: operation=%s error_type=%s",
        operation,
        exc.__class__.__name__,
        exc_info=True,
    )
    return fallback


def public_failure(
    exc: BaseException,
    *,
    operation: str,
    fallback: str = UNEXPECTED_FAILURE_MESSAGE,
    code: str = "",
    level: int = logging.WARNING,
) -> PublicFailure:
    """The public message and machine code for `exc`, whatever kind it is.

    A `PublicMessageError` keeps its own message, because that message was
    written for this audience.  Everything else — provider errors, unexpected
    exceptions — is replaced by caller-owned text and reaches the operator only
    as a code.
    """
    if isinstance(exc, PublicMessageError):
        logger.log(
            level,
            "Operation failed: operation=%s error_type=%s",
            operation,
            exc.__class__.__name__,
            exc_info=True,
        )
        return PublicFailure(exc.public_message, code or exc.error_code)
    return PublicFailure(
        public_exception_message(exc, operation=operation, fallback=fallback, level=level),
        code or ERROR_CODE_UNEXPECTED,
    )


def proxmox_task_failure(exitstatus: str = "", status: str = "") -> PublicFailure:
    """Classify a Proxmox task that ran to completion and reported a failure.

    The raw `exitstatus` is provider text and is not repeated to the operator,
    but it is the only place the difference between "timed out" and "the guest
    ignored the powerdown" exists, so it is turned into a code here — once —
    instead of being persisted and pattern-matched later.
    """
    text = str(exitstatus or status or "").strip().lower()
    if "timeout" in text or "timed out" in text:
        return PublicFailure(TASK_TIMEOUT_MESSAGE, ERROR_CODE_TASK_TIMEOUT)
    if "powerdown" in text:
        return PublicFailure(POWERDOWN_FAILED_MESSAGE, ERROR_CODE_POWERDOWN_FAILED)
    return PublicFailure(TASK_FAILED_MESSAGE, ERROR_CODE_TASK_FAILED)

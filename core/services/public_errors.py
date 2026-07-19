"""Keep diagnostic exception details server-side and responses predictable."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


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

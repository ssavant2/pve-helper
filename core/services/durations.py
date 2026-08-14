"""How a duration is written for an operator, in one place.

Two surfaces already show uptime — a guest's detail rows and a node's Summary —
and they are read side by side, so a second implementation is a second answer to
the same question. The node page had no implementation at all and printed the raw
`868338s`, which is technically the value and practically nothing.
"""

from __future__ import annotations

from datetime import datetime

from django.utils import timezone

SECONDS_PER_DAY = 86400
SECONDS_PER_HOUR = 3600
SECONDS_PER_MINUTE = 60


def format_uptime(seconds: int | None) -> str:
    """A coarse, two-unit uptime: ``12d 3h``, ``3h 20m``, ``45m``, ``<1m``.

    Minutes are dropped once there are days, because the difference between
    ``12d 3h`` and ``12d 3h 47m`` never decides anything and the extra unit is what
    makes a column of these hard to compare at a glance.
    """

    seconds = int(seconds or 0)
    if seconds <= 0:
        return "-"
    days, rest = divmod(seconds, SECONDS_PER_DAY)
    hours, rest = divmod(rest, SECONDS_PER_HOUR)
    minutes = rest // SECONDS_PER_MINUTE
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return " ".join(parts) or "<1m"


def format_age(when: datetime | None, *, now: datetime | None = None) -> str:
    """How long ago something was observed: ``just now``, ``4 minutes ago``.

    Django's `timesince` says ``0 minutes`` for anything under a minute, and a
    projection that refreshes on a short cadence spends most of its life there —
    so the headline on both Summary pages would read ``observed 0 minutes ago``
    almost always. A negative age lands in the same branch on purpose: a node
    clock a few seconds ahead of this one is not an observation from the future.
    """

    if when is None:
        return ""
    seconds = int(((now or timezone.now()) - when).total_seconds())
    if seconds < SECONDS_PER_MINUTE:
        return "just now"
    if seconds < SECONDS_PER_HOUR:
        return _plural(seconds // SECONDS_PER_MINUTE, "minute")
    if seconds < SECONDS_PER_DAY:
        return _plural(seconds // SECONDS_PER_HOUR, "hour")
    return _plural(seconds // SECONDS_PER_DAY, "day")


def _plural(count: int, unit: str) -> str:
    return f"{count} {unit}{'' if count == 1 else 's'} ago"

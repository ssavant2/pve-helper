"""How a duration is written for an operator, in one place.

Two surfaces already show uptime — a guest's detail rows and a node's Summary —
and they are read side by side, so a second implementation is a second answer to
the same question. The node page had no implementation at all and printed the raw
`868338s`, which is technically the value and practically nothing.
"""

from __future__ import annotations

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

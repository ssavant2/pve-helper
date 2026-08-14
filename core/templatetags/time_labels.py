from __future__ import annotations

from datetime import datetime

from django import template

from core.services.durations import format_age

register = template.Library()


@register.filter
def age(when: datetime | None) -> str:
    """`{{ observed_at|age }}` — `just now`, `4 minutes ago`, `2 days ago`.

    A filter rather than a field on the read models because two different
    dataclasses carry the timestamp and neither owns the wording.
    """
    return format_age(when)

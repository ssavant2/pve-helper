from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django_q.models import Schedule

CONSOLE_SESSION_CLEANUP_SCHEDULE_NAME = "pve-helper console session cleanup"
CONSOLE_SESSION_CLEANUP_FUNC = "core.tasks.prune_expired_console_sessions"
CONSOLE_SESSION_CLEANUP_INTERVAL_MINUTES = 60


def ensure_console_session_cleanup_schedule() -> Schedule:
    defaults = {
        "func": CONSOLE_SESSION_CLEANUP_FUNC,
        "schedule_type": Schedule.MINUTES,
        "minutes": CONSOLE_SESSION_CLEANUP_INTERVAL_MINUTES,
        "repeats": -1,
        "next_run": timezone.now() + timedelta(minutes=1),
        "cluster": settings.Q_CLUSTER.get("name"),
    }
    schedule, created = Schedule.objects.get_or_create(
        name=CONSOLE_SESSION_CLEANUP_SCHEDULE_NAME,
        defaults=defaults,
    )
    if created:
        return schedule

    updates = {
        key: value
        for key, value in defaults.items()
        if key != "next_run" and getattr(schedule, key) != value
    }
    if updates:
        for field, value in updates.items():
            setattr(schedule, field, value)
        schedule.save(update_fields=[*updates.keys()])
    return schedule

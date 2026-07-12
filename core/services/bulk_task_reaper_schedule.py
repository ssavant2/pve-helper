from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django_q.models import Schedule

BULK_TASK_REAPER_SCHEDULE_NAME = "pve-helper bulk task reaper"
BULK_TASK_REAPER_FUNC = "core.tasks.reap_stale_bulk_tasks"
BULK_TASK_REAPER_INTERVAL_MINUTES = 5


def ensure_bulk_task_reaper_schedule() -> Schedule:
    """Run reconciliation on the control queue, never on the bulk queue."""
    defaults = {
        "func": BULK_TASK_REAPER_FUNC,
        "schedule_type": Schedule.MINUTES,
        "minutes": BULK_TASK_REAPER_INTERVAL_MINUTES,
        "repeats": -1,
        "next_run": timezone.now() + timedelta(minutes=1),
        "cluster": settings.Q_CLUSTER.get("name"),
    }
    schedule, created = Schedule.objects.get_or_create(
        name=BULK_TASK_REAPER_SCHEDULE_NAME,
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

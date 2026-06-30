from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django_q.models import Schedule


SPACE_SNAPSHOT_SCHEDULE_NAME = "pve-helper storage space snapshots"
SPACE_SNAPSHOT_FUNC = "core.tasks.record_storage_space_snapshots"
SPACE_SNAPSHOT_INTERVAL_MINUTES = 12 * 60


def ensure_space_snapshot_schedule() -> Schedule:
    defaults = {
        "func": SPACE_SNAPSHOT_FUNC,
        "schedule_type": Schedule.MINUTES,
        "minutes": SPACE_SNAPSHOT_INTERVAL_MINUTES,
        "repeats": -1,
        "next_run": timezone.now() + timedelta(minutes=1),
        "cluster": settings.Q_CLUSTER.get("name"),
    }
    schedule, created = Schedule.objects.get_or_create(
        name=SPACE_SNAPSHOT_SCHEDULE_NAME,
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

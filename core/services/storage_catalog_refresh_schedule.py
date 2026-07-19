from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django_q.models import Schedule


METADATA_SCHEDULE_NAME = "pve-helper storage metadata refresh"
VOLUME_SCHEDULE_NAME = "pve-helper storage volume refresh"


def _ensure(*, name: str, func: str, minutes: int, initial_delay: int) -> Schedule:
    defaults = {
        "func": func,
        "schedule_type": Schedule.MINUTES,
        "minutes": max(1, minutes),
        "repeats": -1,
        "next_run": timezone.now() + timedelta(seconds=initial_delay),
        "cluster": settings.Q_CLUSTER.get("name"),
    }
    schedule, created = Schedule.objects.get_or_create(name=name, defaults=defaults)
    if created:
        return schedule
    updates = {
        key: value
        for key, value in defaults.items()
        if key != "next_run" and getattr(schedule, key) != value
    }
    if updates:
        for key, value in updates.items():
            setattr(schedule, key, value)
        schedule.save(update_fields=list(updates))
    return schedule


def ensure_storage_catalog_refresh_schedules() -> tuple[Schedule, Schedule]:
    return (
        _ensure(
            name=METADATA_SCHEDULE_NAME,
            func="core.tasks.refresh_all_storage_metadata",
            minutes=settings.STORAGE_METADATA_REFRESH_INTERVAL_MINUTES,
            initial_delay=20,
        ),
        _ensure(
            name=VOLUME_SCHEDULE_NAME,
            func="core.tasks.refresh_all_storage_volumes",
            minutes=settings.STORAGE_VOLUME_REFRESH_INTERVAL_MINUTES,
            initial_delay=45,
        ),
    )

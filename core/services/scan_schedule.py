from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django_q.models import Schedule

SCAN_SCHEDULE_NAME = "pve-helper automatic storage scan"
SCAN_SCHEDULE_FUNC = "core.tasks.enqueue_scheduled_scan"
DEFAULT_SCAN_INTERVAL_MINUTES = 60
MIN_SCAN_INTERVAL_MINUTES = 1
MAX_SCAN_INTERVAL_MINUTES = 9999


@dataclass(frozen=True)
class ScanScheduleState:
    enabled: bool
    interval_minutes: int
    next_run: object | None = None


def scan_schedule_state() -> ScanScheduleState:
    schedule = _scan_schedule()
    if schedule is None:
        return ScanScheduleState(
            enabled=False,
            interval_minutes=DEFAULT_SCAN_INTERVAL_MINUTES,
        )

    return ScanScheduleState(
        enabled=True,
        interval_minutes=schedule.minutes or DEFAULT_SCAN_INTERVAL_MINUTES,
        next_run=schedule.next_run,
    )


def update_scan_schedule(*, enabled: bool, interval_minutes: int) -> ScanScheduleState:
    interval_minutes = _validated_interval(interval_minutes)
    schedule = _scan_schedule()

    if not enabled:
        if schedule is not None:
            schedule.delete()
        return ScanScheduleState(enabled=False, interval_minutes=interval_minutes)

    defaults = {
        "func": SCAN_SCHEDULE_FUNC,
        "schedule_type": Schedule.MINUTES,
        "minutes": interval_minutes,
        "repeats": -1,
        "next_run": timezone.now() + timedelta(minutes=interval_minutes),
        "cluster": settings.Q_CLUSTER.get("name"),
    }
    schedule, created = Schedule.objects.get_or_create(
        name=SCAN_SCHEDULE_NAME,
        defaults=defaults,
    )
    if not created:
        for field, value in defaults.items():
            setattr(schedule, field, value)
        schedule.save(update_fields=[*defaults.keys()])

    return ScanScheduleState(
        enabled=True,
        interval_minutes=schedule.minutes or interval_minutes,
        next_run=schedule.next_run,
    )


def _scan_schedule() -> Schedule | None:
    return Schedule.objects.filter(name=SCAN_SCHEDULE_NAME).first()


def _validated_interval(value: int) -> int:
    if value < MIN_SCAN_INTERVAL_MINUTES or value > MAX_SCAN_INTERVAL_MINUTES:
        raise ValueError(f"Scan interval must be between {MIN_SCAN_INTERVAL_MINUTES} and {MAX_SCAN_INTERVAL_MINUTES}.")
    return value

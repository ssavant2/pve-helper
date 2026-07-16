from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django_q.models import Schedule


TRASH_PURGE_SCHEDULE_NAME = "pve-helper automatic trash purge"
TRASH_PURGE_FUNC = "core.tasks.purge_expired_trash"
DEFAULT_TRASH_MAX_AGE_DAYS = 30
MIN_TRASH_MAX_AGE_DAYS = 1
MAX_TRASH_MAX_AGE_DAYS = 99


@dataclass(frozen=True)
class TrashPurgeScheduleState:
    enabled: bool
    max_age_days: int
    next_run: object | None = None


def trash_purge_schedule_state() -> TrashPurgeScheduleState:
    schedule = _trash_purge_schedule()
    if schedule is None:
        return TrashPurgeScheduleState(
            enabled=False,
            max_age_days=DEFAULT_TRASH_MAX_AGE_DAYS,
        )

    max_age_days = _max_age_from_schedule_kwargs(schedule.kwargs)

    return TrashPurgeScheduleState(
        enabled=True,
        max_age_days=max_age_days,
        next_run=schedule.next_run,
    )


def update_trash_purge_schedule(*, enabled: bool, max_age_days: int) -> TrashPurgeScheduleState:
    max_age_days = _validated_max_age(max_age_days)
    schedule = _trash_purge_schedule()

    if not enabled:
        if schedule is not None:
            schedule.delete()
        return TrashPurgeScheduleState(enabled=False, max_age_days=max_age_days)

    defaults = {
        "func": TRASH_PURGE_FUNC,
        "schedule_type": Schedule.DAILY,
        "next_run": timezone.now() + timedelta(hours=1),
        "repeats": -1,
        "kwargs": {"max_age_days": max_age_days},
        "cluster": settings.Q_CLUSTER.get("name"),
    }
    schedule, created = Schedule.objects.get_or_create(
        name=TRASH_PURGE_SCHEDULE_NAME,
        defaults=defaults,
    )
    if not created:
        for field, value in defaults.items():
            setattr(schedule, field, value)
        schedule.save(update_fields=[*defaults.keys()])

    return TrashPurgeScheduleState(
        enabled=True,
        max_age_days=max_age_days,
        next_run=schedule.next_run,
    )


def _trash_purge_schedule() -> Schedule | None:
    return Schedule.objects.filter(name=TRASH_PURGE_SCHEDULE_NAME).first()


def _validated_max_age(value: int) -> int:
    if value < MIN_TRASH_MAX_AGE_DAYS or value > MAX_TRASH_MAX_AGE_DAYS:
        raise ValueError(
            f"Trash max age must be between {MIN_TRASH_MAX_AGE_DAYS} and {MAX_TRASH_MAX_AGE_DAYS} days."
        )
    return value


def _max_age_from_schedule_kwargs(value: object) -> int:
    if isinstance(value, dict):
        raw_value = value.get("max_age_days", DEFAULT_TRASH_MAX_AGE_DAYS)
    elif isinstance(value, str) and value.strip():
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return DEFAULT_TRASH_MAX_AGE_DAYS
        if not isinstance(parsed, dict):
            return DEFAULT_TRASH_MAX_AGE_DAYS
        raw_value = parsed.get("max_age_days", DEFAULT_TRASH_MAX_AGE_DAYS)
    else:
        raw_value = DEFAULT_TRASH_MAX_AGE_DAYS

    try:
        return _validated_max_age(int(raw_value))
    except (TypeError, ValueError):
        return DEFAULT_TRASH_MAX_AGE_DAYS

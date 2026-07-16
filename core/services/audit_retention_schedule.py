from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django_q.models import Schedule


AUDIT_RETENTION_SCHEDULE_NAME = "pve-helper automatic audit retention"
AUDIT_RETENTION_FUNC = "core.tasks.purge_expired_audit_events"
DEFAULT_AUDIT_RETENTION_DAYS = 90
MIN_AUDIT_RETENTION_DAYS = 1
MAX_AUDIT_RETENTION_DAYS = 999


@dataclass(frozen=True)
class AuditRetentionScheduleState:
    enabled: bool
    retention_days: int
    next_run: object | None = None


def audit_retention_schedule_state() -> AuditRetentionScheduleState:
    schedule = _audit_retention_schedule()
    if schedule is None:
        return AuditRetentionScheduleState(
            enabled=False,
            retention_days=DEFAULT_AUDIT_RETENTION_DAYS,
        )

    retention_days = _retention_days_from_schedule_kwargs(schedule.kwargs)
    return AuditRetentionScheduleState(
        enabled=True,
        retention_days=retention_days,
        next_run=schedule.next_run,
    )


def update_audit_retention_schedule(*, enabled: bool, retention_days: int) -> AuditRetentionScheduleState:
    retention_days = _validated_retention_days(retention_days)
    schedule = _audit_retention_schedule()

    if not enabled:
        if schedule is not None:
            schedule.delete()
        return AuditRetentionScheduleState(enabled=False, retention_days=retention_days)

    defaults = {
        "func": AUDIT_RETENTION_FUNC,
        "schedule_type": Schedule.DAILY,
        "next_run": timezone.now() + timedelta(hours=1),
        "repeats": -1,
        "kwargs": {"retention_days": retention_days},
        "cluster": settings.Q_CLUSTER.get("name"),
    }
    schedule, created = Schedule.objects.get_or_create(
        name=AUDIT_RETENTION_SCHEDULE_NAME,
        defaults=defaults,
    )
    if not created:
        for field, value in defaults.items():
            setattr(schedule, field, value)
        schedule.save(update_fields=[*defaults.keys()])

    return AuditRetentionScheduleState(
        enabled=True,
        retention_days=retention_days,
        next_run=schedule.next_run,
    )


def _audit_retention_schedule() -> Schedule | None:
    return Schedule.objects.filter(name=AUDIT_RETENTION_SCHEDULE_NAME).first()


def _validated_retention_days(value: int) -> int:
    if value < MIN_AUDIT_RETENTION_DAYS or value > MAX_AUDIT_RETENTION_DAYS:
        raise ValueError(
            f"Audit retention must be between {MIN_AUDIT_RETENTION_DAYS} and {MAX_AUDIT_RETENTION_DAYS} days."
        )
    return value


def _retention_days_from_schedule_kwargs(value: object) -> int:
    if isinstance(value, dict):
        raw_value = value.get("retention_days", DEFAULT_AUDIT_RETENTION_DAYS)
    elif isinstance(value, str) and value.strip():
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return DEFAULT_AUDIT_RETENTION_DAYS
        if not isinstance(parsed, dict):
            return DEFAULT_AUDIT_RETENTION_DAYS
        raw_value = parsed.get("retention_days", DEFAULT_AUDIT_RETENTION_DAYS)
    else:
        raw_value = DEFAULT_AUDIT_RETENTION_DAYS

    try:
        return _validated_retention_days(int(raw_value))
    except (TypeError, ValueError):
        return DEFAULT_AUDIT_RETENTION_DAYS

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import DAILY, MONTHLY, WEEKLY, MO, TU, WE, TH, FR, SA, SU, rrule, rrulestr
from django.utils import timezone

from core.models import ScheduledAction


WEEKDAYS = [MO, TU, WE, TH, FR, SA, SU]
WEEKDAY_NAMES = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}
ORDINALS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "last": -1,
}


@dataclass(frozen=True)
class RecurrenceError(ValueError):
    message: str

    def __str__(self) -> str:
        return self.message


def next_run_after(action: ScheduledAction, *, after: datetime | None = None) -> datetime | None:
    if action.schedule_type == ScheduledAction.ScheduleType.ONCE:
        return action.run_at or action.next_run_at
    if action.schedule_type != ScheduledAction.ScheduleType.RECURRING:
        return None

    after = after or timezone.now()
    local_tz = _schedule_timezone(action.timezone)
    after_local = after.astimezone(local_tz)
    recurrence = action.recurrence if isinstance(action.recurrence, dict) else {}
    kind = action.recurrence_kind

    if kind == ScheduledAction.RecurrenceKind.DAILY:
        rule = _daily_rule(recurrence, after_local)
    elif kind == ScheduledAction.RecurrenceKind.WEEKLY:
        rule = _weekly_rule(recurrence, after_local)
    elif kind == ScheduledAction.RecurrenceKind.MONTHLY_ORDINAL:
        rule = _monthly_ordinal_rule(recurrence, after_local)
    elif kind == ScheduledAction.RecurrenceKind.MONTHLY_DAY:
        rule = _monthly_day_rule(recurrence, after_local)
    elif kind == ScheduledAction.RecurrenceKind.ADVANCED:
        rule = _advanced_rule(recurrence, after_local)
    else:
        raise RecurrenceError(f"Unsupported recurrence kind: {kind}")

    next_local = rule.after(after_local, inc=False)
    if next_local is None:
        return None
    return next_local.astimezone(timezone.UTC)


def _daily_rule(recurrence: dict[str, Any], dtstart: datetime):
    hour, minute = _time_parts(recurrence)
    return rrule(DAILY, dtstart=dtstart, bymonth=_month_numbers(recurrence), byhour=hour, byminute=minute, bysecond=0)


def _weekly_rule(recurrence: dict[str, Any], dtstart: datetime):
    hour, minute = _time_parts(recurrence)
    weekdays = recurrence.get("weekdays", recurrence.get("weekday"))
    if weekdays is None:
        weekdays = [dtstart.weekday()]
    if not isinstance(weekdays, list):
        weekdays = [weekdays]
    byweekday = [WEEKDAYS[_weekday_number(day)] for day in weekdays]
    return rrule(
        WEEKLY,
        dtstart=dtstart,
        bymonth=_month_numbers(recurrence),
        byweekday=byweekday,
        byhour=hour,
        byminute=minute,
        bysecond=0,
    )


def _monthly_ordinal_rule(recurrence: dict[str, Any], dtstart: datetime):
    hour, minute = _time_parts(recurrence)
    ordinals = _list_values(recurrence.get("ordinals", recurrence.get("ordinal", recurrence.get("week", "first"))))
    weekdays = _list_values(recurrence.get("weekdays", recurrence.get("weekday", "monday")))
    byweekday = [
        WEEKDAYS[_weekday_number(weekday)](_ordinal_number(ordinal))
        for ordinal in ordinals
        for weekday in weekdays
    ]
    return rrule(
        MONTHLY,
        dtstart=dtstart,
        bymonth=_month_numbers(recurrence),
        byweekday=byweekday,
        byhour=hour,
        byminute=minute,
        bysecond=0,
    )


def _monthly_day_rule(recurrence: dict[str, Any], dtstart: datetime):
    hour, minute = _time_parts(recurrence)
    days = _list_values(recurrence.get("days_of_month", recurrence.get("day", recurrence.get("day_of_month"))))
    bymonthday = []
    for day in days:
        try:
            day = int(day)
        except (TypeError, ValueError) as exc:
            raise RecurrenceError("Monthly day recurrence requires day_of_month 1-31.") from exc
        if day < 1 or day > 31:
            raise RecurrenceError("Monthly day recurrence requires day_of_month 1-31.")
        bymonthday.append(day)
    return rrule(
        MONTHLY,
        dtstart=dtstart,
        bymonth=_month_numbers(recurrence),
        bymonthday=bymonthday,
        byhour=hour,
        byminute=minute,
        bysecond=0,
    )


def _month_numbers(recurrence: dict[str, Any]) -> list[int] | None:
    raw_months = recurrence.get("months")
    if raw_months in (None, "", []):
        return None
    months = []
    for month in _list_values(raw_months):
        try:
            month_int = int(month)
        except (TypeError, ValueError) as exc:
            raise RecurrenceError(f"Unknown month: {month}") from exc
        if month_int < 1 or month_int > 12:
            raise RecurrenceError("Month must be between 1 and 12.")
        months.append(month_int)
    return months


def _list_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str) and "," in value:
        return [part.strip() for part in value.split(",") if part.strip()]
    return [value]


def _advanced_rule(recurrence: dict[str, Any], dtstart: datetime):
    raw_rule = str(recurrence.get("rrule", "")).strip()
    if not raw_rule:
        raise RecurrenceError("Advanced recurrence requires an RRULE value.")
    try:
        return rrulestr(raw_rule, dtstart=dtstart)
    except (TypeError, ValueError) as exc:
        raise RecurrenceError("Advanced recurrence RRULE is invalid.") from exc


def _schedule_timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value or "UTC")
    except ZoneInfoNotFoundError as exc:
        raise RecurrenceError(f"Unknown timezone: {value}") from exc


def _time_parts(recurrence: dict[str, Any]) -> tuple[int, int]:
    raw_time = recurrence.get("time")
    if isinstance(raw_time, str) and raw_time:
        parts = raw_time.split(":")
        if len(parts) < 2:
            raise RecurrenceError("Recurrence time must use HH:MM format.")
        hour = parts[0]
        minute = parts[1]
    else:
        hour = recurrence.get("hour", 0)
        minute = recurrence.get("minute", 0)

    try:
        hour_int = int(hour)
        minute_int = int(minute)
    except (TypeError, ValueError) as exc:
        raise RecurrenceError("Recurrence time must contain numeric hour and minute.") from exc

    if hour_int < 0 or hour_int > 23 or minute_int < 0 or minute_int > 59:
        raise RecurrenceError("Recurrence time must be between 00:00 and 23:59.")
    return hour_int, minute_int


def _weekday_number(value: Any) -> int:
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in WEEKDAY_NAMES:
            return WEEKDAY_NAMES[cleaned]
        try:
            value = int(cleaned)
        except ValueError as exc:
            raise RecurrenceError(f"Unknown weekday: {value}") from exc
    try:
        weekday = int(value)
    except (TypeError, ValueError) as exc:
        raise RecurrenceError(f"Unknown weekday: {value}") from exc
    if weekday < 0 or weekday > 6:
        raise RecurrenceError("Weekday must be 0-6, where Monday is 0.")
    return weekday


def _ordinal_number(value: Any) -> int:
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in ORDINALS:
            return ORDINALS[cleaned]
        try:
            value = int(cleaned)
        except ValueError as exc:
            raise RecurrenceError(f"Unknown monthly ordinal: {value}") from exc
    try:
        ordinal = int(value)
    except (TypeError, ValueError) as exc:
        raise RecurrenceError(f"Unknown monthly ordinal: {value}") from exc
    if ordinal not in {-1, 1, 2, 3, 4, 5}:
        raise RecurrenceError("Monthly ordinal must be 1-5 or last.")
    return ordinal

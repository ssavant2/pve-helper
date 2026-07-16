from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django_q.models import Schedule


GUEST_INVENTORY_REFRESH_SCHEDULE_NAME = "pve-helper current guest inventory refresh"
GUEST_INVENTORY_REFRESH_FUNC = "core.tasks.refresh_current_guest_inventory"


def ensure_guest_inventory_refresh_schedule() -> Schedule:
    interval = max(1, settings.CURRENT_GUEST_REFRESH_INTERVAL_MINUTES)
    defaults = {
        "func": GUEST_INVENTORY_REFRESH_FUNC,
        "schedule_type": Schedule.MINUTES,
        "minutes": interval,
        "repeats": -1,
        "next_run": timezone.now() + timedelta(seconds=10),
        "cluster": settings.Q_CLUSTER.get("name"),
    }
    schedule, created = Schedule.objects.get_or_create(
        name=GUEST_INVENTORY_REFRESH_SCHEDULE_NAME,
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

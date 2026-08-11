from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django_q.models import Schedule

HOST_PROJECTION_REFRESH_SCHEDULE_NAME = "pve-helper host projection refresh"
HOST_PROJECTION_REFRESH_FUNC = "core.tasks.refresh_cluster_host_projection"


def ensure_host_projection_refresh_schedule() -> Schedule:
    """Register the Module 5 host-projection cadence.

    Deliberately the same default interval as the guest projection: the two feed
    panels that sit on one page, and a different cadence would let them disagree
    about what "fresh" means for the same cluster.
    """
    interval = max(1, settings.HOST_PROJECTION_REFRESH_INTERVAL_MINUTES)
    defaults = {
        "func": HOST_PROJECTION_REFRESH_FUNC,
        "schedule_type": Schedule.MINUTES,
        "minutes": interval,
        "repeats": -1,
        "next_run": timezone.now() + timedelta(seconds=20),
        "cluster": settings.Q_CLUSTER.get("name"),
    }
    schedule, created = Schedule.objects.get_or_create(
        name=HOST_PROJECTION_REFRESH_SCHEDULE_NAME,
        defaults=defaults,
    )
    if created:
        return schedule
    updates = {key: value for key, value in defaults.items() if key != "next_run" and getattr(schedule, key) != value}
    if updates:
        for field, value in updates.items():
            setattr(schedule, field, value)
        schedule.save(update_fields=[*updates.keys()])
    return schedule

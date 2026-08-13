from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django_q.models import Schedule

HOST_PROJECTION_REFRESH_SCHEDULE_NAME = "pve-helper host projection refresh"
HOST_PROJECTION_REFRESH_FUNC = "core.tasks.refresh_cluster_host_projection"

NODE_NETWORK_REFRESH_SCHEDULE_NAME = "pve-helper node network refresh"
NODE_NETWORK_REFRESH_FUNC = "core.tasks.refresh_node_network_projection"


def ensure_host_projection_refresh_schedule() -> Schedule:
    """Register the Module 5 host-projection cadence.

    Deliberately the same default interval as the guest projection: the two feed
    panels that sit on one page, and a different cadence would let them disagree
    about what "fresh" means for the same cluster.
    """
    return _ensure_schedule(
        HOST_PROJECTION_REFRESH_SCHEDULE_NAME,
        HOST_PROJECTION_REFRESH_FUNC,
        max(1, settings.HOST_PROJECTION_REFRESH_INTERVAL_MINUTES),
        # A few seconds after boot: the panels it feeds are on the landing page.
        delay_seconds=20,
    )


def ensure_node_network_refresh_schedule() -> Schedule:
    """Register 5a4B-i's cadence, separately from the host projection's.

    A separate schedule *and* a separate lock. Sharing either would let a slow
    network pass skip a membership/node-runtime cycle, and this domain deliberately
    runs far less often -- interfaces change on operator action.

    The first run is delayed past the host projection's because a node with no
    ``ClusterNodeState`` row is a zero-call refusal here; starting after membership
    has had a chance to publish avoids a whole first pass spent refusing.
    """
    return _ensure_schedule(
        NODE_NETWORK_REFRESH_SCHEDULE_NAME,
        NODE_NETWORK_REFRESH_FUNC,
        max(1, settings.NODE_NETWORK_REFRESH_INTERVAL_MINUTES),
        delay_seconds=90,
    )


def _ensure_schedule(name: str, func: str, interval: int, *, delay_seconds: int) -> Schedule:
    defaults = {
        "func": func,
        "schedule_type": Schedule.MINUTES,
        "minutes": interval,
        "repeats": -1,
        "next_run": timezone.now() + timedelta(seconds=delay_seconds),
        "cluster": settings.Q_CLUSTER.get("name"),
    }
    schedule, created = Schedule.objects.get_or_create(name=name, defaults=defaults)
    if created:
        return schedule
    updates = {key: value for key, value in defaults.items() if key != "next_run" and getattr(schedule, key) != value}
    if updates:
        for field, value in updates.items():
            setattr(schedule, field, value)
        schedule.save(update_fields=[*updates.keys()])
    return schedule

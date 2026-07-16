from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from core.models import ConsoleSession


def prune_console_sessions(*, now=None, retention_hours: int | None = None) -> dict[str, int]:
    """Expire abandoned one-time console sessions and retain terminal metadata briefly.

    The console gateway clears credentials on consumption. This covers tokens that
    were never consumed, then removes terminal session metadata after the short
    operational retention window. AuditEvent remains the durable audit trail.
    """
    now = now or timezone.now()
    if retention_hours is None:
        retention_hours = settings.CONSOLE_SESSION_RETENTION_HOURS
    retention_hours = max(int(retention_hours), 1)

    expired = ConsoleSession.objects.filter(
        status=ConsoleSession.Status.PENDING,
        expires_at__lt=now,
    ).update(
        status=ConsoleSession.Status.EXPIRED,
        closed_at=now,
        close_reason="expired",
        proxmox_ticket="",
        proxmox_password="",
        updated_at=now,
    )

    cutoff = now - timedelta(hours=retention_hours)
    terminal = ConsoleSession.objects.filter(
        status__in=[
            ConsoleSession.Status.CLOSED,
            ConsoleSession.Status.FAILED,
            ConsoleSession.Status.EXPIRED,
        ]
    ).filter(Q(closed_at__lt=cutoff) | Q(closed_at__isnull=True, created_at__lt=cutoff))
    deleted, _ = terminal.delete()
    return {"expired": expired, "deleted": deleted}

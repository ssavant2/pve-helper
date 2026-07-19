from __future__ import annotations

from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.db.models.signals import post_migrate, post_save, pre_delete
from django.dispatch import receiver

from .services.audit_events import record_audit_event
from .services.bulk_task_reaper_schedule import ensure_bulk_task_reaper_schedule
from .services.console_session_cleanup_schedule import ensure_console_session_cleanup_schedule
from .services.guest_task_reaper_schedule import ensure_guest_task_reaper_schedule
from .services.guest_inventory_refresh_schedule import ensure_guest_inventory_refresh_schedule
from .services.scheduled_actions import ensure_scheduled_action_dispatch_schedule
from .services.space_snapshot_schedule import ensure_space_snapshot_schedule
from .services.storage_catalog_refresh_schedule import ensure_storage_catalog_refresh_schedules
from .models import ProxmoxCluster
from .services.cluster_state_identity import invalidate_cluster_cache


@receiver(post_save, sender=ProxmoxCluster)
def invalidate_disabled_cluster_cache(sender, instance, **kwargs):
    if not instance.enabled:
        invalidate_cluster_cache(instance)


@receiver(pre_delete, sender=ProxmoxCluster)
def invalidate_deleted_cluster_cache(sender, instance, **kwargs):
    invalidate_cluster_cache(instance)


@receiver(post_migrate)
def ensure_always_on_schedules(sender, app_config, **kwargs):
    if app_config.name != "core":
        return
    ensure_space_snapshot_schedule()
    ensure_scheduled_action_dispatch_schedule()
    ensure_guest_task_reaper_schedule()
    ensure_guest_inventory_refresh_schedule()
    ensure_console_session_cleanup_schedule()
    ensure_bulk_task_reaper_schedule()
    ensure_storage_catalog_refresh_schedules()


@receiver(user_logged_in)
def audit_user_login(sender, request, user, **kwargs):
    username = user.get_username()
    record_audit_event(
        request=request,
        user=user,
        username=username,
        action="auth.login",
        object_type="user",
        object_id=username,
        outcome="success",
        details={"path": request.path if request else ""},
    )


@receiver(user_logged_out)
def audit_user_logout(sender, request, user, **kwargs):
    username = user.get_username() if user else ""
    record_audit_event(
        request=request,
        user=user if user and user.is_authenticated else None,
        username=username,
        action="auth.logout",
        object_type="user",
        object_id=username,
        outcome="success",
        details={"path": request.path if request else ""},
    )


@receiver(user_login_failed)
def audit_user_login_failed(sender, credentials, request, **kwargs):
    username = _credential_username(credentials)
    record_audit_event(
        request=request,
        username=username,
        action="auth.login_failed",
        object_type="user",
        object_id=username,
        outcome="failed",
        details={"path": request.path if request else ""},
    )


def _credential_username(credentials) -> str:
    if not isinstance(credentials, dict):
        return ""

    for key in ("username", "email", "preferred_username"):
        value = credentials.get(key)
        if value:
            return str(value)
    return ""

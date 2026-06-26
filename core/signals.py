from __future__ import annotations

from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from .models import AuditEvent


@receiver(user_logged_in)
def audit_user_login(sender, request, user, **kwargs):
    username = user.get_username()
    AuditEvent.objects.create(
        user=user,
        username=username,
        source_ip=_client_ip(request),
        action="auth.login",
        object_type="user",
        object_id=username,
        outcome="success",
        details={"path": request.path if request else ""},
    )


@receiver(user_logged_out)
def audit_user_logout(sender, request, user, **kwargs):
    username = user.get_username() if user else ""
    AuditEvent.objects.create(
        user=user if user and user.is_authenticated else None,
        username=username,
        source_ip=_client_ip(request),
        action="auth.logout",
        object_type="user",
        object_id=username,
        outcome="success",
        details={"path": request.path if request else ""},
    )


@receiver(user_login_failed)
def audit_user_login_failed(sender, credentials, request, **kwargs):
    username = _credential_username(credentials)
    AuditEvent.objects.create(
        username=username,
        source_ip=_client_ip(request),
        action="auth.login_failed",
        object_type="user",
        object_id=username,
        outcome="failed",
        details={"path": request.path if request else ""},
    )


def _client_ip(request) -> str | None:
    if request is None:
        return None

    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


def _credential_username(credentials) -> str:
    if not isinstance(credentials, dict):
        return ""

    for key in ("username", "email", "preferred_username"):
        value = credentials.get(key)
        if value:
            return str(value)
    return ""

from __future__ import annotations

import secrets
from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone

from core.models import IntegrationToken


def issue_token(name: str, *, expires_at=None) -> tuple[IntegrationToken, str]:
    token_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    token = IntegrationToken.objects.create(
        token_id=token_id,
        name=name,
        secret_hash=make_password(secret),
        expires_at=expires_at,
    )
    return token, f"{token_id}.{secret}"


def authenticate_token(raw: str) -> IntegrationToken | None:
    token_id, separator, secret = str(raw or "").partition(".")
    if not separator or not token_id or not secret:
        return None
    token = IntegrationToken.objects.filter(token_id=token_id, revoked_at__isnull=True).first()
    now = timezone.now()
    if token is None or (token.expires_at and token.expires_at <= now):
        return None
    if not check_password(secret, token.secret_hash):
        return None
    if token.last_used_at is None or token.last_used_at < now - timedelta(hours=1):
        IntegrationToken.objects.filter(pk=token.pk).update(last_used_at=now)
        token.last_used_at = now
    return token

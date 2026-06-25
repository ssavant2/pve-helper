from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from mozilla_django_oidc.auth import OIDCAuthenticationBackend

logger = logging.getLogger(__name__)


class PveHelperOIDCBackend(OIDCAuthenticationBackend):
    """Authentik OIDC backend with explicit group enforcement."""

    def verify_claims(self, claims: dict[str, Any]) -> bool:
        if not super().verify_claims(claims):
            return False

        required_group = settings.OIDC_REQUIRED_GROUP
        groups = claims.get("groups") or []
        if isinstance(groups, str):
            groups = [groups]

        if required_group and required_group not in groups:
            logger.warning("OIDC login denied; missing required group %s", required_group)
            return False

        return True

    def filter_users_by_claims(self, claims: dict[str, Any]):
        subject = claims.get("sub")
        username = self._username_from_claims(claims)
        User = get_user_model()

        if subject:
            users = User.objects.filter(username=username)
            if users.exists():
                return users

        return User.objects.none()

    def create_user(self, claims: dict[str, Any]):
        User = get_user_model()
        username = self._username_from_claims(claims)
        email = claims.get("email", "")
        user = User.objects.create_user(username=username, email=email)
        return self.update_user(user, claims)

    def update_user(self, user, claims: dict[str, Any]):
        user.email = claims.get("email", user.email)
        user.first_name = claims.get("given_name", user.first_name)
        user.last_name = claims.get("family_name", user.last_name)
        user.is_staff = True
        user.is_superuser = True
        user.save()
        return user

    def _username_from_claims(self, claims: dict[str, Any]) -> str:
        username = (
            claims.get("preferred_username")
            or claims.get("email")
            or claims.get("name")
            or claims.get("sub")
            or "oidc-user"
        )
        return str(username)[:150]

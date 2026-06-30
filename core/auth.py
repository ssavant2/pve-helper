from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction
from mozilla_django_oidc.auth import OIDCAuthenticationBackend

from .models import OidcIdentity

logger = logging.getLogger(__name__)


class PveHelperOIDCBackend(OIDCAuthenticationBackend):
    """Authentik OIDC backend with explicit group enforcement."""

    def verify_claims(self, claims: dict[str, Any]) -> bool:
        if not super().verify_claims(claims):
            return False

        if not claims.get("sub"):
            logger.warning("OIDC login denied; missing subject claim")
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
        subject = self._subject_from_claims(claims)
        User = get_user_model()

        if subject:
            users = User.objects.filter(
                pve_helper_oidc_identities__issuer=self._issuer(),
                pve_helper_oidc_identities__subject=subject,
            )
            if users.exists():
                return users

            username = self._username_from_claims(claims)
            return User.objects.filter(
                username=username,
                pve_helper_oidc_identities__isnull=True,
            )

        return User.objects.none()

    @transaction.atomic
    def create_user(self, claims: dict[str, Any]):
        User = get_user_model()
        username = self._available_username(self._username_from_claims(claims))
        email = claims.get("email", "")
        user = User.objects.create_user(username=username, email=email)
        return self.update_user(user, claims)

    @transaction.atomic
    def update_user(self, user, claims: dict[str, Any]):
        self._ensure_identity(user, claims)
        self._update_username(user, claims)
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

    def _subject_from_claims(self, claims: dict[str, Any]) -> str:
        return str(claims.get("sub") or "")[:255]

    def _issuer(self) -> str:
        return settings.OIDC_ISSUER_URL

    def _available_username(self, username: str) -> str:
        User = get_user_model()
        base = username[:145] or "oidc-user"
        candidate = base[:150]
        counter = 2
        while User.objects.filter(username=candidate).exists():
            suffix = f"-{counter}"
            candidate = f"{base[:150 - len(suffix)]}{suffix}"
            counter += 1
        return candidate

    def _update_username(self, user, claims: dict[str, Any]) -> None:
        username = self._username_from_claims(claims)
        if username == user.username:
            return
        User = get_user_model()
        if not User.objects.exclude(pk=user.pk).filter(username=username).exists():
            user.username = username

    def _ensure_identity(self, user, claims: dict[str, Any]) -> OidcIdentity:
        subject = self._subject_from_claims(claims)
        if not subject:
            raise ValueError("OIDC subject claim is required.")
        identity, _created = OidcIdentity.objects.get_or_create(
            issuer=self._issuer(),
            subject=subject,
            defaults={"user": user},
        )
        if identity.user_id != user.id:
            logger.warning("OIDC login denied; subject is already linked to another user")
            raise PermissionDenied("OIDC subject is already linked to a different user.")
        return identity


def provider_logout(request) -> str:
    """Build the Authentik RP-initiated logout (end-session) URL.

    Used as ``OIDC_OP_LOGOUT_URL_METHOD``. Ending only the local Django session is
    not enough: the Authentik SSO session would silently re-authenticate the user on
    the next protected request. Passing ``id_token_hint`` lets Authentik end the
    session without a confirmation prompt; ``post_logout_redirect_uri`` returns the
    user to the app (it must be registered in the provider's redirect URIs).
    """
    end_session = getattr(settings, "OIDC_OP_END_SESSION_ENDPOINT", "")
    if not end_session:
        return settings.LOGOUT_REDIRECT_URL

    params = {"post_logout_redirect_uri": f"{settings.APP_BASE_URL}/"}
    id_token = request.session.get("oidc_id_token")
    if id_token:
        params["id_token_hint"] = id_token
    return f"{end_session}?{urlencode(params)}"

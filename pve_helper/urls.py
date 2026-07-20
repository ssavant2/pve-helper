"""Top-level URL routing for pve-helper."""

from django.conf import settings
from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.urls import include, path
from mozilla_django_oidc.views import (
    OIDCAuthenticationCallbackView,
    OIDCAuthenticationRequestView,
    OIDCLogoutView,
)

if settings.APP_REQUIRE_LOGIN:
    # Route /admin/ through the same OIDC flow as the rest of the site.
    # instead of Django admin's standalone username/password form. Must run before
    # admin.site.urls is built below.
    admin.site.login = login_required(admin.site.login)

urlpatterns = [
    path("", include("core.urls")),
    path("admin/", admin.site.urls),
    path(
        "auth/oidc/login/",
        OIDCAuthenticationRequestView.as_view(),
        name="oidc_authentication_init",
    ),
    path(
        "auth/oidc/callback",
        OIDCAuthenticationCallbackView.as_view(),
        name="oidc_authentication_callback",
    ),
    path("auth/logout/", OIDCLogoutView.as_view(), name="logout"),
]

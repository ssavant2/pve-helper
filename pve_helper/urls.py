"""Top-level URL routing for pve-helper."""

from django.contrib import admin
from django.urls import include, path
from mozilla_django_oidc.views import (
    OIDCAuthenticationCallbackView,
    OIDCAuthenticationRequestView,
    OIDCLogoutView,
)

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

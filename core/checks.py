from __future__ import annotations

import sys
from urllib.parse import urlparse

from django.conf import settings
from django.core.checks import Error, register


DEV_SECRET_KEY = "dev-insecure-change-me"
DEV_HOSTS = {"*", ".localhost"}
DEV_BASE_HOSTS = {"localhost", "127.0.0.1", "pve-helper.example.com"}


@register()
def production_startup_checks(app_configs, **kwargs):
    if "test" in sys.argv:
        return []
    return production_startup_errors()


def production_startup_errors():
    if settings.DEBUG:
        return []

    errors = []

    if settings.SECRET_KEY == DEV_SECRET_KEY:
        errors.append(
            Error(
                "APP_SECRET_KEY is still the development default while DEBUG=false.",
                hint="Set APP_SECRET_KEY to a long random value before production-like deployment.",
                id="pve_helper.E001",
            )
        )

    allowed_hosts = set(settings.ALLOWED_HOSTS or [])
    if not allowed_hosts:
        errors.append(
            Error(
                "ALLOWED_HOSTS is empty while DEBUG=false.",
                hint="Set ALLOWED_HOSTS to the hostnames that should serve pve-helper.",
                id="pve_helper.E002",
            )
        )
    elif allowed_hosts & DEV_HOSTS:
        errors.append(
            Error(
                "ALLOWED_HOSTS contains a wildcard/development host while DEBUG=false.",
                hint="Replace wildcard hosts with explicit production hostnames.",
                id="pve_helper.E003",
            )
        )

    base_url = getattr(settings, "APP_BASE_URL", "")
    parsed_base = urlparse(base_url)
    if parsed_base.scheme not in {"http", "https"}:
        errors.append(
            Error(
                "APP_BASE_URL must use http or https when DEBUG=false.",
                hint="Set APP_BASE_URL to the external URL registered in your OIDC provider.",
                id="pve_helper.E004",
            )
        )
    if parsed_base.hostname in DEV_BASE_HOSTS:
        errors.append(
            Error(
                "APP_BASE_URL still points at a development/example host while DEBUG=false.",
                hint="Set APP_BASE_URL to this deployment's real external hostname.",
                id="pve_helper.E005",
            )
        )

    if getattr(settings, "APP_REQUIRE_LOGIN", True):
        if not getattr(settings, "OIDC_RP_CLIENT_ID", ""):
            errors.append(
                Error(
                    "OIDC_CLIENT_ID is required when APP_REQUIRE_LOGIN=true and DEBUG=false.",
                    hint="Configure the OIDC client before exposing pve-helper.",
                    id="pve_helper.E006",
                )
            )
        if not getattr(settings, "OIDC_RP_CLIENT_SECRET", ""):
            errors.append(
                Error(
                    "OIDC_CLIENT_SECRET is required when APP_REQUIRE_LOGIN=true and DEBUG=false.",
                    hint="Configure the OIDC client secret before exposing pve-helper.",
                    id="pve_helper.E007",
                )
            )

    return errors

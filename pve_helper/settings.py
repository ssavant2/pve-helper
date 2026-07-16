"""Django settings for pve-helper."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent.parent


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def env_list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def external_url_uses_https(value: str) -> bool:
    return urlparse(value).scheme.lower() == "https"


SECRET_KEY = env("APP_SECRET_KEY", "dev-insecure-change-me")
DEBUG = env_bool("DEBUG", True)
APP_REQUIRE_LOGIN = env_bool("APP_REQUIRE_LOGIN", True)
APP_BASE_URL = env("APP_BASE_URL", "https://pve-helper.example.com").rstrip("/")
APP_VERSION = env("APP_VERSION", "DEV")

ALLOWED_HOSTS = env_list(
    "ALLOWED_HOSTS",
    "pve-helper.example.com,localhost,127.0.0.1",
)
CSRF_TRUSTED_ORIGINS = env_list(
    "CSRF_TRUSTED_ORIGINS",
    "https://pve-helper.example.com,http://localhost:21080",
)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_q",
    "mozilla_django_oidc",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "pve_helper.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.app_settings",
            ],
        },
    },
]

WSGI_APPLICATION = "pve_helper.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", "pve_helper"),
        "USER": env("DB_USER", "pve_helper"),
        "PASSWORD": env("DB_PASSWORD", "pve_helper_dev_password"),
        "HOST": env("DB_HOST", "localhost"),
        "PORT": env("DB_PORT", "5432"),
        "CONN_MAX_AGE": 60,
    }
}

# Dev-only escape hatch: a throwaway SQLite file needing no Postgres server or
# CREATEDB role. Used by the Playwright E2E stack (docker-compose.tools.yml).
# Never set DB_ENGINE=sqlite in production — the deploy compose does not.
if env("DB_ENGINE", "") == "sqlite":
    DATABASES["default"] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": env("DB_NAME", "/tmp/e2e/db.sqlite3"),
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TZ", "Europe/Stockholm")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/auth/oidc/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"

# Production is OIDC-only: Authentik is the sole way in, Django admin included.
# The username/password backend (admin login form, createsuperuser) is kept only
# for the dev/E2E path, where login is not the enforced OIDC gate.
AUTHENTICATION_BACKENDS = ["core.auth.PveHelperOIDCBackend"]
if not APP_REQUIRE_LOGIN:
    AUTHENTICATION_BACKENDS.append("django.contrib.auth.backends.ModelBackend")

OIDC_ISSUER_URL = env("OIDC_ISSUER_URL", "https://auth.example.com/application/o/pve-helper/").rstrip("/")
OIDC_RP_CLIENT_ID = env("OIDC_CLIENT_ID", "")
OIDC_RP_CLIENT_SECRET = env("OIDC_CLIENT_SECRET", "")
OIDC_RP_SCOPES = env("OIDC_SCOPES", "openid profile email groups")
OIDC_REQUIRED_GROUP = env("OIDC_REQUIRED_GROUP", "pve-helper-admins")
OIDC_CREATE_USER = True
# mozilla-django-oidc does NOT perform OIDC discovery (.well-known); the OP endpoints
# must be set explicitly. Defaults below follow Authentik's URL scheme and are derived
# from the issuer URL. authorize/token/userinfo are global under /application/o/, while
# jwks is per-application (includes the provider slug). Each is overridable via env.
_oidc_o_base = OIDC_ISSUER_URL.rsplit("/", 1)[0]  # e.g. https://auth.example.com/application/o
OIDC_OP_AUTHORIZATION_ENDPOINT = env("OIDC_OP_AUTHORIZATION_ENDPOINT", f"{_oidc_o_base}/authorize/")
OIDC_OP_TOKEN_ENDPOINT = env("OIDC_OP_TOKEN_ENDPOINT", f"{_oidc_o_base}/token/")
OIDC_OP_USER_ENDPOINT = env("OIDC_OP_USER_ENDPOINT", f"{_oidc_o_base}/userinfo/")
OIDC_OP_JWKS_ENDPOINT = env("OIDC_OP_JWKS_ENDPOINT", f"{OIDC_ISSUER_URL}/jwks/")
OIDC_OP_END_SESSION_ENDPOINT = env("OIDC_OP_END_SESSION_ENDPOINT", f"{OIDC_ISSUER_URL}/end-session/")
OIDC_RP_SIGN_ALGO = "RS256"

# RP-initiated logout: also end the Authentik SSO session, otherwise local logout is
# immediately undone by silent re-authentication. Storing the id token lets us pass
# id_token_hint so Authentik can log out without an extra confirmation prompt.
OIDC_STORE_ID_TOKEN = True
OIDC_OP_LOGOUT_URL_METHOD = "core.auth.provider_logout"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = external_url_uses_https(APP_BASE_URL)
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = external_url_uses_https(APP_BASE_URL)
CSRF_COOKIE_SAMESITE = "Lax"

USE_X_FORWARDED_HOST = env_bool("USE_X_FORWARDED_HOST", True)
if env_bool("SECURE_PROXY_SSL_HEADER", True):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

PVE_ENDPOINTS = env_list("PVE_ENDPOINTS", "https://pve-node-1.example.com:8006")
PVE_VERIFY_TLS = env_bool("PVE_VERIFY_TLS", True)
PVE_CA_BUNDLE = env("PVE_CA_BUNDLE", "")
PVE_API_TOKEN_ID = env("PVE_API_TOKEN_ID", "")
PVE_API_TOKEN_SECRET = env("PVE_API_TOKEN_SECRET", "")
# Only enabled by pve_helper.test_settings: turns an unmocked Proxmox request
# into an immediate test failure instead of a real infrastructure call.
PVE_TEST_NETWORK_DISABLED = env_bool("PVE_TEST_NETWORK_DISABLED", False)
PVE_EXPECTED_CONSUMERS = env_list("PVE_EXPECTED_CONSUMERS", "pve-node-1")
SCHEDULED_ACTIONS_ENABLED = env_bool("SCHEDULED_ACTIONS_ENABLED", True)
SCHEDULED_ACTION_TIMEOUT_SECONDS = env_int("SCHEDULED_ACTION_TIMEOUT_SECONDS", 1800)
# Backups and restores can legitimately run far longer than a power action.
# Keep this independently configurable without weakening scheduled-action timeouts.
BACKUP_TASK_TIMEOUT_SECONDS = env_int("BACKUP_TASK_TIMEOUT_SECONDS", 21600)
SCHEDULED_ACTION_POLL_INTERVAL_SECONDS = env_int("SCHEDULED_ACTION_POLL_INTERVAL_SECONDS", 5)
SCHEDULED_ACTION_RUN_RETENTION_DAYS = env_int("SCHEDULED_ACTION_RUN_RETENTION_DAYS", 90)

TRUENAS_FS_STORAGE_ID = env("TRUENAS_FS_STORAGE_ID", "nfs-fs")
TRUENAS_VM_STORAGE_ID = env("TRUENAS_VM_STORAGE_ID", "nfs-vm")
TRUENAS_FS_EXPORT = env("TRUENAS_FS_EXPORT", "")
TRUENAS_VM_EXPORT = env("TRUENAS_VM_EXPORT", "")
TRUENAS_FS_CONTAINER_PATH = env("TRUENAS_FS_CONTAINER_PATH", "/storages/truenas-fs")
TRUENAS_VM_CONTAINER_PATH = env("TRUENAS_VM_CONTAINER_PATH", "/storages/truenas-vm")
STORAGE_WRITE_ENABLED = env_bool("STORAGE_WRITE_ENABLED", True)
CONSOLE_ENABLED = env_bool("CONSOLE_ENABLED", True)
CONSOLE_SESSION_TTL_SECONDS = env_int("CONSOLE_SESSION_TTL_SECONDS", 30)
CONSOLE_CONNECT_TIMEOUT_SECONDS = env_int("CONSOLE_CONNECT_TIMEOUT_SECONDS", 10)
# Terminal console-session metadata is short-lived; credentials are cleared as
# soon as a session is consumed or expires.
CONSOLE_SESSION_RETENTION_HOURS = env_int("CONSOLE_SESSION_RETENTION_HOURS", 24)
STORAGE_UPLOAD_MAX_SIZE_MB = env_int("STORAGE_UPLOAD_MAX_SIZE_MB", 0)
FILE_UPLOAD_TEMP_DIR = env("FILE_UPLOAD_TEMP_DIR", "") or None
STORAGE_IMAGE_INFO_ENABLED = env_bool("STORAGE_IMAGE_INFO_ENABLED", True)
STORAGE_IMAGE_INFO_TIMEOUT_SECONDS = env_int("STORAGE_IMAGE_INFO_TIMEOUT_SECONDS", 15)
STORAGE_INFLATE_TIMEOUT_SECONDS = env_int("STORAGE_INFLATE_TIMEOUT_SECONDS", 14400)
STORAGE_INFLATE_WORKER_PRESERVES_OWNER = env_bool("STORAGE_INFLATE_WORKER_PRESERVES_OWNER", False)
SCAN_TASK_TIMEOUT_SECONDS = env_int("SCAN_TASK_TIMEOUT_SECONDS", 21600)
STORAGE_DOWNLOAD_ACCEL_ENABLED = env_bool("STORAGE_DOWNLOAD_ACCEL_ENABLED", False)
STORAGE_DOWNLOAD_ACCEL_PREFIX = env("STORAGE_DOWNLOAD_ACCEL_PREFIX", "/_pve_helper_download")
CURRENT_GUEST_REFRESH_INTERVAL_MINUTES = env_int("CURRENT_GUEST_REFRESH_INTERVAL_MINUTES", 1)

Q_CLUSTER = {
    "name": "pve-helper",
    "workers": env_int("Q_CLUSTER_WORKERS", 1),
    "timeout": env_int("Q_CLUSTER_TIMEOUT", 1800),
    "retry": env_int("Q_CLUSTER_RETRY", 2100),
    "ALT_CLUSTERS": {
        # Only the default/control cluster runs Django-Q's scheduler. The
        # separate bulk cluster receives explicitly routed data-plane payloads.
        "bulk": {
            "workers": env_int("Q_BULK_WORKERS", 2),
            "timeout": env_int("Q_BULK_TIMEOUT", 21600),
            # Must exceed timeout: django-q retries a task once retry elapses.
            "retry": env_int("Q_BULK_RETRY", 22200),
            "scheduler": False,
        },
    },
    "queue_limit": 50,
    "bulk": 10,
    "orm": "default",
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": env("LOG_LEVEL", "INFO"),
    },
}

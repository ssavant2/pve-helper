"""Django settings for pve-helper."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from django.utils.csp import CSP

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
# False is the fallback rather than the convenience value because DEBUG=True does not
# merely add detail: it returns tracebacks with local variables and a settings dump to
# whoever made the request, and it short-circuits `production_startup_errors()`, so a
# deployment that simply forgets this variable also loses the check that would have
# complained about the default SECRET_KEY and an empty ALLOWED_HOSTS. Development sets
# it explicitly in Compose.
DEBUG = env_bool("DEBUG", False)
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
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

SECURE_CSP = {
    "default-src": [CSP.SELF],
    "base-uri": [CSP.SELF],
    "connect-src": [CSP.SELF, "ws:", "wss:"],
    "font-src": [CSP.SELF, "data:"],
    "form-action": [CSP.SELF],
    "frame-ancestors": [CSP.NONE],
    "frame-src": [CSP.NONE],
    "img-src": [CSP.SELF, "data:", "blob:"],
    "media-src": [CSP.SELF, "blob:"],
    "object-src": [CSP.NONE],
    "script-src": [CSP.SELF],
    "script-src-attr": [CSP.NONE],
    "style-src": [CSP.SELF],
    # Existing server-rendered progress, tag-color and indentation values use
    # narrowly-scoped style attributes. External stylesheets remain same-origin.
    "style-src-attr": [CSP.UNSAFE_INLINE],
    "worker-src": [CSP.SELF, "blob:"],
}

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

# Process-local on purpose, declared rather than inherited so the choice is
# visible and its condition is written down next to it.
#
# Everything in this cache is a read-through memo of a Proxmox response: live
# guest status, inventory, locks, lineage, the tag registry and the datastore
# nav. Nothing coordination-shaped lives here — sessions are database-backed and
# mutual exclusion uses PostgreSQL advisory locks — so two processes holding
# different copies costs a provider round-trip and never correctness.
# Staleness is handled by `cluster_state_identity.cluster_cache_key()`, which
# namespaces every key with the cluster's `cache_generation`; a writer bumps
# that column, so invalidation already reaches sibling processes that a
# `cache.delete` in one worker would have missed.
#
# MAX_ENTRIES is raised well above Django's default of 300 because the per-guest
# snapshot/agent/HA keys reach that ceiling long before their TTLs expire in a
# real fleet, and the default CULL_FREQUENCY then discards a third of the cache
# at a moment unrelated to freshness. Entries are small dicts and every TTL here
# is a minute or less, so the ceiling costs a few megabytes per process.
#
# Revisit when `web` runs as more than one container: sharing the memo across
# five processes on one host is worth little, but across replicas the hit rate
# falls with each one, and a shared backend becomes the right trade. That is a
# backend swap in this dict — the call sites and the generation scheme carry
# over unchanged.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "pve-helper",
        "OPTIONS": {"MAX_ENTRIES": 10000, "CULL_FREQUENCY": 4},
    }
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

# Production is OIDC-only: the configured provider is the sole way in, Django admin included.
# The username/password backend (admin login form, createsuperuser) is kept only
# for the dev/E2E path, where login is not the enforced OIDC gate.
AUTHENTICATION_BACKENDS = ["core.auth.PveHelperOIDCBackend"]
if not APP_REQUIRE_LOGIN:
    AUTHENTICATION_BACKENDS.append("django.contrib.auth.backends.ModelBackend")

# Django admin is a development and E2E convenience, not an operator surface: the
# app's own UI covers everything an operator does, and `manage.py shell`/`dbshell`
# cover everything it does not. Where login is enforced it would instead be a
# second, browser-reachable write path over models the app mutates only through
# validated services - repointing a verified endpoint, rebinding a mount, flipping
# a schedule without an audit row - and over AuditEvent itself, which PLAN.md names
# as part of the safety boundary. A boundary its own session can rewrite is not one.
# Host shell access can still reach the database; a stolen cookie or an XSS cannot.
DJANGO_ADMIN_ENABLED = DEBUG or not APP_REQUIRE_LOGIN

OIDC_ISSUER_URL = env("OIDC_ISSUER_URL", "https://auth.example.com/application/o/pve-helper/").rstrip("/")
OIDC_RP_CLIENT_ID = env("OIDC_CLIENT_ID", "")
OIDC_RP_CLIENT_SECRET = env("OIDC_CLIENT_SECRET", "")
OIDC_RP_SCOPES = env("OIDC_SCOPES", "openid profile email groups")
# Access requires this value in the provider's `groups` claim. Providers differ in
# what they put there: Authentik/Authelia/Keycloak emit group names, Entra emits
# group object GUIDs, and some deployments emit nothing because the provider's own
# application assignment is the gate. Deployments in that last position set the
# sentinel below; an empty value is refused at startup (pve_helper.E011) because it
# cannot be told apart from a dropped configuration line, and it would silently
# admit every account the provider authenticates.
OIDC_ANY_AUTHENTICATED_USER = "any-authenticated-user"
OIDC_REQUIRED_GROUP = env("OIDC_REQUIRED_GROUP", "pve-helper-admins").strip()
OIDC_CREATE_USER = True
# mozilla-django-oidc does NOT perform OIDC discovery (.well-known); the OP endpoints
# must be set explicitly. Defaults below follow Authentik's URL scheme for the bundled
# provider-specific recipe. Other providers must override every endpoint via env.
_oidc_o_base = OIDC_ISSUER_URL.rsplit("/", 1)[0]  # e.g. https://auth.example.com/application/o
OIDC_OP_AUTHORIZATION_ENDPOINT = env("OIDC_OP_AUTHORIZATION_ENDPOINT", "") or f"{_oidc_o_base}/authorize/"
OIDC_OP_TOKEN_ENDPOINT = env("OIDC_OP_TOKEN_ENDPOINT", "") or f"{_oidc_o_base}/token/"
OIDC_OP_USER_ENDPOINT = env("OIDC_OP_USER_ENDPOINT", "") or f"{_oidc_o_base}/userinfo/"
OIDC_OP_JWKS_ENDPOINT = env("OIDC_OP_JWKS_ENDPOINT", "") or f"{OIDC_ISSUER_URL}/jwks/"
_oidc_end_session = env("OIDC_OP_END_SESSION_ENDPOINT", "")
OIDC_OP_END_SESSION_ENDPOINT = (
    "" if _oidc_end_session.lower() == "local" else _oidc_end_session or f"{OIDC_ISSUER_URL}/end-session/"
)
OIDC_RP_SIGN_ALGO = "RS256"

# RP-initiated logout: also end the provider SSO session, otherwise local logout can be
# immediately undone by silent re-authentication. Storing the id token lets us pass
# id_token_hint so a conforming provider can identify the session.
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

# Secrets stored in the database (cluster API tokens) are sealed under this
# keyring, deliberately not under SECRET_KEY: SECRET_KEY is rotated for session
# and signing reasons, and doing so must not make every cluster credential
# unreadable. Format: "<key-id>:<base64-32-byte-key>,<key-id>:<...>".
# Losing the active key makes all cluster credentials unrecoverable, so every key
# id still named by stored ciphertext needs backup/escrow — see the runbook.
PVE_HELPER_ENCRYPTION_KEYS = env("PVE_HELPER_ENCRYPTION_KEYS", "")
PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID = env("PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID", "")

PVE_ENDPOINTS = env_list("PVE_ENDPOINTS", "")
PVE_VERIFY_TLS = env_bool("PVE_VERIFY_TLS", True)
PVE_CA_BUNDLE = env("PVE_CA_BUNDLE", "")
PVE_API_TOKEN_ID = env("PVE_API_TOKEN_ID", "")
PVE_API_TOKEN_SECRET = env("PVE_API_TOKEN_SECRET", "")
# Only enabled by pve_helper.test_settings: turns an unmocked Proxmox request
# into an immediate test failure instead of a real infrastructure call.
PVE_TEST_NETWORK_DISABLED = env_bool("PVE_TEST_NETWORK_DISABLED", False)
SCHEDULED_ACTION_TIMEOUT_SECONDS = env_int("SCHEDULED_ACTION_TIMEOUT_SECONDS", 1800)
# Backups and restores can legitimately run far longer than a power action.
# Keep this independently configurable without weakening scheduled-action timeouts.
BACKUP_TASK_TIMEOUT_SECONDS = env_int("BACKUP_TASK_TIMEOUT_SECONDS", 21600)
SCHEDULED_ACTION_POLL_INTERVAL_SECONDS = env_int("SCHEDULED_ACTION_POLL_INTERVAL_SECONDS", 5)
SCHEDULED_ACTION_RUN_RETENTION_DAYS = env_int("SCHEDULED_ACTION_RUN_RETENTION_DAYS", 90)

# Writes are the application's normal mode, not an opt-in: this is an administrative
# tool and every mount already carries its own `ro`/`rw` answer, which
# `storage_mounts.mount_health()` honours per datastore. This flag is the
# coarse operational brake above that — something an operator turns *off* to freeze
# storage writes during maintenance without remounting anything on the host.
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
STORAGE_DOWNLOAD_ACCEL_MANIFEST_PATH = Path("/storage-accel-state/mounts")
CURRENT_GUEST_REFRESH_INTERVAL_MINUTES = env_int("CURRENT_GUEST_REFRESH_INTERVAL_MINUTES", 1)
# This is the container-visible root, never an operator supplied host path.  The
# Compose source may vary, but application paths are always confined below this
# fixed namespace.
PVE_HELPER_STORAGE_CONTAINER_ROOT = Path(env("PVE_HELPER_STORAGE_CONTAINER_ROOT", "/storages"))
STORAGE_METADATA_REFRESH_INTERVAL_MINUTES = env_int("STORAGE_METADATA_REFRESH_INTERVAL_MINUTES", 1)
STORAGE_VOLUME_REFRESH_INTERVAL_MINUTES = env_int("STORAGE_VOLUME_REFRESH_INTERVAL_MINUTES", 5)

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

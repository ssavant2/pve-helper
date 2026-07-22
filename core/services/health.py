"""Readiness that can tell a running process from a usable one.

Liveness answers "is this process responding" and must stay dumb: a probe that
touches the database turns a brief Postgres blip into a restarted web container.
Readiness answers "can this process serve requests", and the honest answer
depends on the schema, not only on the connection.  An image whose migrations
have not been applied accepts connections, reports healthy, and returns 500 on
every page — the v0.1.1->v0.1.2 bring-up did exactly that with six green health
checks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

logger = logging.getLogger(__name__)

# Once every migration this image carries has been applied, it stays applied:
# the migration set is frozen at build time, so a process that has confirmed it
# once cannot legitimately fall behind again.  The check therefore runs only in
# the window between container start and `migrate` finishing, and the docker
# probe that repeats every 30s reads a boolean after that.  Only a manually
# reversed migration could invalidate a confirmation, and this project fixes
# forward rather than reversing; a restart would report it anyway.
_schema_confirmed_current = False


@dataclass(frozen=True)
class SchemaState:
    current: bool
    pending_count: int = 0


def reset_schema_cache() -> None:
    """Forget the confirmation. For tests; nothing in production calls this."""
    global _schema_confirmed_current
    _schema_confirmed_current = False


def schema_state() -> SchemaState:
    """Whether the database carries every migration this image expects.

    Requires a working connection; the caller reports connection failure itself.
    """
    global _schema_confirmed_current
    if _schema_confirmed_current:
        return SchemaState(True)

    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    if not plan:
        _schema_confirmed_current = True
        return SchemaState(True)

    logger.warning(
        "Schema is behind this image: pending_migrations=%d. The service is not ready to serve requests.",
        len(plan),
    )
    return SchemaState(False, len(plan))


def readiness_report(service: str) -> tuple[dict, int]:
    """The readiness payload and HTTP status, shared by the web app and the console.

    The endpoint is unauthenticated, so the payload names what is wrong and how
    much, never migration names or exception text.
    """
    checks: dict[str, object] = {"database": "unknown", "migrations": "unknown"}

    try:
        connection.ensure_connection()
        checks["database"] = "ok"
    except Exception as exc:  # pragma: no cover - defensive health endpoint
        checks["database"] = "error"
        checks["database_error"] = exc.__class__.__name__
        return {"status": "error", "service": service, "checks": checks}, 503

    try:
        state = schema_state()
    except Exception as exc:  # pragma: no cover - defensive health endpoint
        logger.warning("Readiness could not determine the schema state", exc_info=True)
        checks["migrations_error"] = exc.__class__.__name__
        return {"status": "error", "service": service, "checks": checks}, 503

    if not state.current:
        checks["migrations"] = "pending"
        checks["pending_count"] = state.pending_count
        return {"status": "error", "service": service, "checks": checks}, 503

    checks["migrations"] = "ok"
    return {"status": "ok", "service": service, "checks": checks}, 200

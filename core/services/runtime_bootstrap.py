"""One-time environment import into DB-owned runtime configuration.

The database is the source of truth for runtime configuration; the environment is a
bootstrap importer that seeds an empty installation exactly once. After the durable
marker exists this module is read-only with respect to the environment: callers may
ensure bootstrap happened, but configuration then comes from the database.

Bootstrap is a service invariant rather than a process-startup assumption. Scans
overlap today and more callers may appear, so serialization uses a PostgreSQL
transaction-scoped advisory lock rather than a Python lock. SQLite remains valid for
the dev/E2E path and relies on the atomic import, the singleton constraint and
idempotent retry instead of executing PostgreSQL-specific SQL.
"""

from __future__ import annotations

import hashlib
import json

from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from core.models import ProxmoxCluster, ProxmoxEndpoint, RuntimeConfigurationState, StorageMount
from core.services.config import (
    configured_endpoint_definitions,
    configured_storage_definitions,
    sync_storage_consumers,
)


DEFAULT_CLUSTER_KEY = "default"
DEFAULT_CLUSTER_DISPLAY_NAME = "Default cluster"

# Distinct from the tag inventory lock IDs in core.services.tag_inventory_refresh.
_BOOTSTRAP_LOCK_ID = 0x50564548424F01


def _advisory_xact_lock(lock_id: int) -> None:
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_id])


def environment_fingerprint() -> str:
    """Stable digest of the environment configuration a bootstrap would import."""
    payload = {
        "endpoints": sorted(
            [definition.name, definition.url] for definition in configured_endpoint_definitions()
        ),
        "storages": sorted(
            [
                definition.storage_id,
                definition.export,
                definition.path,
                sorted(definition.expected_consumers),
            ]
            for definition in configured_storage_definitions()
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def ensure_bootstrap() -> RuntimeConfigurationState:
    """Return the runtime configuration state, importing the environment once if needed.

    Losing callers wait on the lock and then observe the winner's committed marker;
    the import is never repeated or partially merged.
    """
    try:
        return _ensure_bootstrap_once()
    except IntegrityError:
        # SQLite has no advisory lock, so a concurrent winner is detected by the
        # singleton/unique constraints instead. The loser's transaction rolled back
        # whole; re-reading observes the committed result.
        return _ensure_bootstrap_once()


def _ensure_bootstrap_once() -> RuntimeConfigurationState:
    with transaction.atomic():
        _advisory_xact_lock(_BOOTSTRAP_LOCK_ID)
        state = RuntimeConfigurationState.objects.select_for_update().filter(
            pk=RuntimeConfigurationState.SINGLETON_PK
        ).first()
        if state is not None and state.bootstrap_completed:
            return state
        if state is None:
            state = RuntimeConfigurationState(pk=RuntimeConfigurationState.SINGLETON_PK)
        _import_environment(state)
        return state


def _import_environment(state: RuntimeConfigurationState) -> None:
    """Seed legacy environment configuration, or record an intentional empty install.

    Runs inside the caller's atomic block so the imported records and the completion
    marker commit together: a failed transaction leaves no marker and is safe to retry.
    A new wizard-owned installation has no ``PVE_ENDPOINTS`` and must remain at zero
    clusters; inventing a disabled or endpoint-less ``default`` row would hide the
    onboarding state and make environment bootstrap own identity again.
    """
    endpoint_definitions = configured_endpoint_definitions()
    storage_definitions = configured_storage_definitions()
    cluster = None
    if endpoint_definitions:
        cluster, _ = ProxmoxCluster.objects.get_or_create(
            key=DEFAULT_CLUSTER_KEY,
            defaults={"display_name": DEFAULT_CLUSTER_DISPLAY_NAME, "enabled": True},
        )

    for definition in endpoint_definitions:
        endpoint, created = ProxmoxEndpoint.objects.get_or_create(
            name=definition.name,
            defaults={"url": definition.url, "enabled": True, "cluster": cluster},
        )
        if not created and endpoint.cluster_id is None:
            endpoint.cluster = cluster
            endpoint.save(update_fields=["cluster", "updated_at"])

    for definition in storage_definitions:
        storage, _ = StorageMount.objects.get_or_create(
            storage_id=definition.storage_id,
            defaults={
                "display_name": definition.display_name,
                "export": definition.export,
                "path": definition.path,
                "trash_path": definition.trash_path,
                "expected_consumers": definition.expected_consumers,
                "enabled": True,
            },
        )
        if cluster is not None:
            sync_storage_consumers(storage, cluster)

    state.bootstrap_completed = True
    state.bootstrap_completed_at = timezone.now()
    state.bootstrap_fingerprint = environment_fingerprint()
    state.save()

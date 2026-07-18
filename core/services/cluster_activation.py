"""The single service path that enables a cluster, plus the pre-activation key choice.

More than one enabled cluster is permitted only once every read, write, URL and
payload boundary is cluster-qualified. That is an invariant of the migration, not a
feature flag: for the whole of Phases 1-4 the codebase still contains global
selectors alongside cluster-aware ones, and a half-migrated system does not fail
loudly — it silently accepts the wrong cluster's evidence.

Before contract activation the shared enable service refuses a second enabled
cluster. Phase 4 removes the temporary database uniqueness constraint so version 1
can admit several clusters; UI forms, management commands, workers and import
services must still call `enable_cluster()` rather than flipping `enabled` directly.
"""

from __future__ import annotations

from django.db import connection, transaction
from django.utils import timezone

from core.models import (
    AuditEvent,
    ConsoleSession,
    ProxmoxCluster,
    ProxmoxEndpoint,
    ProxmoxStorageConsumer,
    RuntimeConfigurationState,
    ScheduledAction,
    cluster_key_validator,
)
from core.services.runtime_bootstrap import ensure_bootstrap


class ClusterActivationError(RuntimeError):
    """A cluster enable/key change was refused because an identity contract forbids it."""


_ACTIVATION_LOCK_ID = 0x5056454D554C01


def _activation_data_errors() -> list[str]:
    errors = []
    if ProxmoxEndpoint.objects.filter(cluster__isnull=True).exists():
        errors.append("one or more Proxmox endpoints have no cluster")
    if ProxmoxStorageConsumer.objects.filter(cluster__isnull=True).exists():
        errors.append("one or more storage consumers have no cluster")
    if ScheduledAction.objects.filter(enabled=True, cluster__isnull=True).exists():
        errors.append("one or more enabled scheduled actions have no cluster")
    if ConsoleSession.objects.filter(
        cluster__isnull=True,
        expires_at__gt=timezone.now(),
        status__in=(
            ConsoleSession.Status.PENDING,
            ConsoleSession.Status.CONNECTING,
            ConsoleSession.Status.CONNECTED,
        ),
    ).exists():
        errors.append("one or more active console sessions have no cluster")
    if AuditEvent.objects.filter(
        action__startswith="guest.",
        outcome__in=("queued", "running"),
        cluster__isnull=True,
    ).exists():
        errors.append("one or more active guest operations have no cluster")
    return errors


@transaction.atomic
def activate_multicluster_identity() -> RuntimeConfigurationState:
    """Permanently activate explicit multi-cluster identity after a fail-closed audit."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_ACTIVATION_LOCK_ID])
    state = ensure_bootstrap()
    state = RuntimeConfigurationState.objects.select_for_update().get(pk=state.pk)
    if state.identity_contract_version >= 1:
        return state
    errors = _activation_data_errors()
    if errors:
        raise ClusterActivationError(
            "Multi-cluster identity activation was refused: " + "; ".join(errors) + "."
        )
    state.identity_contract_version = 1
    details = dict(state.details or {})
    details["multicluster_identity_activated_at"] = timezone.now().isoformat()
    state.details = details
    state.save(update_fields=["identity_contract_version", "details", "updated_at"])
    return state


def identity_contract_version() -> int:
    state = RuntimeConfigurationState.objects.filter(pk=RuntimeConfigurationState.SINGLETON_PK).first()
    return state.identity_contract_version if state is not None else 0


def assert_can_enable_cluster(cluster: ProxmoxCluster) -> None:
    if identity_contract_version() >= 1:
        return
    conflict = ProxmoxCluster.objects.filter(enabled=True).exclude(pk=cluster.pk).first()
    if conflict is not None:
        raise ClusterActivationError(
            f"Cannot enable cluster '{cluster.key}' while cluster '{conflict.key}' is enabled: "
            "multi-cluster identity is not active yet (identity contract version 0). A second "
            "cluster may only be enabled once every read, write, URL and payload boundary is "
            "cluster-qualified."
        )


def enable_cluster(cluster: ProxmoxCluster) -> ProxmoxCluster:
    with transaction.atomic():
        locked = ProxmoxCluster.objects.select_for_update().get(pk=cluster.pk)
        assert_can_enable_cluster(locked)
        if not locked.enabled:
            locked.enabled = True
            locked.save(update_fields=["enabled", "updated_at"])
    cluster.refresh_from_db()
    return cluster


def set_initial_cluster_key(new_key: str, *, current_key: str | None = None) -> ProxmoxCluster:
    """Choose a cluster's durable key, before cluster-qualified contracts activate.

    The key is immutable afterwards: it is durable identity that appears in URLs,
    queue payloads and audit rows, so a rename would mean maintaining aliases
    indefinitely. An installation that chooses nothing simply keeps `default`.

    The guard is identity contract version 0, not the number of configured clusters.
    Version 0 *is* the statement that no durable cluster-qualified payload exists —
    those arrive in Phase 3 — so a key is safe to change for exactly as long as the
    version says so. Gating on "only one cluster" instead was a proxy for a pristine
    bootstrap install, and it locks out a safe rename as soon as a second cluster is
    registered, which is easy to do before choosing the first one's key. Nothing has
    to be tightened later: activation to version 1 closes this permanently.

    Historical audit rows keep the key they were written with. That drift is
    accepted rather than repaired, on the same grounds the migration plan gives for
    backfill: rewriting history to match a later decision asserts a provenance that
    was never true.
    """
    normalized = (new_key or "").strip().lower()
    cluster_key_validator(normalized)

    with transaction.atomic():
        state = ensure_bootstrap()
        if state.identity_contract_version >= 1:
            raise ClusterActivationError(
                "The cluster key is immutable once the identity contract is active; it appears in "
                "URLs, queued payloads and audit history. Change the display name instead."
            )
        clusters = list(ProxmoxCluster.objects.select_for_update().order_by("key"))
        if not clusters:
            raise ClusterActivationError("No cluster is configured.")

        if current_key:
            wanted = current_key.strip().lower()
            cluster = next((item for item in clusters if item.key == wanted), None)
            if cluster is None:
                raise ClusterActivationError(f"No cluster with key '{wanted}'.")
        elif len(clusters) == 1:
            cluster = clusters[0]
        else:
            available = ", ".join(item.key for item in clusters)
            raise ClusterActivationError(
                f"Several clusters are configured ({available}); name which one to rekey."
            )

        if cluster.key == normalized:
            return cluster
        if ProxmoxCluster.objects.filter(key__iexact=normalized).exclude(pk=cluster.pk).exists():
            raise ClusterActivationError(f"Cluster key '{normalized}' is already in use.")
        cluster.key = normalized
        cluster.full_clean()
        cluster.save(update_fields=["key", "updated_at"])
    return cluster

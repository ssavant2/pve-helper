"""The single service path that enables a cluster, plus the pre-activation key choice.

More than one enabled cluster is permitted only once every read, write, URL and
payload boundary is cluster-qualified. That is an invariant of the migration, not a
feature flag: for the whole of Phases 1-4 the codebase still contains global
selectors alongside cluster-aware ones, and a half-migrated system does not fail
loudly — it silently accepts the wrong cluster's evidence.

Enforcement is layered. `ProxmoxCluster` carries a partial unique constraint that
lets the database refuse a second enabled cluster outright; this module is the path
that reports the refusal usefully. UI forms, management commands, workers and import
services all call `enable_cluster()`; none of them flips `enabled` directly.
"""

from __future__ import annotations

from django.db import transaction

from core.models import ProxmoxCluster, RuntimeConfigurationState, cluster_key_validator
from core.services.runtime_bootstrap import ensure_bootstrap


class ClusterActivationError(RuntimeError):
    """A cluster enable/key change was refused because an identity contract forbids it."""


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


def set_initial_cluster_key(new_key: str) -> ProxmoxCluster:
    """Change the auto-created bootstrap key, before cluster-qualified contracts activate.

    The key is immutable afterwards: it is durable identity that appears in URLs,
    queue payloads and audit rows, so a rename would mean maintaining aliases
    indefinitely. This is the one pre-activation exception, and an installation that
    chooses nothing simply keeps `default`.
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
        if len(clusters) != 1:
            raise ClusterActivationError(
                f"set_initial_cluster_key requires exactly one configured cluster, found {len(clusters)}."
            )
        cluster = clusters[0]
        if cluster.key == normalized:
            return cluster
        if ProxmoxCluster.objects.filter(key__iexact=normalized).exclude(pk=cluster.pk).exists():
            raise ClusterActivationError(f"Cluster key '{normalized}' is already in use.")
        cluster.key = normalized
        cluster.full_clean()
        cluster.save(update_fields=["key", "updated_at"])
    return cluster

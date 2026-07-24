"""The strict unused-connection hard delete — R4's second slice.

This is the only path that physically removes a ``ProxmoxCluster`` row and, with
it, releases the permanent key, the pinned CA UUID and its endpoints' globally
unique normalized URLs so the same logical connection can be configured again.
It is gated by :func:`unused_connection_deletion_eligibility` (slice 1), which
proves the connection carries **no** operational footprint; slice 3 owns the
operator-facing confirmation UX.

Two properties make this safe (see ``docs/cluster-retire.local.md`` →
*No general hard delete*):

* **Eligibility is re-checked under the lifecycle lock**, after the cluster row
  is ``select_for_update``-locked, so a footprint acquired between the operator's
  read and this transaction cannot slip through. The lock is the same barrier
  provider acquisition and retirement take, so a concurrent scan/console/operation
  cannot start against a connection while it is being deleted.
* **Configuration Audit is preserved, not deleted.** ``AuditEvent.cluster`` is
  ``PROTECT``, so the cluster row cannot be removed while any event still points at
  it. Rather than cascade the history away, every event is detached — its
  ``cluster`` relation nulled while its durable ``cluster_key_snapshot``, object
  identity and normalized details stay — so the deleted attempt's configuration
  trail (and the final deletion event itself) remain discoverable by the key
  snapshot, exactly as a later exact-key re-registration expects.

Eligibility guarantees the only reverse relations that can still hold rows are the
disposable connection configuration (``credential``, ``transport_trust``,
``endpoints``) and configuration-allowlist Audit; every operational, projection,
storage and schedule relation is already empty, so the deletion is a bounded,
set-based teardown of exactly those rows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction

from core.models import (
    AuditEvent,
    ClusterCredential,
    ClusterTransportTrust,
    ProxmoxCluster,
    ProxmoxEndpoint,
)
from core.services.audit_events import record_audit_event
from core.services.cluster_deletion_eligibility import DeletionEligibility, unused_connection_deletion_eligibility
from core.services.cluster_lifecycle_lock import cluster_lifecycle_lock
from core.services.cluster_scopes import historical_clusters, managed_clusters
from core.services.cluster_trust import reset_trust_pools
from core.services.public_errors import PublicMessageError

DELETION_AUDIT_DETAIL_LIMIT = 100

ERROR_CODE_DELETION_NOT_ALLOWED = "cluster_deletion_not_allowed"
ERROR_CODE_DELETION_BLOCKED = "cluster_deletion_blocked"
ERROR_CODE_DELETION_POSTCONDITION = "cluster_deletion_postcondition_failed"
ERROR_CODE_DELETION_FAILED = "cluster_deletion_failed"

logger = logging.getLogger(__name__)


class ClusterDeletionError(PublicMessageError, RuntimeError):
    """Base class for stable, public unused-connection deletion refusals."""


class ClusterDeletionNotAllowed(ClusterDeletionError):
    error_code = ERROR_CODE_DELETION_NOT_ALLOWED


class ClusterDeletionBlocked(ClusterDeletionError):
    error_code = ERROR_CODE_DELETION_BLOCKED

    def __init__(self, message: str, *, blocker_relation: str, blocker_kind: str):
        self.blocker_relation = blocker_relation
        self.blocker_kind = blocker_kind
        super().__init__(message)


class ClusterDeletionPostconditionFailed(ClusterDeletionError):
    error_code = ERROR_CODE_DELETION_POSTCONDITION


class ClusterDeletionFailed(ClusterDeletionError):
    error_code = ERROR_CODE_DELETION_FAILED


@dataclass(frozen=True)
class DeletionResult:
    cluster_pk: int
    cluster_key: str
    display_name: str
    audit_event_id: int
    endpoints_deleted: int
    credential_deleted: bool
    trust_deleted: bool
    audit_events_detached: int


def _endpoint_snapshots(cluster_id: int) -> tuple[tuple[dict[str, object], ...], int]:
    """Non-secret, bounded endpoint refs for the deletion Audit event."""
    rows = list(
        ProxmoxEndpoint.objects.filter(cluster_id=cluster_id).order_by("pk").values("name", "normalized_url", "enabled")
    )
    snapshots = tuple(
        {
            "name": str(row["name"]),
            "url": str(row["normalized_url"]),
            "enabled": bool(row["enabled"]),
        }
        for row in rows[:DELETION_AUDIT_DETAIL_LIMIT]
    )
    return snapshots, len(rows)


def _assert_deletion_postconditions(cluster_pk: int) -> None:
    """Prove the teardown removed the row and its config and left no dangling relation."""
    if any(
        (
            historical_clusters().filter(pk=cluster_pk).exists(),
            ClusterCredential.objects.filter(cluster_id=cluster_pk).exists(),
            ClusterTransportTrust.objects.filter(cluster_id=cluster_pk).exists(),
            ProxmoxEndpoint.objects.filter(cluster_id=cluster_pk).exists(),
            AuditEvent.objects.filter(cluster_id=cluster_pk).exists(),
        )
    ):
        raise ClusterDeletionPostconditionFailed(
            "The unused connection could not prove its deletion postconditions; no changes were committed."
        )


def _delete_unused_connection_atomic(cluster, *, actor) -> DeletionResult:
    with transaction.atomic():
        with cluster_lifecycle_lock(cluster):
            try:
                locked = managed_clusters().select_for_update().get(pk=cluster.pk)
            except ProxmoxCluster.DoesNotExist as exc:
                raise ClusterDeletionNotAllowed(
                    "This cluster connection no longer exists or has been retired and cannot be deleted."
                ) from exc

            eligibility: DeletionEligibility = unused_connection_deletion_eligibility(locked)
            if eligibility.blocked:
                first = eligibility.blockers[0]
                raise ClusterDeletionBlocked(
                    "This connection has operational history or unresolved state and cannot be deleted.",
                    blocker_relation=first.relation,
                    blocker_kind=first.kind,
                )

            cluster_pk = locked.pk
            cluster_key = locked.key
            display_name = locked.display_name
            endpoints, endpoint_count = _endpoint_snapshots(cluster_pk)
            credential = ClusterCredential.objects.filter(cluster_id=cluster_pk).only("token_id").first()
            trust = ClusterTransportTrust.objects.filter(cluster_id=cluster_pk).only("mode").first()
            credential_token_id = credential.token_id if credential is not None else ""
            trust_mode = trust.mode if trust is not None else ""
            # Every remaining event is configuration-allowlist history (eligibility
            # blocks on any operational event), so this count is the trail that is
            # preserved through detachment.
            config_audit_count = AuditEvent.objects.filter(cluster_id=cluster_pk).count()

            # Record the immutable deletion event while the relation still exists, so
            # the log-forwarding signal snapshots its top-level outbox payload in this
            # transaction. It is a configuration-allowlist action, so it never stamps
            # operational footprint on the row it is about to remove.
            event = record_audit_event(
                user=actor,
                action="cluster.unused_connection_deleted",
                object_type="cluster",
                object_id=cluster_key,
                outcome="success",
                cluster=locked,
                cluster_key_snapshot=cluster_key,
                details={
                    "display_name": display_name,
                    "cluster_key": cluster_key,
                    "endpoint_count": endpoint_count,
                    "endpoints": list(endpoints),
                    "endpoints_omitted": max(0, endpoint_count - len(endpoints)),
                    "credential_token_id": credential_token_id,
                    "trust_mode": trust_mode,
                    "configuration_audit_events_detached": config_audit_count,
                },
            )

            # Detach the whole configuration trail — including the event just created —
            # from the cluster relation, retaining every durable key snapshot. This is
            # both what preserves the history and what releases the PROTECT that would
            # otherwise refuse the cluster delete below.
            audit_events_detached = AuditEvent.objects.filter(cluster_id=cluster_pk).update(cluster=None)

            # Delete disposable configuration in dependency order. endpoints is PROTECT
            # so it must go before the cluster; credential/trust CASCADE but are removed
            # explicitly for an accountable set-based count.
            endpoints_deleted = ProxmoxEndpoint.objects.filter(cluster_id=cluster_pk).delete()[0]
            credential_deleted = bool(ClusterCredential.objects.filter(cluster_id=cluster_pk).delete()[0])
            trust_deleted = bool(ClusterTransportTrust.objects.filter(cluster_id=cluster_pk).delete()[0])

            # Remove the row itself, releasing the permanent key, the pinned CA UUID and
            # the endpoints' globally unique URLs for a fresh registration.
            managed_clusters().filter(pk=cluster_pk).delete()

            _assert_deletion_postconditions(cluster_pk)
            transaction.on_commit(reset_trust_pools)

            return DeletionResult(
                cluster_pk=cluster_pk,
                cluster_key=cluster_key,
                display_name=display_name,
                audit_event_id=event.pk,
                endpoints_deleted=endpoints_deleted,
                credential_deleted=credential_deleted,
                trust_deleted=trust_deleted,
                audit_events_detached=audit_events_detached,
            )


def delete_unused_cluster_connection(cluster, *, actor) -> DeletionResult:
    """Physically delete a demonstrably unused cluster connection.

    Re-checks :func:`unused_connection_deletion_eligibility` under the shared
    lifecycle lock, preserves the configuration Audit trail by detaching it,
    deletes the disposable configuration rows and the cluster row, and releases the
    key, CA UUID and endpoint URLs. Makes no provider request. Raises a stable
    public error — never a provider/Python string — when the connection is not
    eligible or the teardown cannot prove its postconditions.
    """
    try:
        return _delete_unused_connection_atomic(cluster, actor=actor)
    except ClusterDeletionError:
        raise
    except Exception as exc:
        logger.exception(
            "Unexpected unused cluster connection deletion failure",
            extra={"cluster_pk": getattr(cluster, "pk", None)},
        )
        raise ClusterDeletionFailed("Deleting the unused connection failed safely; no changes were committed.") from exc

"""Operator exits for standalone/corosync identity and membership transitions."""

from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import (
    ClusterCredential,
    ClusterMembershipState,
    ClusterProjectionCoverage,
    ClusterStorage,
    ClusterStorageMount,
    ClusterStorageNodeState,
    ClusterTopologyHandoffStorageBinding,
    ClusterTransportTrust,
    ProxmoxCluster,
    ProxmoxEndpoint,
    StorageCatalogState,
)
from core.services.audit_events import record_audit_event
from core.services.cluster_lifecycle_lock import cluster_lifecycle_lock, scan_admission_lock
from core.services.cluster_membership import (
    ERROR_OBSERVER_NOT_MEMBER,
    InvalidMembershipPayload,
    _publish_complete,
    normalize_cluster_status,
)
from core.services.cluster_onboarding import (
    ClusterCandidate,
    VerifiedConnection,
    active_cluster_operation_labels,
    persist_verified_cluster_configuration,
    verify_registered_endpoint,
)
from core.services.cluster_resolver import client_for_endpoint, enabled_endpoints
from core.services.cluster_retirement import retire_cluster
from core.services.cluster_scopes import historical_clusters
from core.services.cluster_topology_role import (
    MembershipObservation,
    RoleDecision,
    TopologyRole,
    evaluate_role_transition,
    resolve_transition,
)
from core.services.proxmox import ProxmoxAPIError
from core.services.public_errors import PublicMessageError


class ClusterTopologyHandoffError(PublicMessageError, RuntimeError):
    error_code = "cluster_topology_handoff_refused"


@dataclass(frozen=True)
class HandoffStorageBinding:
    pk: int
    storage_id: str
    mount_id: int
    mount_label: str
    scope: str
    node: str


@dataclass(frozen=True)
class TopologyHandoffSnapshot:
    cluster_pk: int
    cluster_key: str
    lifecycle_generation: int
    membership_generation: int
    registered_role: str
    pending_role: str
    endpoint_digest: str
    configuration_digest: str
    storage_digest: str
    endpoints: tuple[tuple[int, str, str, bool, str], ...]
    storage_bindings: tuple[HandoffStorageBinding, ...]

    @property
    def digest(self) -> str:
        payload = {
            "cluster_pk": self.cluster_pk,
            "cluster_key": self.cluster_key,
            "lifecycle_generation": self.lifecycle_generation,
            "membership_generation": self.membership_generation,
            "registered_role": self.registered_role,
            "pending_role": self.pending_role,
            "endpoint_digest": self.endpoint_digest,
            "configuration_digest": self.configuration_digest,
            "storage_digest": self.storage_digest,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class MembershipRecoveryCandidate:
    endpoint_id: int
    endpoint_name: str
    members: tuple[str, ...]
    observed_from: str
    topology_role: TopologyRole
    digest: str


def _hash(rows: object) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def topology_handoff_snapshot(cluster: ProxmoxCluster, *, lock: bool = False) -> TopologyHandoffSnapshot:
    cluster_qs = historical_clusters()
    state_qs = ClusterMembershipState.objects
    endpoint_qs = ProxmoxEndpoint.objects
    credential_qs = ClusterCredential.objects
    trust_qs = ClusterTransportTrust.objects
    binding_qs = ClusterStorageMount.objects
    if lock:
        cluster_qs = cluster_qs.select_for_update()
        state_qs = state_qs.select_for_update()
        endpoint_qs = endpoint_qs.select_for_update()
        credential_qs = credential_qs.select_for_update()
        trust_qs = trust_qs.select_for_update()
        binding_qs = binding_qs.select_for_update()
    current = cluster_qs.get(pk=cluster.pk)
    state = state_qs.filter(cluster=current).first()
    if state is None or not state.transition_pending:
        raise ClusterTopologyHandoffError("This connection no longer has a topology transition to hand off.")
    if not state.pending_role_is_readable or state.pending_role() is TopologyRole.UNKNOWN:
        raise ClusterTopologyHandoffError(
            "This build cannot read the pending topology role. Use the explicit pending-evidence repair first."
        )
    endpoints = tuple(
        endpoint_qs.filter(cluster=current)
        .order_by("pk")
        .values_list("pk", "name", "normalized_url", "enabled", "updated_at")
    )
    credential = (
        credential_qs.filter(cluster=current)
        .values_list(
            "token_id",
            "token_secret_sealed",
            "encryption_key_id",
            "rotated_at",
            "updated_at",
        )
        .first()
    )
    trust = (
        trust_qs.filter(cluster=current)
        .values_list(
            "mode",
            "ca_pem",
            "approved_at",
            "updated_at",
        )
        .first()
    )
    configuration = (
        current.enabled,
        current.ingestion_quarantined,
        current.quarantine_reason,
        current.discovered_ca_uuid,
        current.discovered_ca_fingerprint,
        credential,
        trust,
    )
    binding_rows = list(
        binding_qs.filter(cluster_storage__cluster=current).select_related("cluster_storage", "mount").order_by("pk")
    )
    bindings = tuple(
        HandoffStorageBinding(
            pk=row.pk,
            storage_id=row.cluster_storage.storage_id,
            mount_id=row.mount_id,
            mount_label=row.mount.display_name,
            scope=row.scope,
            node=row.node or "",
        )
        for row in binding_rows
    )
    storage_rows = [(row.pk, row.storage_id, row.mount_id, row.scope, row.node) for row in bindings]
    return TopologyHandoffSnapshot(
        cluster_pk=current.pk,
        cluster_key=current.key,
        lifecycle_generation=current.lifecycle_generation,
        membership_generation=state.membership_generation,
        registered_role=state.topology_role,
        pending_role=state.pending_topology_role,
        endpoint_digest=_hash(endpoints),
        configuration_digest=_hash(configuration),
        storage_digest=_hash(storage_rows),
        endpoints=endpoints,
        storage_bindings=bindings,
    )


def _pending_decision(state: ClusterMembershipState, verified: VerifiedConnection) -> RoleDecision:
    if not state.transition_pending or not state.pending_role_is_readable:
        raise ClusterTopologyHandoffError("This topology hand-off is no longer available.")
    pending_role = state.pending_role()
    if not verified.membership_complete or verified.topology_role is TopologyRole.UNKNOWN:
        raise ClusterTopologyHandoffError("The replacement did not return a complete topology observation.")
    if verified.topology_role is not pending_role:
        raise ClusterTopologyHandoffError(
            f"This connection is pending toward {pending_role.value}, but the replacement verified as "
            f"{verified.topology_role.value}."
        )
    observation = MembershipObservation(
        complete=True,
        has_cluster_row=verified.topology_role is TopologyRole.COROSYNC,
        member_count=len(verified.node_names),
    )
    decision = evaluate_role_transition(state.role(), observation, pending_role=pending_role)
    try:
        return resolve_transition(decision, confirmed_role=verified.topology_role)
    except ValueError as exc:
        raise ClusterTopologyHandoffError(
            "The current pending topology decision cannot be resolved by this verified replacement."
        ) from exc


def complete_topology_handoff(
    *,
    old_cluster: ProxmoxCluster,
    candidate: ClusterCandidate,
    verified: VerifiedConnection,
    expected_snapshot_digest: str,
    selected_storage_binding_ids: tuple[int, ...],
    retirement_confirmation: str,
    actor,
) -> ProxmoxCluster:
    """Retire one immutable identity and atomically configure its replacement."""
    try:
        with transaction.atomic():
            with scan_admission_lock():
                replacement = historical_clusters().create(
                    key=candidate.key,
                    display_name=candidate.display_name,
                    enabled=False,
                )
                with ExitStack() as locks:
                    for cluster in sorted((old_cluster, replacement), key=lambda item: item.pk):
                        locks.enter_context(cluster_lifecycle_lock(cluster))
                    old = historical_clusters().select_for_update().get(pk=old_cluster.pk)
                    new = historical_clusters().select_for_update().get(pk=replacement.pk)
                    current_snapshot = topology_handoff_snapshot(old, lock=True)
                    if current_snapshot.digest != expected_snapshot_digest:
                        raise ClusterTopologyHandoffError(
                            "The transition, endpoints or storage mappings changed. Review the hand-off again."
                        )
                    state = ClusterMembershipState.objects.select_for_update().get(cluster=old)
                    _pending_decision(state, verified)
                    blockers = active_cluster_operation_labels(old)
                    if blockers:
                        raise ClusterTopologyHandoffError(
                            "Provider work became active before the hand-off: " + "; ".join(blockers) + "."
                        )
                    selected = set(selected_storage_binding_ids)
                    available = {row.pk: row for row in current_snapshot.storage_bindings}
                    if not selected <= set(available):
                        raise ClusterTopologyHandoffError("The confirmed storage mapping list changed.")

                    old.enabled = False
                    old.save(update_fields=["enabled", "updated_at"])
                    retire_cluster(
                        old,
                        confirmation=retirement_confirmation,
                        actor=actor,
                        reason=(
                            f"Topology identity hand-off from {current_snapshot.registered_role} "
                            f"to {current_snapshot.pending_role}."
                        ),
                        replacement_ca_uuid=(
                            verified.identity.ca_uuid if verified.identity.ca_uuid != old.discovered_ca_uuid else ""
                        ),
                    )
                    new = persist_verified_cluster_configuration(new, candidate, verified)
                    for binding_id in sorted(selected):
                        binding = available[binding_id]
                        ClusterTopologyHandoffStorageBinding.objects.create(
                            cluster=new,
                            source_cluster_key_snapshot=old.key,
                            storage_id=binding.storage_id,
                            mount_id=binding.mount_id,
                            scope=binding.scope,
                            node=binding.node or None,
                        )
                    record_audit_event(
                        user=actor,
                        action="cluster.added",
                        object_type="cluster",
                        object_id=new.key,
                        cluster=new,
                        details={
                            "cluster_key": new.key,
                            "display_name": new.display_name,
                            "endpoint_name": candidate.endpoint_name,
                            "endpoint_url": new.endpoints.get(name=candidate.endpoint_name).normalized_url,
                            "trust_mode": candidate.trust_mode,
                            "token_id": candidate.token_id,
                            "ca_uuid": verified.identity.ca_uuid,
                            "topology_handoff_from": old.key,
                        },
                    )
                    record_audit_event(
                        user=actor,
                        action="cluster.topology_handoff_completed",
                        object_type="cluster",
                        object_id=new.key,
                        cluster=new,
                        details={
                            "cluster_key": new.key,
                            "source_cluster_key": old.key,
                            "registered_role": current_snapshot.registered_role,
                            "confirmed_role": current_snapshot.pending_role,
                            "transferred_endpoint_url": candidate.endpoint_url,
                            "released_endpoint_urls": [
                                row[2]
                                for row in current_snapshot.endpoints
                                if row[2] != new.endpoints.get(name=candidate.endpoint_name).normalized_url
                            ],
                            "storage_binding_intent_count": len(selected),
                        },
                    )
                    return new
    except ClusterTopologyHandoffError:
        raise
    except IntegrityError as exc:
        raise ClusterTopologyHandoffError(
            "The replacement key, endpoint, identity or storage mapping was claimed concurrently. Nothing changed."
        ) from exc


def repair_unreadable_pending_transition(
    cluster: ProxmoxCluster,
    *,
    typed_cluster_key: str,
    actor,
) -> None:
    try:
        with transaction.atomic():
            with cluster_lifecycle_lock(cluster):
                locked = historical_clusters().select_for_update().get(pk=cluster.pk)
                state = ClusterMembershipState.objects.select_for_update().filter(cluster=locked).first()
                if typed_cluster_key != locked.key:
                    raise ClusterTopologyHandoffError("Type the exact permanent cluster key to discard the evidence.")
                if state is None or not state.transition_pending or state.pending_role_is_readable:
                    raise ClusterTopologyHandoffError("There is no unreadable pending topology evidence to repair.")
                discarded = state.pending_topology_role
                state.transition_pending = False
                state.pending_topology_role = TopologyRole.UNKNOWN.value
                state.save(update_fields=["transition_pending", "pending_topology_role", "updated_at"])
                record_audit_event(
                    user=actor,
                    action="cluster.topology_pending_repaired",
                    object_type="cluster",
                    object_id=locked.key,
                    cluster=locked,
                    details={"cluster_key": locked.key, "discarded_pending_role": discarded},
                )
    except ClusterTopologyHandoffError as exc:
        record_audit_event(
            user=actor,
            action="cluster.topology_pending_repaired",
            object_type="cluster",
            object_id=cluster.key,
            outcome="refused",
            cluster=cluster,
            details={"cluster_key": cluster.key, "reason_code": exc.error_code},
        )
        raise


def _membership_recovery_read(cluster: ProxmoxCluster, endpoint_id: int | None = None):
    endpoints = enabled_endpoints(cluster)
    endpoint = next((row for row in endpoints if endpoint_id is None or row.pk == endpoint_id), None)
    if endpoint is None:
        raise ClusterTopologyHandoffError("Choose an enabled endpoint for membership recovery.")
    verify_registered_endpoint(cluster, endpoint)
    try:
        normalized = normalize_cluster_status(client_for_endpoint(endpoint).get("cluster/status"))
    except (InvalidMembershipPayload, ProxmoxAPIError) as exc:
        raise ClusterTopologyHandoffError("The endpoint did not return a fresh complete membership response.") from exc
    payload = {
        "endpoint_id": endpoint.pk,
        "members": [row.node_name for row in normalized.nodes],
        "observed_from": normalized.observed_from,
        "has_cluster_row": normalized.has_cluster_row,
    }
    role = TopologyRole.COROSYNC if normalized.has_cluster_row else TopologyRole.STANDALONE
    return (
        endpoint,
        normalized,
        MembershipRecoveryCandidate(
            endpoint_id=endpoint.pk,
            endpoint_name=endpoint.name,
            members=tuple(payload["members"]),
            observed_from=normalized.observed_from,
            topology_role=role,
            digest=_hash(payload),
        ),
    )


def inspect_membership_recovery(
    cluster: ProxmoxCluster, *, endpoint_id: int | None = None
) -> MembershipRecoveryCandidate:
    coverage = ClusterProjectionCoverage.objects.filter(
        cluster=cluster,
        domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP,
        node_name__isnull=True,
    ).first()
    if coverage is None or coverage.error_code != ERROR_OBSERVER_NOT_MEMBER:
        raise ClusterTopologyHandoffError("Membership recovery is available only for observer_not_a_member.")
    _endpoint, _normalized, candidate = _membership_recovery_read(cluster, endpoint_id)
    return candidate


def confirm_membership_recovery(
    cluster: ProxmoxCluster,
    *,
    endpoint_id: int,
    expected_digest: str,
    actor,
) -> None:
    with transaction.atomic():
        with cluster_lifecycle_lock(cluster):
            locked = historical_clusters().select_for_update().get(pk=cluster.pk)
            coverage = (
                ClusterProjectionCoverage.objects.select_for_update()
                .filter(
                    cluster=locked,
                    domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP,
                    node_name__isnull=True,
                )
                .first()
            )
            if coverage is None or coverage.error_code != ERROR_OBSERVER_NOT_MEMBER:
                raise ClusterTopologyHandoffError("The stale-observer condition no longer exists.")
            _endpoint, normalized, candidate = _membership_recovery_read(locked, endpoint_id)
            if candidate.digest != expected_digest:
                raise ClusterTopologyHandoffError("The candidate member set changed. Review it again.")
            state = ClusterMembershipState.objects.select_for_update().filter(cluster=locked).first()
            stored_role = state.role() if state is not None else TopologyRole.UNKNOWN
            pending_role = state.pending_role() if state is not None else TopologyRole.UNKNOWN
            decision = evaluate_role_transition(
                stored_role, normalized.observation(frozenset()), pending_role=pending_role
            )
            generation, _error = _publish_complete(locked, normalized, decision, observed_at=timezone.now())
            record_audit_event(
                user=actor,
                action="cluster.topology_membership_recovered",
                object_type="cluster",
                object_id=locked.key,
                cluster=locked,
                details={
                    "cluster_key": locked.key,
                    "endpoint_id": endpoint_id,
                    "accepted_members": list(candidate.members),
                    "observed_from": candidate.observed_from,
                    "membership_generation": generation,
                },
            )


def apply_topology_handoff_storage_bindings(cluster: ProxmoxCluster, *, metadata_generation) -> None:
    """Apply/refuse pending intents after this cluster's own complete metadata."""
    intents = list(
        ClusterTopologyHandoffStorageBinding.objects.select_for_update()
        .filter(cluster=cluster, status=ClusterTopologyHandoffStorageBinding.Status.PENDING)
        .select_related("mount")
        .order_by("pk")
    )
    if not intents:
        return
    catalog_state = StorageCatalogState.objects.select_for_update().filter(cluster=cluster).first()
    if (
        catalog_state is None
        or not catalog_state.metadata_complete
        or catalog_state.metadata_generation is None
        or catalog_state.metadata_generation != metadata_generation
    ):
        raise ClusterTopologyHandoffError(
            "Storage hand-off mappings require the replacement's current complete metadata generation."
        )
    definitions = {
        row.storage_id: row
        for row in ClusterStorage.objects.select_for_update().filter(
            cluster=cluster,
            present=True,
            unmanaged_at__isnull=True,
            observed_metadata_generation=metadata_generation,
        )
    }
    node_states = {
        (row.cluster_storage_id, row.node)
        for row in ClusterStorageNodeState.objects.select_for_update().filter(
            cluster_storage__in=definitions.values(),
            present=True,
            observed_metadata_generation=metadata_generation,
        )
    }
    existing_targets = {
        (row.cluster_storage_id, row.node)
        for row in ClusterStorageMount.objects.select_for_update().filter(cluster_storage__in=definitions.values())
    }
    refusal = ""
    for intent in intents:
        definition = definitions.get(intent.storage_id)
        if definition is None:
            refusal = f"Storage '{intent.storage_id}' was absent from complete replacement metadata."
            break
        if definition.shared != (intent.scope == ClusterStorageMount.Scope.SHARED):
            refusal = f"Storage '{intent.storage_id}' changed shared/node scope."
            break
        if intent.scope == ClusterStorageMount.Scope.NODE and definition.nodes and intent.node not in definition.nodes:
            refusal = f"Storage '{intent.storage_id}' no longer permits node '{intent.node}'."
            break
        if intent.scope == ClusterStorageMount.Scope.NODE and (definition.pk, intent.node) not in node_states:
            refusal = f"Storage '{intent.storage_id}' has no complete present metadata for node '{intent.node}'."
            break
        if (definition.pk, intent.node) in existing_targets:
            refusal = (
                f"Storage '{intent.storage_id}' already has a binding for the confirmed scope and node; "
                "it may have changed after hand-off."
            )
            break
    now = timezone.now()
    if refusal:
        for intent in intents:
            intent.status = ClusterTopologyHandoffStorageBinding.Status.REFUSED
            intent.refusal_reason = refusal
            intent.save(update_fields=["status", "refusal_reason", "updated_at"])
        action = "cluster.topology_handoff_storage_refused"
        outcome = "refused"
    else:
        for intent in intents:
            definition = definitions[intent.storage_id]
            ClusterStorageMount.objects.create(
                cluster_storage=definition,
                node=intent.node,
                mount=intent.mount,
                scope=intent.scope,
            )
            intent.status = ClusterTopologyHandoffStorageBinding.Status.APPLIED
            intent.applied_at = now
            intent.save(update_fields=["status", "applied_at", "updated_at"])
        action = "cluster.topology_handoff_storage_applied"
        outcome = "success"
    record_audit_event(
        action=action,
        object_type="cluster_storage_handoff",
        object_id=cluster.key,
        outcome=outcome,
        system_username="system",
        cluster=cluster,
        details={
            "cluster_key": cluster.key,
            "intent_ids": [row.pk for row in intents],
            "reason": refusal,
            "error_reason": refusal,
            "bindings": [
                {
                    "storage_id": row.storage_id,
                    "scope": row.scope,
                    "node": row.node or "",
                    "mount_id": row.mount_id,
                    "status": row.status,
                }
                for row in intents
            ],
        },
    )

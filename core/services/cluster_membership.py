"""Canonical ``cluster/status`` normalization and membership publication.

Module 5 phase 5a1B owns this provider boundary. One logical refresh stays inside
one cluster, tries each enabled endpoint at most once and publishes a single
atomic membership generation. Web processes consume the projection; they never
call this service while rendering.

Topology hand-off deliberately does not live here. 5a1G persists the state
machine's sticky target and supplies the operator exit; this publisher only opens
or automatically withdraws that gate together with one auditable generation.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from django.db import transaction
from django.utils import timezone

from core.models import (
    ClusterMembershipState,
    ClusterNodeState,
    ClusterProjectionCoverage,
    ProxmoxCluster,
)
from core.services.audit_events import record_audit_event
from core.services.cluster_lifecycle_lock import cluster_lifecycle_lock
from core.services.cluster_projection import stamp_cluster_projection_footprint
from core.services.cluster_resolver import client_for_endpoint, enabled_endpoints
from core.services.cluster_scopes import historical_clusters
from core.services.cluster_topology_role import (
    MembershipObservation,
    RoleDecision,
    RoleTransition,
    TopologyRole,
    evaluate_role_transition,
)
from core.services.proxmox import (
    ProxmoxAPIError,
    ProxmoxInvalidResponseError,
    ProxmoxTransportError,
)

logger = logging.getLogger(__name__)

ERROR_TOPOLOGY_ROLE_CHANGE = "topology_role_change_observed"
ERROR_OBSERVER_NOT_MEMBER = "observer_not_a_member"
ERROR_PROVIDER_UNAUTHORIZED = "provider_unauthorized"
ERROR_PROVIDER_TIMEOUT = "provider_timeout"
ERROR_INVALID_PAYLOAD = "invalid_payload"
ERROR_PROVIDER = "provider_error"

ERROR_ACQUISITION_DISABLED = "acquisition_disabled"
ERROR_ACQUISITION_QUARANTINED = "acquisition_quarantined"
ERROR_ACQUISITION_RETIRED = "acquisition_retired"
ERROR_NO_ENABLED_ENDPOINT = "no_enabled_endpoint"


class InvalidMembershipPayload(ValueError):
    """A successful HTTP response was not a complete membership snapshot."""


@dataclass(frozen=True)
class MembershipNodeObservation:
    node_name: str
    nodeid: int
    online: bool
    ring_address: str


@dataclass(frozen=True)
class NormalizedMembership:
    nodes: tuple[MembershipNodeObservation, ...]
    has_cluster_row: bool
    quorate: bool
    observed_from: str

    def observation(self, accepted_members: frozenset[str]) -> MembershipObservation:
        return MembershipObservation(
            complete=True,
            has_cluster_row=self.has_cluster_row,
            member_count=len(self.nodes),
            quorate=self.quorate,
            observed_from=self.observed_from,
            accepted_members=accepted_members,
        )


@dataclass(frozen=True)
class MembershipEndpointAttempt:
    endpoint_name: str
    error_code: str
    observed_from: str = ""

    @property
    def complete(self) -> bool:
        return not self.error_code


@dataclass(frozen=True)
class MembershipRefreshResult:
    cluster_key: str
    generation: int
    complete: bool
    error_code: str
    observed_from: str = ""
    attempts: tuple[MembershipEndpointAttempt, ...] = ()
    role_decision: RoleDecision | None = None


def _required_str(row: Mapping[str, Any], key: str, *, nonempty: bool = False) -> str:
    value = row.get(key)
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise InvalidMembershipPayload(f"Membership row field {key!r} is not a valid string.")
    return value.strip() if nonempty else value


def _required_int(row: Mapping[str, Any], key: str, *, minimum: int = 0) -> int:
    value = row.get(key)
    if type(value) is not int or value < minimum:
        raise InvalidMembershipPayload(f"Membership row field {key!r} is not a valid integer.")
    return value


def _binary_int(row: Mapping[str, Any], key: str) -> bool:
    value = _required_int(row, key)
    if value not in (0, 1):
        raise InvalidMembershipPayload(f"Membership row field {key!r} is not binary.")
    return bool(value)


def _optional_binary_int(row: Mapping[str, Any], key: str) -> bool:
    if key not in row:
        return False
    return _binary_int(row, key)


def normalize_cluster_status(payload: object) -> NormalizedMembership:
    """Validate and normalize one complete Proxmox ``cluster/status`` body."""

    if not isinstance(payload, list) or not payload:
        raise InvalidMembershipPayload("Membership payload must be a nonempty list.")
    if any(not isinstance(item, Mapping) for item in payload):
        raise InvalidMembershipPayload("Every membership row must be a mapping.")

    rows = tuple(payload)
    node_rows = [row for row in rows if row.get("type") == "node"]
    cluster_rows = [row for row in rows if row.get("type") == "cluster"]
    if len(node_rows) + len(cluster_rows) != len(rows):
        raise InvalidMembershipPayload("Membership payload contains an unknown row type.")
    if not node_rows or len(cluster_rows) > 1:
        raise InvalidMembershipPayload("Membership payload has an ambiguous cluster shape.")

    nodes: list[MembershipNodeObservation] = []
    names: set[str] = set()
    local_names: list[str] = []
    for row in node_rows:
        node_name = _required_str(row, "name", nonempty=True)
        if ":" in node_name or node_name in names:
            raise InvalidMembershipPayload("Membership node names must be unique valid NodeRefs.")
        names.add(node_name)
        ring_address = _required_str(row, "ip") if "ip" in row else ""
        # A standalone node reports `nodeid: 0` -- corosync is what assigns one, and
        # there is no corosync. Requiring >= 1 here rejected the real standalone
        # payload as `invalid_payload`, so a standalone host never published
        # membership at all and could not be told apart from an unreadable cluster.
        # The corosync branch below still requires a real nodeid.
        nodeid = _required_int(row, "nodeid", minimum=0)
        online = _binary_int(row, "online")
        local = _optional_binary_int(row, "local")
        if local:
            local_names.append(node_name)
        nodes.append(
            MembershipNodeObservation(
                node_name=node_name,
                nodeid=nodeid,
                online=online,
                ring_address=ring_address,
            )
        )

    if len(local_names) != 1:
        raise InvalidMembershipPayload("Membership payload must identify exactly one local node.")
    if not cluster_rows:
        if len(nodes) != 1:
            raise InvalidMembershipPayload("A standalone response must contain exactly one node.")
        quorate = False
    else:
        # Inside a corosync cluster every member has a corosync-assigned nodeid, so
        # a zero here is a malformed answer rather than the standalone shape.
        if any(node.nodeid == 0 for node in nodes):
            raise InvalidMembershipPayload("A clustered node must carry a corosync nodeid.")
        cluster_row = cluster_rows[0]
        reported_nodes = _required_int(cluster_row, "nodes", minimum=1)
        if reported_nodes != len(nodes):
            raise InvalidMembershipPayload("Cluster node count does not match its node rows.")
        quorate = _binary_int(cluster_row, "quorate")

    return NormalizedMembership(
        nodes=tuple(sorted(nodes, key=lambda item: item.node_name)),
        has_cluster_row=bool(cluster_rows),
        quorate=quorate,
        observed_from=local_names[0],
    )


def _provider_error_code(exc: ProxmoxAPIError) -> str:
    if isinstance(exc, ProxmoxInvalidResponseError):
        return ERROR_INVALID_PAYLOAD
    if isinstance(exc, ProxmoxTransportError):
        if isinstance(exc.__cause__, httpx.TimeoutException):
            return ERROR_PROVIDER_TIMEOUT
        return ERROR_PROVIDER
    if exc.status_code in (401, 403):
        return ERROR_PROVIDER_UNAUTHORIZED
    # Compatibility for errors created before ProxmoxAPIError gained structured
    # status provenance. U1 captured this exact transport encoding: a leading
    # decimal HTTP code, then a colon and provider detail. Only the code token is
    # interpreted; the provider prose is never a decision input.
    leading_status, separator, _detail = str(exc).partition(":")
    if separator and leading_status.strip() in ("401", "403"):
        return ERROR_PROVIDER_UNAUTHORIZED
    cause = exc.__cause__
    if isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code in (401, 403):
        return ERROR_PROVIDER_UNAUTHORIZED
    return ERROR_PROVIDER


def _stored_generation(cluster: ProxmoxCluster) -> int:
    state = ClusterMembershipState.objects.filter(cluster=cluster).only("membership_generation").first()
    return state.membership_generation if state is not None else 0


def _accepted_members(cluster: ProxmoxCluster) -> frozenset[str]:
    state = ClusterMembershipState.objects.filter(cluster=cluster).only("membership_generation").first()
    coverage = (
        ClusterProjectionCoverage.objects.filter(
            cluster=cluster,
            domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP,
            node_name__isnull=True,
        )
        .only("observed_at")
        .first()
    )
    if state is None or coverage is None or coverage.observed_at is None:
        return frozenset()
    return frozenset(
        ClusterNodeState.objects.filter(
            cluster=cluster,
            present=True,
            membership_generation=state.membership_generation,
        ).values_list("node_name", flat=True)
    )


def _publish_incomplete(cluster: ProxmoxCluster, *, attempted_at, error_code: str) -> int:
    coverage, _created = ClusterProjectionCoverage.objects.select_for_update().get_or_create(
        cluster=cluster,
        domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP,
        node_name=None,
    )
    coverage.complete = False
    coverage.attempted_at = attempted_at
    coverage.error_code = error_code
    coverage.save(update_fields=["complete", "attempted_at", "error_code", "updated_at"])
    stamp_cluster_projection_footprint(cluster)
    return coverage.generation


def _publish_complete(
    cluster: ProxmoxCluster,
    normalized: NormalizedMembership,
    decision: RoleDecision,
    *,
    observed_at,
) -> tuple[int, str]:
    state, _created = ClusterMembershipState.objects.select_for_update().get_or_create(cluster=cluster)
    generation = state.membership_generation + 1
    error_code = ""
    opened = False
    withdrawn_role = ""

    if decision.transition in (RoleTransition.ADOPTED, RoleTransition.STABLE):
        state.topology_role = decision.role.value
    elif decision.transition is RoleTransition.TRANSITION_PENDING:
        error_code = ERROR_TOPOLOGY_ROLE_CHANGE
        opened = not state.transition_pending
        state.transition_pending = True
        state.pending_topology_role = decision.pending_role.value
    elif decision.transition is RoleTransition.TRANSITION_WITHDRAWN:
        withdrawn_role = state.pending_topology_role
        state.transition_pending = False
        state.pending_topology_role = TopologyRole.UNKNOWN.value
    elif (
        decision.transition is RoleTransition.INDETERMINATE
        and state.transition_pending
        and not state.pending_role_is_readable
    ):
        # A newer build (or a hand edit) asked a question this binary cannot
        # interpret. Membership facts may stay current, but only the explicit
        # Connections repair may discard that fail-closed evidence.
        error_code = ERROR_TOPOLOGY_ROLE_CHANGE
    else:
        raise RuntimeError(f"Unexpected complete membership decision: {decision.transition}")

    state.membership_generation = generation
    state.member_count = len(normalized.nodes)
    state.quorate = normalized.quorate
    state.observed_from = normalized.observed_from
    state.save()

    if opened:
        record_audit_event(
            action="cluster.topology_transition_detected",
            object_type="cluster",
            object_id=cluster.key,
            outcome="warning",
            system_username="system",
            cluster=cluster,
            details={
                "cluster_key": cluster.key,
                "registered_role": decision.previous_role.value,
                "pending_role": decision.pending_role.value,
                "operator_confirmation_required": True,
            },
        )
    elif decision.transition is RoleTransition.TRANSITION_WITHDRAWN:
        record_audit_event(
            action="cluster.topology_transition_withdrawn",
            object_type="cluster",
            object_id=cluster.key,
            system_username="system",
            cluster=cluster,
            details={
                "cluster_key": cluster.key,
                "registered_role": decision.role.value,
                "withdrawn_pending_role": withdrawn_role,
            },
        )

    observed_names = {item.node_name for item in normalized.nodes}
    existing = {item.node_name: item for item in ClusterNodeState.objects.select_for_update().filter(cluster=cluster)}
    for item in normalized.nodes:
        row = existing.get(item.node_name)
        if row is None:
            row = ClusterNodeState(
                cluster=cluster,
                node_name=item.node_name,
                first_discovered_at=observed_at,
            )
        elif row.first_discovered_at is None:
            row.first_discovered_at = observed_at
        row.nodeid = item.nodeid
        row.present = True
        row.online = item.online
        row.reported_ring_address = item.ring_address
        row.membership_generation = generation
        row.last_discovered_at = observed_at
        row.save()

    # Re-prove absence at every complete generation, including rows that were
    # already absent. Otherwise an old ``membership_generation`` would make a
    # current complete absence look stale to later composed readers.
    ClusterNodeState.objects.filter(cluster=cluster).exclude(node_name__in=observed_names).update(
        present=False,
        online=False,
        membership_generation=generation,
        updated_at=observed_at,
    )

    coverage, _created = ClusterProjectionCoverage.objects.select_for_update().get_or_create(
        cluster=cluster,
        domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP,
        node_name=None,
    )
    coverage.generation = generation
    coverage.complete = True
    coverage.attempted_at = observed_at
    coverage.observed_at = observed_at
    coverage.error_code = error_code
    coverage.save()
    stamp_cluster_projection_footprint(cluster)
    return generation, error_code


def _zero_call_result(cluster: ProxmoxCluster, error_code: str) -> MembershipRefreshResult:
    return MembershipRefreshResult(
        cluster_key=cluster.key,
        generation=_stored_generation(cluster),
        complete=False,
        error_code=error_code,
    )


@transaction.atomic
def refresh_cluster_membership(cluster: ProxmoxCluster, *, observed_at=None) -> MembershipRefreshResult:
    """Acquire and publish one cluster's membership under its lifecycle barrier."""

    with cluster_lifecycle_lock(cluster):
        # Historical scope is deliberate: a late queued refresh must observe a
        # retired row and return without recreating the finalized projection.
        locked = historical_clusters().select_for_update().get(pk=cluster.pk)
        if locked.retired_at is not None:
            return _zero_call_result(locked, ERROR_ACQUISITION_RETIRED)
        if not locked.enabled:
            return _zero_call_result(locked, ERROR_ACQUISITION_DISABLED)
        if locked.ingestion_quarantined:
            return _zero_call_result(locked, ERROR_ACQUISITION_QUARANTINED)

        endpoints = enabled_endpoints(locked)
        if not endpoints:
            return _zero_call_result(locked, ERROR_NO_ENABLED_ENDPOINT)

        accepted_members = _accepted_members(locked)
        attempts: list[MembershipEndpointAttempt] = []
        outside_observers: list[str] = []
        normalized: NormalizedMembership | None = None
        observation: MembershipObservation | None = None

        for endpoint in endpoints:
            client = client_for_endpoint(endpoint)
            try:
                payload = client.get("cluster/status")
                candidate = normalize_cluster_status(payload)
            except InvalidMembershipPayload:
                logger.warning(
                    "Invalid membership response: cluster=%s endpoint=%s",
                    locked.key,
                    endpoint.name,
                    exc_info=True,
                )
                attempts.append(MembershipEndpointAttempt(endpoint.name, ERROR_INVALID_PAYLOAD))
                continue
            except ProxmoxAPIError as exc:
                code = _provider_error_code(exc)
                logger.warning(
                    "Membership read failed: cluster=%s endpoint=%s error_type=%s",
                    locked.key,
                    endpoint.name,
                    exc.__class__.__name__,
                    exc_info=True,
                )
                attempts.append(MembershipEndpointAttempt(endpoint.name, code))
                continue

            candidate_observation = candidate.observation(accepted_members)
            if not candidate_observation.speaks_for_the_scope:
                outside_observers.append(candidate.observed_from)
                attempts.append(
                    MembershipEndpointAttempt(
                        endpoint.name,
                        ERROR_OBSERVER_NOT_MEMBER,
                        candidate.observed_from,
                    )
                )
                continue

            attempts.append(MembershipEndpointAttempt(endpoint.name, "", candidate.observed_from))
            normalized = candidate
            observation = candidate_observation
            break

        if normalized is None or observation is None:
            attempted_at = observed_at or timezone.now()
            if outside_observers:
                error_code = ERROR_OBSERVER_NOT_MEMBER
                observed_from = outside_observers[0]
            else:
                codes = {attempt.error_code for attempt in attempts}
                error_code = next(iter(codes)) if len(codes) == 1 else ERROR_PROVIDER
                observed_from = ""
            generation = _publish_incomplete(locked, attempted_at=attempted_at, error_code=error_code)
            return MembershipRefreshResult(
                cluster_key=locked.key,
                generation=generation,
                complete=False,
                error_code=error_code,
                observed_from=observed_from,
                attempts=tuple(attempts),
            )

        state = ClusterMembershipState.objects.filter(cluster=locked).first()
        stored_role = state.role() if state is not None else TopologyRole.UNKNOWN
        pending_role = state.pending_role() if state is not None else TopologyRole.UNKNOWN
        if state is not None and state.transition_pending and not state.pending_role_is_readable:
            decision = RoleDecision(
                transition=RoleTransition.INDETERMINATE,
                role=stored_role,
                previous_role=stored_role,
                reason="The pending topology role was written by a newer build and requires operator repair.",
            )
        else:
            decision = evaluate_role_transition(stored_role, observation, pending_role=pending_role)
        published_at = observed_at or timezone.now()
        generation, error_code = _publish_complete(
            locked,
            normalized,
            decision,
            observed_at=published_at,
        )
        return MembershipRefreshResult(
            cluster_key=locked.key,
            generation=generation,
            complete=True,
            error_code=error_code,
            observed_from=normalized.observed_from,
            attempts=tuple(attempts),
            role_decision=decision,
        )

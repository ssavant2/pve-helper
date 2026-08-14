"""Cluster-retirement preflight, signed evidence and atomic coordinator.

Provider verification deliberately lives outside the eventual retirement
transaction.  Verified retirement reads exactly one operator-selected endpoint;
forced retirement never constructs a provider client.  The signed evidence binds
the local state that the final transaction must re-check before it mutates
anything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from django.core import signing
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from core.models import (
    ClusterCredential,
    ClusterMembershipState,
    ClusterNodeEnrollment,
    ClusterNodeInterface,
    ClusterNodeState,
    ClusterProjectionCoverage,
    ClusterStorage,
    ClusterStorageMount,
    ClusterStorageNodeState,
    ClusterStorageVolumeCoverage,
    ClusterStorageVolumeObservation,
    ClusterTopologyHandoffStorageBinding,
    ClusterTransportTrust,
    CurrentGuestInventory,
    CurrentGuestInventoryState,
    ProxmoxCluster,
    ProxmoxEndpoint,
    ProxmoxStorageConsumer,
    ScanRun,
    ScheduledAction,
    StorageCatalogState,
)
from core.services.audit_events import (
    AuditRetirementBlocked,
    AuditRetirementPreflight,
    AuditRetirementResult,
    cluster_retirement_audit_preflight,
    finalize_cluster_retirement_audit_operations,
    record_audit_event,
)
from core.services.cluster_enrollment import EnrollmentRetirementResult, retire_cluster_enrollments
from core.services.cluster_identity import ClusterIdentityError, discover_cluster_identity
from core.services.cluster_lifecycle_lock import cluster_lifecycle_lock, scan_admission_lock
from core.services.cluster_projection import ClusterProjectionRetirementResult, retire_cluster_projection
from core.services.cluster_resolver import ClusterResolutionError, client_for_endpoint, enabled_endpoints
from core.services.cluster_scopes import historical_clusters
from core.services.cluster_state_identity import invalidate_cluster_cache
from core.services.cluster_trust import reset_trust_pools
from core.services.console_sessions import (
    ConsoleRetirementBlocked,
    ConsoleRetirementPreflight,
    ConsoleRetirementResult,
    cluster_retirement_console_preflight,
    finalize_cluster_retirement_consoles,
)
from core.services.current_guest_inventory import GuestInventoryRetirementResult, retire_cluster_guest_inventory
from core.services.public_errors import PublicMessageError
from core.services.scheduled_actions import (
    ScheduledActionRetirementBlocked,
    ScheduledActionRetirementPreflight,
    ScheduledActionRetirementResult,
    cluster_retirement_scheduled_actions_preflight,
    finalize_cluster_retirement_scheduled_actions,
)
from core.services.storage_retirement import (
    StorageRetirementConsumersBlock,
    StorageRetirementImpactChanged,
    StorageRetirementPreflight,
    StorageRetirementResult,
    cluster_retirement_storage_preflight,
    finalize_cluster_retirement_storage,
)

RETIREMENT_PREFLIGHT_SALT = "pve-helper.cluster-retirement-preflight.v1"
RETIREMENT_PREFLIGHT_MAX_AGE_SECONDS = 10 * 60
RETIREMENT_REASON_MAX_LENGTH = ProxmoxCluster._meta.get_field("retirement_reason").max_length
RETIREMENT_AUDIT_DETAIL_LIMIT = 100

ERROR_CODE_PREFLIGHT_INVALID = "cluster_retirement_preflight_invalid"
ERROR_CODE_PREFLIGHT_CHANGED = "cluster_retirement_preflight_changed"
ERROR_CODE_PREFLIGHT_NOT_ALLOWED = "cluster_retirement_preflight_not_allowed"
ERROR_CODE_PREFLIGHT_ENDPOINT = "cluster_retirement_preflight_endpoint"
ERROR_CODE_PREFLIGHT_UNREACHABLE = "cluster_retirement_preflight_unreachable"
ERROR_CODE_PREFLIGHT_IDENTITY_MISMATCH = "cluster_retirement_preflight_identity_mismatch"
ERROR_CODE_RETIREMENT_CONFIRMATION = "cluster_retirement_confirmation_required"
ERROR_CODE_RETIREMENT_BLOCKED = "cluster_retirement_blocked"
ERROR_CODE_RETIREMENT_ACTIVE_SCAN = "cluster_retirement_active_scan"
ERROR_CODE_RETIREMENT_POSTCONDITION = "cluster_retirement_postcondition_failed"
ERROR_CODE_RETIREMENT_FAILED = "cluster_retirement_failed"

logger = logging.getLogger(__name__)

_TOKEN_FIELDS = frozenset(
    {
        "version",
        "cluster_pk",
        "cluster_key",
        "mode",
        "endpoint_id",
        "pinned_ca_uuid",
        "lifecycle_generation",
        "credential_version",
        "trust_version",
        "storage_impact_digest",
        "issued_at",
        "identity_verification",
        "replacement_ca_uuid",
    }
)


class RetirementPreflightError(PublicMessageError, RuntimeError):
    """Base class for safe, stable retirement-preflight refusals."""


class RetirementPreflightInvalid(RetirementPreflightError):
    error_code = ERROR_CODE_PREFLIGHT_INVALID


class RetirementPreflightChanged(RetirementPreflightError):
    error_code = ERROR_CODE_PREFLIGHT_CHANGED


class RetirementPreflightNotAllowed(RetirementPreflightError):
    error_code = ERROR_CODE_PREFLIGHT_NOT_ALLOWED


class RetirementPreflightEndpointError(RetirementPreflightError):
    error_code = ERROR_CODE_PREFLIGHT_ENDPOINT


class RetirementPreflightUnavailable(RetirementPreflightError):
    error_code = ERROR_CODE_PREFLIGHT_UNREACHABLE


class RetirementPreflightIdentityMismatch(RetirementPreflightError):
    error_code = ERROR_CODE_PREFLIGHT_IDENTITY_MISMATCH

    def __init__(self, *, observed_uuid: str, pinned_uuid: str):
        self.observed_uuid = observed_uuid
        self.pinned_uuid = pinned_uuid
        super().__init__(
            "The selected endpoint reports a different Proxmox cluster identity. "
            "Verified retirement was refused; use the separately confirmed forced path "
            "only if the original site is permanently unavailable."
        )


class ClusterRetirementError(PublicMessageError, RuntimeError):
    """Base class for stable, public final-retirement refusals."""


class ClusterRetirementConfirmationRequired(ClusterRetirementError):
    error_code = ERROR_CODE_RETIREMENT_CONFIRMATION


class ClusterRetirementBlocked(ClusterRetirementError):
    error_code = ERROR_CODE_RETIREMENT_BLOCKED

    def __init__(self, message: str, *, blocker_code: str):
        self.blocker_code = blocker_code
        super().__init__(message)


class ClusterRetirementActiveScan(ClusterRetirementBlocked):
    error_code = ERROR_CODE_RETIREMENT_ACTIVE_SCAN

    def __init__(self):
        super().__init__(
            "Retirement is blocked while an installation-wide scan is queued or running.",
            blocker_code="active_scan",
        )


class ClusterRetirementPostconditionFailed(ClusterRetirementError):
    error_code = ERROR_CODE_RETIREMENT_POSTCONDITION


class ClusterRetirementFailed(ClusterRetirementError):
    error_code = ERROR_CODE_RETIREMENT_FAILED


@dataclass(frozen=True)
class RetirementPreflight:
    mode: str
    endpoint_id: int | None
    identity_verification: str
    observed_at: datetime
    storage: StorageRetirementPreflight
    scheduled_actions: ScheduledActionRetirementPreflight
    consoles: ConsoleRetirementPreflight
    audit_operations: AuditRetirementPreflight
    active_scan_count: int
    blocker_codes: tuple[str, ...]
    confirmation: str

    @property
    def gate_clear(self) -> bool:
        return not self.blocker_codes


@dataclass(frozen=True)
class ConfirmedRetirementPreflight:
    cluster_pk: int
    cluster_key: str
    mode: str
    endpoint_id: int | None
    pinned_ca_uuid: str
    lifecycle_generation: int
    credential_version: str
    trust_version: str
    storage_impact_digest: str
    issued_at: int
    identity_verification: str
    replacement_ca_uuid: str


@dataclass(frozen=True)
class RetirementResult:
    cluster_pk: int
    cluster_key: str
    mode: str
    retired_at: datetime
    audit_event_id: int
    scheduled_actions: ScheduledActionRetirementResult
    consoles: ConsoleRetirementResult
    audit_operations: AuditRetirementResult
    storage: StorageRetirementResult
    guest_inventory: GuestInventoryRetirementResult
    host_projection: ClusterProjectionRetirementResult
    enrollments: EnrollmentRetirementResult
    endpoints_deleted: int
    credential_deleted: bool
    trust_deleted: bool


def _normalize_mode(mode: str) -> str:
    value = str(getattr(mode, "value", mode))
    if value not in {
        ProxmoxCluster.RetirementMode.VERIFIED,
        ProxmoxCluster.RetirementMode.FORCED,
    }:
        raise RetirementPreflightInvalid("Choose verified or forced retirement.")
    return value


def _configuration_version(model, cluster_id: int) -> str:
    row = model.objects.filter(cluster_id=cluster_id).only("pk", "updated_at").first()
    if row is None:
        return ""
    return f"{row.pk}:{row.updated_at.isoformat()}"


def _credential_version(cluster_id: int) -> str:
    return _configuration_version(ClusterCredential, cluster_id)


def _trust_version(cluster_id: int) -> str:
    return _configuration_version(ClusterTransportTrust, cluster_id)


def _current_cluster(cluster) -> ProxmoxCluster:
    if getattr(cluster, "pk", None) is None:
        raise RetirementPreflightInvalid("The cluster must be saved before retirement preflight.")
    try:
        current = historical_clusters().get(pk=cluster.pk)
    except ProxmoxCluster.DoesNotExist as exc:
        raise RetirementPreflightNotAllowed("The cluster connection no longer exists.") from exc
    if current.retired_at is not None:
        raise RetirementPreflightNotAllowed("This cluster connection has already been retired.")
    return current


def _selected_endpoint(cluster: ProxmoxCluster, endpoint_id) -> ProxmoxEndpoint:
    if endpoint_id is None:
        endpoints = enabled_endpoints(cluster)
        if endpoints:
            return endpoints[0]
        raise RetirementPreflightEndpointError("Choose an enabled endpoint for verified retirement.")
    if isinstance(endpoint_id, bool) or not isinstance(endpoint_id, int):
        raise RetirementPreflightEndpointError("Choose an enabled endpoint for verified retirement.")
    endpoint = (
        ProxmoxEndpoint.objects.select_related("cluster")
        .filter(pk=endpoint_id, cluster_id=cluster.pk, enabled=True)
        .first()
    )
    if endpoint is None:
        raise RetirementPreflightEndpointError(
            "The selected endpoint is no longer enabled for this cluster. Choose an endpoint again."
        )
    return endpoint


def _local_impact(cluster: ProxmoxCluster, mode: str):
    storage = cluster_retirement_storage_preflight(cluster, mode=mode)
    scheduled = cluster_retirement_scheduled_actions_preflight(cluster, mode=mode)
    consoles = cluster_retirement_console_preflight(cluster, mode=mode)
    audit = cluster_retirement_audit_preflight(cluster, mode=mode)
    active_scan_count = ScanRun.objects.filter(status__in=(ScanRun.Status.QUEUED, ScanRun.Status.RUNNING)).count()

    blockers: list[str] = []
    if active_scan_count:
        blockers.append("active_scan")
    if not storage.consumer_gate_clear:
        blockers.append("storage_consumers")
    if scheduled.unknown_run_count:
        blockers.append("scheduled_actions_unknown")
    elif mode == ProxmoxCluster.RetirementMode.VERIFIED and scheduled.active_run_count:
        blockers.append("scheduled_actions_active")
    if consoles.unknown_count:
        blockers.append("consoles_unknown")
    elif mode == ProxmoxCluster.RetirementMode.VERIFIED and consoles.active_count:
        blockers.append("consoles_active")
    if audit.unknown_count:
        blockers.append("audit_operations_unknown")
    elif mode == ProxmoxCluster.RetirementMode.VERIFIED and audit.running_count:
        blockers.append("audit_operations_active")
    return storage, scheduled, consoles, audit, active_scan_count, tuple(blockers)


def _verify_selected_endpoint(cluster: ProxmoxCluster, endpoint: ProxmoxEndpoint) -> None:
    if not cluster.discovered_ca_uuid:
        raise RetirementPreflightNotAllowed(
            "This cluster has no pinned Proxmox CA identity and cannot use verified retirement."
        )
    try:
        client = client_for_endpoint(endpoint)
        node = client.discover_node_name(endpoint.name)
        observed = discover_cluster_identity(client, node)
    except ClusterIdentityError as exc:
        raise RetirementPreflightUnavailable(
            "The selected endpoint could not report its Proxmox cluster identity."
        ) from exc
    except (ClusterResolutionError, PublicMessageError) as exc:
        raise RetirementPreflightUnavailable(
            "The selected endpoint cannot be verified with the stored connection configuration."
        ) from exc

    if observed.ca_uuid != cluster.discovered_ca_uuid:
        raise RetirementPreflightIdentityMismatch(
            observed_uuid=observed.ca_uuid,
            pinned_uuid=cluster.discovered_ca_uuid,
        )


def _payload(
    cluster: ProxmoxCluster,
    *,
    mode: str,
    endpoint_id: int | None,
    storage_impact_digest: str,
    issued_at: int,
    identity_verification: str,
    replacement_ca_uuid: str = "",
) -> dict:
    return {
        "version": 2,
        "cluster_pk": cluster.pk,
        "cluster_key": cluster.key,
        "mode": mode,
        "endpoint_id": endpoint_id,
        "pinned_ca_uuid": (cluster.discovered_ca_uuid if mode == ProxmoxCluster.RetirementMode.VERIFIED else ""),
        "lifecycle_generation": cluster.lifecycle_generation,
        "credential_version": _credential_version(cluster.pk),
        "trust_version": _trust_version(cluster.pk),
        "storage_impact_digest": storage_impact_digest,
        "issued_at": issued_at,
        "identity_verification": identity_verification,
        "replacement_ca_uuid": replacement_ca_uuid,
    }


def cluster_retirement_preflight(
    cluster,
    *,
    mode: str,
    endpoint_id: int | None = None,
) -> RetirementPreflight:
    """Build local impact and, when clear, mint short-lived retirement evidence."""
    mode = _normalize_mode(mode)
    cluster = _current_cluster(cluster)

    endpoint = None
    if mode == ProxmoxCluster.RetirementMode.VERIFIED:
        if cluster.enabled:
            raise RetirementPreflightNotAllowed("Disable the cluster before verified retirement.")
        endpoint = _selected_endpoint(cluster, endpoint_id)
    elif endpoint_id is not None:
        raise RetirementPreflightEndpointError("Forced retirement does not select or contact an endpoint.")

    storage, scheduled, consoles, audit, active_scan_count, blockers = _local_impact(cluster, mode)

    identity_verification = "skipped"
    if endpoint is not None:
        _verify_selected_endpoint(cluster, endpoint)
        identity_verification = "matched"

    observed_at = timezone.now()
    issued_at = int(observed_at.timestamp())
    confirmation = ""
    if not blockers:
        confirmation = signing.dumps(
            _payload(
                cluster,
                mode=mode,
                endpoint_id=endpoint.pk if endpoint is not None else None,
                storage_impact_digest=storage.impact_digest,
                issued_at=issued_at,
                identity_verification=identity_verification,
            ),
            salt=RETIREMENT_PREFLIGHT_SALT,
            compress=True,
        )

    return RetirementPreflight(
        mode=mode,
        endpoint_id=endpoint.pk if endpoint is not None else None,
        identity_verification=identity_verification,
        observed_at=observed_at,
        storage=storage,
        scheduled_actions=scheduled,
        consoles=consoles,
        audit_operations=audit,
        active_scan_count=active_scan_count,
        blocker_codes=blockers,
        confirmation=confirmation,
    )


def cluster_handoff_retirement_preflight(
    cluster,
    *,
    endpoint_id: int | None = None,
    replacement_ca_uuid: str,
) -> RetirementPreflight:
    """Verified-retirement evidence for a transition-blocked enabled identity.

    The ordinary retirement preflight deliberately requires an operator-disabled
    connection. A topology hand-off cannot disable before its replacement has
    passed onboarding verification without risking a strand, so this narrow seam
    performs the same identity and impact proof while leaving the row unchanged.
    The final coordinator disables under the lifecycle lock immediately before
    the ordinary retirement validator/finalizer consumes this token.
    """
    cluster = _current_cluster(cluster)
    pending = ClusterMembershipState.objects.filter(cluster=cluster, transition_pending=True).first()
    if pending is None or not pending.pending_role_is_readable:
        raise RetirementPreflightInvalid(
            "Topology hand-off retirement is available only for a readable pending identity transition."
        )
    endpoint = _selected_endpoint(cluster, endpoint_id)
    storage, scheduled, consoles, audit, active_scan_count, blockers = _local_impact(
        cluster,
        ProxmoxCluster.RetirementMode.VERIFIED,
    )
    replacement_ca_uuid = str(replacement_ca_uuid or "").strip()
    if not replacement_ca_uuid:
        raise RetirementPreflightInvalid("The replacement Proxmox CA identity is required.")
    if replacement_ca_uuid == cluster.discovered_ca_uuid:
        _verify_selected_endpoint(cluster, endpoint)
        identity_verification = "matched"
        token_replacement_ca_uuid = ""
    else:
        identity_verification = "superseded_by_verified_handoff"
        token_replacement_ca_uuid = replacement_ca_uuid
    observed_at = timezone.now()
    issued_at = int(observed_at.timestamp())
    confirmation = ""
    if not blockers:
        confirmation = signing.dumps(
            _payload(
                cluster,
                mode=ProxmoxCluster.RetirementMode.VERIFIED,
                endpoint_id=endpoint.pk,
                storage_impact_digest=storage.impact_digest,
                issued_at=issued_at,
                identity_verification=identity_verification,
                replacement_ca_uuid=token_replacement_ca_uuid,
            ),
            salt=RETIREMENT_PREFLIGHT_SALT,
            compress=True,
        )
    return RetirementPreflight(
        mode=ProxmoxCluster.RetirementMode.VERIFIED,
        endpoint_id=endpoint.pk,
        identity_verification=identity_verification,
        observed_at=observed_at,
        storage=storage,
        scheduled_actions=scheduled,
        consoles=consoles,
        audit_operations=audit,
        active_scan_count=active_scan_count,
        blocker_codes=blockers,
        confirmation=confirmation,
    )


def _load_confirmation(token: str) -> dict:
    try:
        payload = signing.loads(
            token,
            salt=RETIREMENT_PREFLIGHT_SALT,
            max_age=RETIREMENT_PREFLIGHT_MAX_AGE_SECONDS,
        )
    except (signing.BadSignature, signing.SignatureExpired) as exc:
        raise RetirementPreflightInvalid(
            "This retirement confirmation is invalid or has expired. Run preflight again."
        ) from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != _TOKEN_FIELDS
        or payload.get("version") != 2
        or isinstance(payload.get("cluster_pk"), bool)
        or not isinstance(payload.get("cluster_pk"), int)
        or isinstance(payload.get("lifecycle_generation"), bool)
        or not isinstance(payload.get("lifecycle_generation"), int)
        or isinstance(payload.get("issued_at"), bool)
        or not isinstance(payload.get("issued_at"), int)
        or not isinstance(payload.get("cluster_key"), str)
        or not isinstance(payload.get("mode"), str)
        or not isinstance(payload.get("pinned_ca_uuid"), str)
        or not isinstance(payload.get("credential_version"), str)
        or not isinstance(payload.get("trust_version"), str)
        or not isinstance(payload.get("storage_impact_digest"), str)
        or payload.get("identity_verification") not in {"matched", "skipped", "superseded_by_verified_handoff"}
        or not isinstance(payload.get("replacement_ca_uuid"), str)
        or (
            payload.get("endpoint_id") is not None
            and (isinstance(payload.get("endpoint_id"), bool) or not isinstance(payload.get("endpoint_id"), int))
        )
    ):
        raise RetirementPreflightInvalid("This retirement confirmation is invalid or has expired. Run preflight again.")
    return payload


def validate_retirement_preflight(
    token: str,
    *,
    cluster,
    mode: str,
) -> ConfirmedRetirementPreflight:
    """Validate signed evidence against current local state without provider I/O."""
    expected_mode = _normalize_mode(mode)
    payload = _load_confirmation(token)
    current = _current_cluster(cluster)

    if any(
        (
            payload["cluster_pk"] != current.pk,
            payload["cluster_key"] != current.key,
            payload["mode"] != expected_mode,
            payload["lifecycle_generation"] != current.lifecycle_generation,
            payload["credential_version"] != _credential_version(current.pk),
            payload["trust_version"] != _trust_version(current.pk),
        )
    ):
        raise RetirementPreflightChanged("The cluster connection changed after preflight. Run preflight again.")

    endpoint_id = payload["endpoint_id"]
    pinned_ca_uuid = payload["pinned_ca_uuid"]
    if expected_mode == ProxmoxCluster.RetirementMode.VERIFIED:
        if current.enabled:
            raise RetirementPreflightChanged("The cluster connection changed after preflight. Run preflight again.")
        try:
            endpoint = _selected_endpoint(current, endpoint_id)
        except RetirementPreflightEndpointError as exc:
            raise RetirementPreflightChanged(
                "The selected endpoint changed after preflight. Choose an endpoint again."
            ) from exc
        if pinned_ca_uuid != current.discovered_ca_uuid:
            raise RetirementPreflightChanged("The cluster identity changed after preflight. Run preflight again.")
        if payload["identity_verification"] == "superseded_by_verified_handoff":
            if not payload["replacement_ca_uuid"] or payload["replacement_ca_uuid"] == pinned_ca_uuid:
                raise RetirementPreflightInvalid(
                    "This retirement confirmation is invalid or has expired. Run preflight again."
                )
        elif payload["identity_verification"] != "matched" or payload["replacement_ca_uuid"]:
            raise RetirementPreflightInvalid(
                "This retirement confirmation is invalid or has expired. Run preflight again."
            )
        endpoint_id = endpoint.pk
    elif (
        endpoint_id is not None
        or pinned_ca_uuid != ""
        or payload["identity_verification"] != "skipped"
        or payload["replacement_ca_uuid"]
    ):
        raise RetirementPreflightInvalid("This retirement confirmation is invalid or has expired. Run preflight again.")

    storage = cluster_retirement_storage_preflight(current, mode=expected_mode)
    if payload["storage_impact_digest"] != storage.impact_digest:
        raise RetirementPreflightChanged("The storage impact changed after preflight. Review it again.")

    issued_at = payload["issued_at"]
    if issued_at > int(timezone.now().timestamp()) + 60:
        raise RetirementPreflightInvalid("This retirement confirmation is invalid or has expired. Run preflight again.")

    return ConfirmedRetirementPreflight(
        cluster_pk=current.pk,
        cluster_key=current.key,
        mode=expected_mode,
        endpoint_id=endpoint_id,
        pinned_ca_uuid=pinned_ca_uuid,
        lifecycle_generation=current.lifecycle_generation,
        credential_version=payload["credential_version"],
        trust_version=payload["trust_version"],
        storage_impact_digest=storage.impact_digest,
        issued_at=issued_at,
        identity_verification=payload["identity_verification"],
        replacement_ca_uuid=payload["replacement_ca_uuid"],
    )


def _forced_confirmation(
    *,
    cluster: ProxmoxCluster,
    mode: str,
    typed_cluster_key: str,
    permanent_unavailability_asserted: bool,
    reason: str,
) -> str:
    normalized_reason = str(reason or "").strip()
    if len(normalized_reason) > RETIREMENT_REASON_MAX_LENGTH:
        raise ClusterRetirementConfirmationRequired(
            f"The retirement reason may contain at most {RETIREMENT_REASON_MAX_LENGTH} characters."
        )
    if mode == ProxmoxCluster.RetirementMode.FORCED:
        if typed_cluster_key != cluster.key:
            raise ClusterRetirementConfirmationRequired("Type the exact permanent cluster key to force retirement.")
        if permanent_unavailability_asserted is not True:
            raise ClusterRetirementConfirmationRequired(
                "Confirm that the Proxmox site is permanently unavailable before forced retirement."
            )
        if not normalized_reason:
            raise ClusterRetirementConfirmationRequired("A reason is required for forced retirement.")
    return normalized_reason


def _endpoint_snapshots(cluster_id: int) -> tuple[tuple[dict[str, object], ...], int]:
    rows = list(
        ProxmoxEndpoint.objects.filter(cluster_id=cluster_id).order_by("pk").values("name", "normalized_url", "enabled")
    )
    snapshots = tuple(
        {
            "name": str(row["name"]),
            "url": str(row["normalized_url"]),
            "enabled": bool(row["enabled"]),
        }
        for row in rows[:RETIREMENT_AUDIT_DETAIL_LIMIT]
    )
    return snapshots, len(rows)


def _participant_details(
    scheduled: ScheduledActionRetirementResult,
    consoles: ConsoleRetirementResult,
    audit: AuditRetirementResult,
) -> dict[str, object]:
    return {
        "scheduled_runs": [
            {
                "id": participant.pk,
                "scheduled_action_id": participant.scheduled_action_id,
                "state": participant.status,
            }
            for participant in scheduled.participants
        ],
        "scheduled_runs_omitted": scheduled.participants_omitted,
        "consoles": [
            {
                "id": participant.pk,
                "state": participant.status,
                "target_type": participant.target_type,
                "target_vmid": participant.target_vmid,
                "target_node": participant.target_node,
            }
            for participant in consoles.participants
        ],
        "consoles_omitted": consoles.participants_omitted,
        "audit_operations": [
            {
                "id": participant.pk,
                "action": participant.action,
                "state": participant.outcome,
                "object_type": participant.object_type,
                "object_id": participant.object_id,
            }
            for participant in audit.participants
        ],
        "audit_operations_omitted": audit.participants_omitted,
    }


def _retirement_audit_details(
    cluster: ProxmoxCluster,
    *,
    confirmed: ConfirmedRetirementPreflight,
    reason: str,
    retired_at: datetime,
    endpoints: tuple[dict[str, object], ...],
    endpoint_count: int,
    credential_token_id: str,
    trust_mode: str,
    scheduled: ScheduledActionRetirementResult,
    consoles: ConsoleRetirementResult,
    audit: AuditRetirementResult,
    storage: StorageRetirementResult,
    guest_inventory: GuestInventoryRetirementResult,
    host_projection: ClusterProjectionRetirementResult,
    enrollments: EnrollmentRetirementResult,
) -> dict[str, object]:
    return {
        "display_name": cluster.display_name,
        "cluster_key": cluster.key,
        "retirement_mode": confirmed.mode,
        "retirement_reason": reason,
        "retired_at": retired_at.isoformat(),
        "identity_verification": confirmed.identity_verification,
        "replacement_ca_uuid": confirmed.replacement_ca_uuid,
        "identity_observed_at": datetime.fromtimestamp(confirmed.issued_at, tz=UTC).isoformat(),
        "verified_endpoint_id": confirmed.endpoint_id,
        "pinned_ca_uuid": cluster.discovered_ca_uuid,
        "pinned_ca_fingerprint": cluster.discovered_ca_fingerprint,
        "endpoint_count": endpoint_count,
        "endpoints": list(endpoints),
        "endpoints_omitted": max(0, endpoint_count - len(endpoints)),
        "credential_token_id": credential_token_id,
        "trust_mode": trust_mode,
        "cleanup": {
            "schedules_deleted": scheduled.schedules_deleted,
            "scheduled_runs_cancelled": scheduled.not_started_runs_cancelled,
            "scheduled_runs_abandoned": scheduled.active_runs_abandoned,
            "consoles_closed_before_start": consoles.pending_closed,
            "consoles_abandoned": consoles.active_closed,
            "console_sessions_sanitized": consoles.sessions_sanitized,
            "audit_operations_cancelled": audit.queued_cancelled,
            "audit_operations_abandoned": audit.running_abandoned,
            "storage_definitions_unmanaged": storage.definitions_unmanaged,
            "storage_node_states_deleted": storage.node_states_deleted,
            "storage_coverages_deleted": storage.coverages_deleted,
            "storage_observations_deleted": storage.observations_deleted,
            "storage_bindings_deleted": storage.bindings_deleted,
            "topology_handoff_storage_intents_deleted": storage.handoff_intents_deleted,
            "storage_consumers_deleted": storage.consumers_deleted,
            "storage_catalog_states_deleted": storage.catalog_states_deleted,
            "current_guests_deleted": guest_inventory.guest_rows_deleted,
            "current_guest_states_deleted": guest_inventory.state_rows_deleted,
            "cluster_membership_states_deleted": host_projection.membership_rows_deleted,
            "cluster_node_states_deleted": host_projection.node_rows_deleted,
            "cluster_projection_coverages_deleted": host_projection.coverage_rows_deleted,
            "cluster_node_interfaces_deleted": host_projection.interface_rows_deleted,
            "cluster_node_enrollments_deleted": enrollments.enrollment_rows_deleted,
        },
        "node_enrollments": list(enrollments.enrollments),
        "node_enrollments_omitted": enrollments.enrollments_omitted,
        "storage_mount_refs": list(storage.mount_refs),
        "storage_mount_refs_omitted": storage.mount_refs_omitted,
        "storage_consumer_refs": list(storage.consumer_refs),
        "storage_consumer_refs_omitted": storage.consumer_refs_omitted,
        "participants": _participant_details(scheduled, consoles, audit),
    }


def _raise_for_local_blockers(cluster: ProxmoxCluster, mode: str) -> None:
    _storage, _scheduled, _consoles, _audit, active_scan_count, blockers = _local_impact(cluster, mode)
    if active_scan_count:
        raise ClusterRetirementActiveScan()
    if blockers:
        raise ClusterRetirementBlocked(
            "The retirement impact changed or still contains work that this mode cannot resolve.",
            blocker_code=blockers[0],
        )


def _assert_retirement_postconditions(cluster: ProxmoxCluster, mode: str) -> None:
    cluster.refresh_from_db(
        fields=[
            "enabled",
            "retired_at",
            "retirement_mode",
            "retirement_reason",
            "discovered_ca_uuid",
            "discovered_ca_fingerprint",
            "retired_ca_uuid",
            "retired_ca_fingerprint",
            "lifecycle_generation",
        ]
    )
    config_or_current_exists = any(
        (
            ClusterCredential.objects.filter(cluster_id=cluster.pk).exists(),
            ClusterTransportTrust.objects.filter(cluster_id=cluster.pk).exists(),
            ProxmoxEndpoint.objects.filter(cluster_id=cluster.pk).exists(),
            CurrentGuestInventory.objects.filter(cluster_id=cluster.pk).exists(),
            CurrentGuestInventoryState.objects.filter(cluster_id=cluster.pk).exists(),
            ClusterMembershipState.objects.filter(cluster_id=cluster.pk).exists(),
            ClusterNodeState.objects.filter(cluster_id=cluster.pk).exists(),
            ClusterNodeInterface.objects.filter(cluster_id=cluster.pk).exists(),
            ClusterProjectionCoverage.objects.filter(cluster_id=cluster.pk).exists(),
            ClusterNodeEnrollment.objects.filter(cluster_id=cluster.pk).exists(),
            StorageCatalogState.objects.filter(cluster_id=cluster.pk).exists(),
            ClusterStorageNodeState.objects.filter(cluster_storage__cluster_id=cluster.pk).exists(),
            ClusterStorageVolumeCoverage.objects.filter(cluster_storage__cluster_id=cluster.pk).exists(),
            ClusterStorageVolumeObservation.objects.filter(cluster_storage__cluster_id=cluster.pk).exists(),
            ClusterStorageMount.objects.filter(cluster_storage__cluster_id=cluster.pk).exists(),
            ClusterTopologyHandoffStorageBinding.objects.filter(cluster_id=cluster.pk).exists(),
            ProxmoxStorageConsumer.objects.filter(cluster_id=cluster.pk).exists(),
            ScheduledAction.objects.filter(cluster_id=cluster.pk, deleted_at__isnull=True).exists(),
        )
    )
    definitions_not_tombstoned = ClusterStorage.objects.filter(
        cluster_id=cluster.pk,
        unmanaged_at__isnull=True,
    ).exists()
    _storage, _scheduled, _consoles, _audit, active_scan_count, blockers = _local_impact(cluster, mode)
    if any(
        (
            cluster.enabled,
            cluster.retired_at is None,
            cluster.retirement_mode != mode,
            bool(cluster.discovered_ca_uuid),
            bool(cluster.discovered_ca_fingerprint),
            config_or_current_exists,
            definitions_not_tombstoned,
            active_scan_count,
            bool(blockers),
        )
    ):
        raise ClusterRetirementPostconditionFailed(
            "Retirement could not prove its local cleanup postconditions; no changes were committed."
        )


def _invalidate_retired_cluster(cluster_pk: int) -> None:
    cluster = historical_clusters().filter(pk=cluster_pk).first()
    if cluster is not None:
        invalidate_cluster_cache(cluster)
    reset_trust_pools()


def _retire_cluster_atomic(
    cluster,
    *,
    confirmation: str,
    actor,
    reason: str,
    typed_cluster_key: str,
    permanent_unavailability_asserted: bool,
    replacement_ca_uuid: str,
) -> RetirementResult:
    with transaction.atomic():
        with scan_admission_lock():
            with cluster_lifecycle_lock(cluster):
                try:
                    locked = historical_clusters().select_for_update().get(pk=cluster.pk)
                except ProxmoxCluster.DoesNotExist as exc:
                    raise RetirementPreflightNotAllowed("The cluster connection no longer exists.") from exc

                confirmed = validate_retirement_preflight(
                    confirmation,
                    cluster=locked,
                    mode=_load_confirmation(confirmation)["mode"],
                )
                normalized_reason = _forced_confirmation(
                    cluster=locked,
                    mode=confirmed.mode,
                    typed_cluster_key=typed_cluster_key,
                    permanent_unavailability_asserted=permanent_unavailability_asserted,
                    reason=reason,
                )
                if confirmed.replacement_ca_uuid != str(replacement_ca_uuid or ""):
                    raise RetirementPreflightChanged(
                        "The verified replacement identity changed. Run hand-off review again."
                    )

                # Lock every active global scan row while holding the admission
                # lock. Scan admission takes the same locks in this order.
                active_scan_ids = list(
                    ScanRun.objects.select_for_update()
                    .filter(status__in=(ScanRun.Status.QUEUED, ScanRun.Status.RUNNING))
                    .order_by("pk")
                    .values_list("pk", flat=True)
                )
                if active_scan_ids:
                    raise ClusterRetirementActiveScan()
                _raise_for_local_blockers(locked, confirmed.mode)

                retired_at = timezone.now()
                endpoints, endpoint_count = _endpoint_snapshots(locked.pk)
                credential = ClusterCredential.objects.filter(cluster_id=locked.pk).only("token_id").first()
                trust = ClusterTransportTrust.objects.filter(cluster_id=locked.pk).only("mode").first()
                credential_token_id = credential.token_id if credential is not None else ""
                trust_mode = trust.mode if trust is not None else ""
                action = (
                    "cluster.retired"
                    if confirmed.mode == ProxmoxCluster.RetirementMode.VERIFIED
                    else "cluster.force_retired"
                )
                # Create the immutable success record before any destructive
                # finalizer. Its signal snapshots the top-level outbox payload
                # in this transaction; cleanup facts are filled below with a
                # queryset update so no second outbox revision is emitted.
                event = record_audit_event(
                    user=actor,
                    action=action,
                    object_type="cluster",
                    object_id=locked.key,
                    outcome="success",
                    cluster=locked,
                    cluster_key_snapshot=locked.key,
                    details={
                        "display_name": locked.display_name,
                        "cluster_key": locked.key,
                        "retirement_mode": confirmed.mode,
                        "retirement_reason": normalized_reason,
                        "identity_verification": confirmed.identity_verification,
                        "replacement_ca_uuid": confirmed.replacement_ca_uuid,
                        "endpoint_count": endpoint_count,
                        "endpoints": list(endpoints),
                        "credential_token_id": credential_token_id,
                        "trust_mode": trust_mode,
                    },
                )

                if confirmed.mode == ProxmoxCluster.RetirementMode.FORCED and locked.enabled:
                    historical_clusters().filter(pk=locked.pk).update(enabled=False, updated_at=retired_at)
                    locked.enabled = False

                scheduled = finalize_cluster_retirement_scheduled_actions(
                    locked,
                    mode=confirmed.mode,
                    retired_at=retired_at,
                )
                consoles = finalize_cluster_retirement_consoles(
                    locked,
                    mode=confirmed.mode,
                    retired_at=retired_at,
                )
                audit_operations = finalize_cluster_retirement_audit_operations(
                    locked,
                    mode=confirmed.mode,
                    retired_at=retired_at,
                )
                storage = finalize_cluster_retirement_storage(
                    locked,
                    mode=confirmed.mode,
                    expected_digest=confirmed.storage_impact_digest,
                    unmanaged_at=retired_at,
                )
                guest_inventory = retire_cluster_guest_inventory(locked)
                host_projection = retire_cluster_projection(locked)
                enrollments = retire_cluster_enrollments(locked)

                event.details = _retirement_audit_details(
                    locked,
                    confirmed=confirmed,
                    reason=normalized_reason,
                    retired_at=retired_at,
                    endpoints=endpoints,
                    endpoint_count=endpoint_count,
                    credential_token_id=credential_token_id,
                    trust_mode=trust_mode,
                    scheduled=scheduled,
                    consoles=consoles,
                    audit=audit_operations,
                    storage=storage,
                    guest_inventory=guest_inventory,
                    host_projection=host_projection,
                    enrollments=enrollments,
                )
                type(event).objects.filter(pk=event.pk).update(details=event.details)

                endpoints_deleted = ProxmoxEndpoint.objects.filter(cluster_id=locked.pk).delete()[0]
                credential_deleted = bool(ClusterCredential.objects.filter(cluster_id=locked.pk).delete()[0])
                trust_deleted = bool(ClusterTransportTrust.objects.filter(cluster_id=locked.pk).delete()[0])
                historical_clusters().filter(pk=locked.pk).update(
                    enabled=False,
                    retired_at=retired_at,
                    retired_by_id=getattr(actor, "pk", None),
                    retirement_mode=confirmed.mode,
                    retirement_reason=normalized_reason,
                    retired_ca_uuid=locked.discovered_ca_uuid,
                    retired_ca_fingerprint=locked.discovered_ca_fingerprint,
                    discovered_ca_uuid="",
                    discovered_ca_fingerprint="",
                    lifecycle_generation=F("lifecycle_generation") + 1,
                    updated_at=retired_at,
                )
                _assert_retirement_postconditions(locked, confirmed.mode)
                transaction.on_commit(lambda cluster_pk=locked.pk: _invalidate_retired_cluster(cluster_pk))

                return RetirementResult(
                    cluster_pk=locked.pk,
                    cluster_key=locked.key,
                    mode=confirmed.mode,
                    retired_at=retired_at,
                    audit_event_id=event.pk,
                    scheduled_actions=scheduled,
                    consoles=consoles,
                    audit_operations=audit_operations,
                    storage=storage,
                    guest_inventory=guest_inventory,
                    host_projection=host_projection,
                    enrollments=enrollments,
                    endpoints_deleted=endpoints_deleted,
                    credential_deleted=credential_deleted,
                    trust_deleted=trust_deleted,
                )


def _refusal_code(exc: Exception) -> str:
    if isinstance(exc, ClusterRetirementBlocked):
        return exc.blocker_code
    if isinstance(
        exc,
        (
            AuditRetirementBlocked,
            ConsoleRetirementBlocked,
            ScheduledActionRetirementBlocked,
            StorageRetirementConsumersBlock,
            StorageRetirementImpactChanged,
        ),
    ):
        return "participant_changed"
    return str(getattr(exc, "error_code", "") or ERROR_CODE_RETIREMENT_FAILED)


def _record_retirement_refusal(cluster, *, actor, mode: str, exc: Exception) -> None:
    current = historical_clusters().filter(pk=getattr(cluster, "pk", None)).first()
    if current is None:
        return
    record_audit_event(
        user=actor,
        action="cluster.retirement_refused",
        object_type="cluster",
        object_id=current.key,
        outcome="refused",
        cluster=current,
        cluster_key_snapshot=current.key,
        details={
            "cluster_key": current.key,
            "retirement_mode": str(mode or ""),
            "reason_code": _refusal_code(exc),
        },
    )


def retire_cluster(
    cluster,
    *,
    confirmation: str,
    actor,
    reason: str = "",
    typed_cluster_key: str = "",
    permanent_unavailability_asserted: bool = False,
    replacement_ca_uuid: str = "",
) -> RetirementResult:
    """Atomically retire one cluster without making any provider request.

    ``confirmation`` is the signed preflight evidence. Forced retirement also
    requires the exact permanent key, a bounded reason and an explicit assertion
    that the external site is permanently unavailable.
    """
    mode = ""
    try:
        payload = _load_confirmation(confirmation)
        mode = payload["mode"]
        return _retire_cluster_atomic(
            cluster,
            confirmation=confirmation,
            actor=actor,
            reason=reason,
            typed_cluster_key=typed_cluster_key,
            permanent_unavailability_asserted=permanent_unavailability_asserted,
            replacement_ca_uuid=replacement_ca_uuid,
        )
    except Exception as exc:
        owner_blocked = isinstance(
            exc,
            (
                AuditRetirementBlocked,
                ConsoleRetirementBlocked,
                ScheduledActionRetirementBlocked,
                StorageRetirementConsumersBlock,
                StorageRetirementImpactChanged,
            ),
        )
        if not isinstance(exc, (RetirementPreflightError, ClusterRetirementError)) and not owner_blocked:
            logger.exception(
                "Unexpected cluster retirement failure",
                extra={"cluster_pk": getattr(cluster, "pk", None)},
            )
        try:
            _record_retirement_refusal(cluster, actor=actor, mode=mode, exc=exc)
        except Exception:
            logger.exception(
                "Could not record cluster retirement refusal",
                extra={"cluster_pk": getattr(cluster, "pk", None)},
            )
        if isinstance(exc, (RetirementPreflightError, ClusterRetirementError)):
            raise
        if owner_blocked:
            raise ClusterRetirementBlocked(
                "The retirement impact changed or contains work that cannot be resolved.",
                blocker_code="participant_changed",
            ) from exc
        raise ClusterRetirementFailed(
            "Cluster retirement failed safely; no retirement changes were committed."
        ) from exc

"""Cluster-retirement preflight and short-lived signed evidence.

Provider verification deliberately lives outside the eventual retirement
transaction.  Verified retirement reads exactly one operator-selected endpoint;
forced retirement never constructs a provider client.  The signed evidence binds
the local state that the final transaction must re-check before it mutates
anything.

This module does not retire a cluster.  The coordinator that consumes the
evidence lands separately so this preflight remains independently reviewable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.core import signing
from django.utils import timezone

from core.models import (
    ClusterCredential,
    ClusterTransportTrust,
    ProxmoxCluster,
    ProxmoxEndpoint,
    ScanRun,
)
from core.services.audit_events import AuditRetirementPreflight, cluster_retirement_audit_preflight
from core.services.cluster_identity import ClusterIdentityError, discover_cluster_identity
from core.services.cluster_resolver import ClusterResolutionError, client_for_endpoint, enabled_endpoints
from core.services.cluster_scopes import historical_clusters
from core.services.console_sessions import ConsoleRetirementPreflight, cluster_retirement_console_preflight
from core.services.public_errors import PublicMessageError
from core.services.scheduled_actions import (
    ScheduledActionRetirementPreflight,
    cluster_retirement_scheduled_actions_preflight,
)
from core.services.storage_retirement import StorageRetirementPreflight, cluster_retirement_storage_preflight

RETIREMENT_PREFLIGHT_SALT = "pve-helper.cluster-retirement-preflight.v1"
RETIREMENT_PREFLIGHT_MAX_AGE_SECONDS = 10 * 60

ERROR_CODE_PREFLIGHT_INVALID = "cluster_retirement_preflight_invalid"
ERROR_CODE_PREFLIGHT_CHANGED = "cluster_retirement_preflight_changed"
ERROR_CODE_PREFLIGHT_NOT_ALLOWED = "cluster_retirement_preflight_not_allowed"
ERROR_CODE_PREFLIGHT_ENDPOINT = "cluster_retirement_preflight_endpoint"
ERROR_CODE_PREFLIGHT_UNREACHABLE = "cluster_retirement_preflight_unreachable"
ERROR_CODE_PREFLIGHT_IDENTITY_MISMATCH = "cluster_retirement_preflight_identity_mismatch"

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
) -> dict:
    return {
        "version": 1,
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
        or payload.get("version") != 1
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
        endpoint_id = endpoint.pk
    elif endpoint_id is not None or pinned_ca_uuid != "":
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
    )

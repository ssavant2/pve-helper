"""Verified cluster onboarding and connection administration.

Network verification happens against in-memory credentials and trust.  No cluster,
endpoint, trust row or credential is persisted until transport, effective
Administrator permissions and Proxmox CA identity have all been proven.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import (
    AuditEvent,
    ConsoleSession,
    ProxmoxCluster,
    ProxmoxEndpoint,
    ScanRun,
    ScheduledActionRun,
    cluster_key_validator,
)
from core.services.cluster_activation import activate_multicluster_identity, enable_cluster
from core.services.cluster_credentials import (
    ClusterCredentialError,
    ProxmoxCredential,
    resolve_credential,
    set_cluster_credential,
)
from core.services.cluster_identity import (
    ClusterIdentityError,
    ObservedClusterIdentity,
    discover_cluster_identity,
    reapprove_identity,
)
from core.services.cluster_state_identity import invalidate_cluster_cache
from core.services.cluster_trust import (
    TRUST_CA_PEM,
    TRUST_PUBLIC,
    InspectedCertificate,
    TransportTrustError,
    TrustProfile,
    approve_cluster_transport,
    inspect_endpoint_certificate,
    resolve_trust_profile,
)
from core.services.config import endpoint_name_from_url, normalize_endpoint_url
from core.services.proxmox import ProxmoxAPIError, ProxmoxClient
from core.services.public_errors import PROVIDER_FAILURE_MESSAGE, PublicMessageError, public_failure

_ENDPOINT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
_MINIMUM_PROXMOX_VERSION = (9, 2)
_PROXMOX_VERSION_RE = re.compile(r"^\s*(\d+)\.(\d+)(?:[.\-]|$)")


def _reason(exc: Exception, operation: str) -> str:
    """The public half of `exc`, for composing into an onboarding message.

    Sibling domain errors carry text written for this same operator and keep it;
    a `ProxmoxAPIError` does not, and is replaced. Either way the raise site owns
    what it says, which is what `PublicMessageError` promises.
    """
    return public_failure(exc, operation=f"cluster_onboarding.{operation}", fallback=PROVIDER_FAILURE_MESSAGE).message


class ClusterOnboardingError(PublicMessageError, RuntimeError):
    """A candidate is invalid, untrusted or does not satisfy the admin contract."""


@dataclass(frozen=True)
class ClusterCandidate:
    key: str
    display_name: str
    endpoint_url: str
    endpoint_name: str
    trust_mode: str
    token_id: str
    token_secret: str = field(repr=False)
    ca_pem: str = field(default="", repr=False)


@dataclass(frozen=True)
class VerifiedConnection:
    certificate: InspectedCertificate
    identity: ObservedClusterIdentity
    node_names: tuple[str, ...]
    version: str
    discovered_name: str
    administrator_privileges: tuple[str, ...]


def normalize_candidate(candidate: ClusterCandidate) -> ClusterCandidate:
    key = str(candidate.key or "").strip().lower()
    display_name = str(candidate.display_name or "").strip()
    endpoint_url = str(candidate.endpoint_url or "").strip().rstrip("/")
    endpoint_name = str(candidate.endpoint_name or "").strip() or endpoint_name_from_url(endpoint_url)
    trust_mode = str(candidate.trust_mode or "").strip()
    token_id = str(candidate.token_id or "").strip()
    token_secret = str(candidate.token_secret or "").strip()
    ca_pem = str(candidate.ca_pem or "").strip()

    try:
        cluster_key_validator(key)
    except ValidationError as exc:
        raise ClusterOnboardingError("; ".join(exc.messages)) from exc
    if not display_name:
        raise ClusterOnboardingError("Cluster name is required.")
    if len(display_name) > ProxmoxCluster._meta.get_field("display_name").max_length:
        raise ClusterOnboardingError("Cluster name is too long.")
    try:
        URLValidator(schemes=["https"])(endpoint_url)
    except ValidationError as exc:
        raise ClusterOnboardingError("Endpoint URL must be a valid HTTPS URL.") from exc
    parsed = urlparse(endpoint_url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ClusterOnboardingError("Endpoint URL may not contain credentials, a query or a fragment.")
    if parsed.path not in {"", "/"}:
        raise ClusterOnboardingError("Endpoint URL must identify the Proxmox API root, without a path.")
    if not _ENDPOINT_NAME_RE.fullmatch(endpoint_name):
        raise ClusterOnboardingError(
            "Endpoint name must start with a letter or digit and contain only letters, digits, '.', '_' or '-'."
        )
    if trust_mode not in {TRUST_PUBLIC, TRUST_CA_PEM}:
        raise ClusterOnboardingError("Choose public CA trust or provide an internal CA bundle.")
    if trust_mode == TRUST_CA_PEM and not ca_pem:
        raise ClusterOnboardingError("Internal CA trust requires a PEM CA bundle.")
    if not token_id or not token_secret:
        raise ClusterOnboardingError("Both API token ID and token secret are required.")
    if "@" not in token_id or "!" not in token_id:
        raise ClusterOnboardingError("Token ID must use the Proxmox 'user@realm!token-name' format.")

    return ClusterCandidate(
        key=key,
        display_name=display_name,
        endpoint_url=endpoint_url,
        endpoint_name=endpoint_name,
        trust_mode=trust_mode,
        token_id=token_id,
        token_secret=token_secret,
        ca_pem=ca_pem,
    )


def inspect_transport(endpoint_url: str) -> InspectedCertificate:
    endpoint_url = str(endpoint_url or "").strip().rstrip("/")
    try:
        URLValidator(schemes=["https"])(endpoint_url)
    except ValidationError as exc:
        raise ClusterOnboardingError("Endpoint URL must be a valid HTTPS URL.") from exc
    parsed = urlparse(endpoint_url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ClusterOnboardingError("Endpoint URL must be an HTTPS API root without credentials or extra path data.")
    try:
        return inspect_endpoint_certificate(endpoint_url)
    except TransportTrustError as exc:
        raise ClusterOnboardingError(_reason(exc, "inspect_endpoint")) from exc


def verify_new_cluster(
    candidate: ClusterCandidate,
    *,
    expected_certificate_fingerprint: str,
) -> tuple[ClusterCandidate, VerifiedConnection]:
    candidate = normalize_candidate(candidate)
    if ProxmoxCluster.objects.filter(key__iexact=candidate.key).exists():
        raise ClusterOnboardingError(f"Cluster key '{candidate.key}' is already registered.")
    _assert_endpoint_available(candidate.endpoint_url)
    verified = _verify_connection(
        endpoint_url=candidate.endpoint_url,
        endpoint_name=candidate.endpoint_name,
        trust_profile=_trust_profile(candidate.trust_mode, candidate.ca_pem),
        credential=ProxmoxCredential(candidate.token_id, candidate.token_secret),
        expected_certificate_fingerprint=expected_certificate_fingerprint,
    )
    owner = ProxmoxCluster.objects.filter(discovered_ca_uuid=verified.identity.ca_uuid).first()
    if owner is not None:
        raise ClusterOnboardingError(f"This physical Proxmox cluster is already registered as '{owner.key}'.")
    return candidate, verified


def verify_endpoint_for_cluster(
    cluster: ProxmoxCluster,
    *,
    endpoint_url: str,
    endpoint_name: str,
    expected_certificate_fingerprint: str,
) -> VerifiedConnection:
    endpoint_url = str(endpoint_url or "").strip().rstrip("/")
    endpoint_name = str(endpoint_name or "").strip() or endpoint_name_from_url(endpoint_url)
    _validate_endpoint_only(endpoint_url, endpoint_name)
    _assert_endpoint_available(endpoint_url)
    try:
        trust_profile = resolve_trust_profile(cluster)
        credential = resolve_credential(cluster)
    except (TransportTrustError, ClusterCredentialError) as exc:
        raise ClusterOnboardingError(_reason(exc, "verify_new_cluster")) from exc
    verified = _verify_connection(
        endpoint_url=endpoint_url,
        endpoint_name=endpoint_name,
        trust_profile=trust_profile,
        credential=credential,
        expected_certificate_fingerprint=expected_certificate_fingerprint,
    )
    if not cluster.discovered_ca_uuid:
        raise ClusterOnboardingError(
            f"Cluster '{cluster.key}' has no pinned CA identity; re-verify it before adding endpoints."
        )
    if verified.identity.ca_uuid != cluster.discovered_ca_uuid:
        raise ClusterOnboardingError(
            f"Endpoint belongs to Proxmox CA {verified.identity.ca_uuid}, but '{cluster.key}' is pinned "
            f"to {cluster.discovered_ca_uuid}. Nothing was saved."
        )
    return verified


def verify_registered_endpoint(
    cluster: ProxmoxCluster,
    endpoint: ProxmoxEndpoint,
) -> VerifiedConnection:
    """Re-verify one stored endpoint before it is returned to service.

    A disabled endpoint may have been re-pointed while it was out of rotation. It
    therefore has to prove transport, credential and pinned cluster identity again;
    merely flipping its database flag would make the original onboarding checks a
    one-time assertion.
    """
    if endpoint.cluster_id != cluster.pk:
        raise ClusterOnboardingError("The endpoint belongs to a different cluster record.")
    try:
        trust_profile = resolve_trust_profile(cluster)
        credential = resolve_credential(cluster)
    except (TransportTrustError, ClusterCredentialError) as exc:
        raise ClusterOnboardingError(_reason(exc, "verify_endpoint")) from exc
    verified = _verify_connection(
        endpoint_url=endpoint.url,
        endpoint_name=endpoint.name,
        trust_profile=trust_profile,
        credential=credential,
        expected_certificate_fingerprint="",
    )
    if not cluster.discovered_ca_uuid:
        raise ClusterOnboardingError(
            f"Cluster '{cluster.key}' has no pinned CA identity; re-verify it before enabling endpoints."
        )
    if verified.identity.ca_uuid != cluster.discovered_ca_uuid:
        raise ClusterOnboardingError(
            f"Endpoint now reports Proxmox CA {verified.identity.ca_uuid}, but '{cluster.key}' is pinned "
            f"to {cluster.discovered_ca_uuid}. The endpoint remains disabled."
        )
    return verified


def verify_replacement_credential(
    cluster: ProxmoxCluster,
    *,
    token_id: str,
    token_secret: str,
) -> VerifiedConnection:
    try:
        trust_profile = resolve_trust_profile(cluster)
    except TransportTrustError as exc:
        raise ClusterOnboardingError(_reason(exc, "verify_replacement_credential")) from exc
    return _verify_enabled_endpoints(
        cluster,
        trust_profile=trust_profile,
        credential=ProxmoxCredential(str(token_id or "").strip(), str(token_secret or "").strip()),
        expected_ca_uuid=cluster.discovered_ca_uuid,
        purpose="credential verification",
    )


def verify_cluster_connection(cluster: ProxmoxCluster) -> VerifiedConnection:
    try:
        credential = resolve_credential(cluster)
    except ClusterCredentialError as exc:
        raise ClusterOnboardingError(_reason(exc, "verify_cluster_connection")) from exc
    return verify_replacement_credential(
        cluster,
        token_id=credential.token_id,
        token_secret=credential.token_secret,
    )


@transaction.atomic
def persist_new_cluster(candidate: ClusterCandidate, verified: VerifiedConnection) -> ProxmoxCluster:
    """Persist one already-verified candidate as one atomic configuration change."""
    candidate = normalize_candidate(candidate)
    try:
        cluster = ProxmoxCluster.objects.create(
            key=candidate.key,
            display_name=candidate.display_name,
            enabled=False,
            discovered_name=verified.discovered_name,
            discovered_ca_uuid=verified.identity.ca_uuid,
            discovered_ca_fingerprint=verified.identity.ca_fingerprint,
            details={"proxmox_version": verified.version, "onboarded_nodes": list(verified.node_names)},
        )
        ProxmoxEndpoint.objects.create(
            cluster=cluster,
            name=candidate.endpoint_name,
            url=candidate.endpoint_url,
            enabled=True,
        )
        approve_cluster_transport(cluster, mode=candidate.trust_mode, ca_pem=candidate.ca_pem)
        set_cluster_credential(
            cluster,
            token_id=candidate.token_id,
            token_secret=candidate.token_secret,
        )
        activate_multicluster_identity()
        return enable_cluster(cluster)
    except IntegrityError as exc:
        raise ClusterOnboardingError(
            "The cluster key, endpoint URL or Proxmox CA identity was registered concurrently. Nothing was saved."
        ) from exc


@transaction.atomic
def persist_endpoint(
    cluster: ProxmoxCluster,
    *,
    endpoint_url: str,
    endpoint_name: str,
) -> ProxmoxEndpoint:
    _validate_endpoint_only(endpoint_url, endpoint_name)
    try:
        endpoint = ProxmoxEndpoint.objects.create(
            cluster=cluster,
            name=endpoint_name,
            url=endpoint_url,
            enabled=True,
        )
    except IntegrityError as exc:
        raise ClusterOnboardingError(
            "The endpoint URL or name was registered concurrently. Nothing was saved."
        ) from exc
    return endpoint


@transaction.atomic
def disable_cluster(cluster: ProxmoxCluster) -> ProxmoxCluster:
    locked = ProxmoxCluster.objects.select_for_update().get(pk=cluster.pk)
    blockers = active_cluster_operation_labels(locked)
    if blockers:
        raise ClusterOnboardingError("Disable was refused while provider work is active: " + "; ".join(blockers) + ".")
    if locked.enabled:
        locked.enabled = False
        locked.save(update_fields=["enabled", "updated_at"])
        invalidate_cluster_cache(locked)
    return locked


@transaction.atomic
def remove_stored_credential(cluster: ProxmoxCluster) -> None:
    locked = ProxmoxCluster.objects.select_for_update().get(pk=cluster.pk)
    if locked.enabled:
        raise ClusterOnboardingError("Disable the cluster before removing its stored credential.")
    blockers = active_cluster_operation_labels(locked)
    if blockers:
        raise ClusterOnboardingError(
            "Credential removal was refused while provider work is active: " + "; ".join(blockers) + "."
        )
    from core.models import ClusterCredential

    ClusterCredential.objects.filter(cluster=locked).delete()
    invalidate_cluster_cache(locked)


@transaction.atomic
def set_endpoint_enabled(endpoint: ProxmoxEndpoint, *, enabled: bool) -> ProxmoxEndpoint:
    locked = ProxmoxEndpoint.objects.select_for_update().select_related("cluster").get(pk=endpoint.pk)
    if not enabled and locked.enabled and locked.cluster.enabled:
        other_enabled = locked.cluster.endpoints.filter(enabled=True).exclude(pk=locked.pk).exists()
        if not other_enabled:
            raise ClusterOnboardingError(
                "An enabled cluster must retain at least one enabled endpoint. Disable the cluster first."
            )
    if locked.enabled != enabled:
        locked.enabled = enabled
        locked.save(update_fields=["enabled", "updated_at"])
        invalidate_cluster_cache(locked.cluster)
    return locked


def reapprove_cluster_identity(cluster: ProxmoxCluster) -> ObservedClusterIdentity:
    try:
        trust_profile = resolve_trust_profile(cluster)
        credential = resolve_credential(cluster)
    except (TransportTrustError, ClusterCredentialError) as exc:
        raise ClusterOnboardingError(_reason(exc, "reapprove_identity")) from exc
    verified = _verify_enabled_endpoints(
        cluster,
        trust_profile=trust_profile,
        credential=credential,
        expected_ca_uuid="",
        purpose="identity re-approval",
    )
    try:
        reapprove_identity(cluster, verified.identity)
    except ClusterIdentityError as exc:
        raise ClusterOnboardingError(_reason(exc, "reapprove_identity.store")) from exc
    return verified.identity


def _verify_enabled_endpoints(
    cluster: ProxmoxCluster,
    *,
    trust_profile: TrustProfile,
    credential: ProxmoxCredential,
    expected_ca_uuid: str,
    purpose: str,
) -> VerifiedConnection:
    """Verify all reachable transports without making redundancy a dependency.

    One healthy endpoint is enough when another is down, but every endpoint that
    does answer must report the same physical Proxmox CA. Returning after the first
    success would let a re-pointed second endpoint hide behind a healthy first one.
    """
    endpoints = list(cluster.endpoints.filter(enabled=True).order_by("name"))
    if not endpoints:
        raise ClusterOnboardingError(f"The cluster has no enabled endpoint for {purpose}.")

    verified_connections: list[VerifiedConnection] = []
    failures: list[str] = []
    for endpoint in endpoints:
        try:
            verified = _verify_connection(
                endpoint_url=endpoint.url,
                endpoint_name=endpoint.name,
                trust_profile=trust_profile,
                credential=credential,
                expected_certificate_fingerprint="",
            )
        except ClusterOnboardingError as exc:
            failures.append(f"{endpoint.name}: {_reason(exc, 'verify_enabled_endpoint')}")
            continue
        verified_connections.append(verified)

    if not verified_connections:
        detail = "; ".join(failures)
        raise ClusterOnboardingError(f"No enabled endpoint passed {purpose}: {detail}")

    observed_uuids = {verified.identity.ca_uuid for verified in verified_connections}
    if len(observed_uuids) != 1:
        raise ClusterOnboardingError(
            "Enabled endpoints report different Proxmox CA identities. Disable the incorrect "
            "endpoint before continuing."
        )
    observed_uuid = next(iter(observed_uuids))
    if expected_ca_uuid and observed_uuid != expected_ca_uuid:
        raise ClusterOnboardingError(
            f"Reachable endpoints report Proxmox CA {observed_uuid}, but cluster '{cluster.key}' "
            f"is pinned to {expected_ca_uuid}."
        )
    return verified_connections[0]


def active_cluster_operation_labels(cluster: ProxmoxCluster) -> list[str]:
    labels: list[str] = []
    active_audit = AuditEvent.objects.filter(
        cluster=cluster,
        outcome__in=("queued", "running"),
    ).count()
    if active_audit:
        labels.append(f"{active_audit} queued/running audit operation(s)")
    active_runs = ScheduledActionRun.objects.filter(
        scheduled_action__cluster=cluster,
        status__in=(
            ScheduledActionRun.Status.QUEUED,
            ScheduledActionRun.Status.PREFLIGHT,
            ScheduledActionRun.Status.SUBMITTED,
            ScheduledActionRun.Status.POLLING,
        ),
    ).count()
    if active_runs:
        labels.append(f"{active_runs} scheduled action run(s)")
    active_consoles = ConsoleSession.objects.filter(
        cluster=cluster,
        expires_at__gt=timezone.now(),
        status__in=(
            ConsoleSession.Status.PENDING,
            ConsoleSession.Status.CONNECTING,
            ConsoleSession.Status.CONNECTED,
        ),
    ).count()
    if active_consoles:
        labels.append(f"{active_consoles} console session(s)")
    # A scan is cluster-wide: it snapshots every enabled endpoint and reads this
    # cluster's nodes for the whole run. Disabling mid-scan lets the in-flight run
    # keep reading the just-disabled cluster, so any active scan blocks the change.
    active_scans = ScanRun.objects.filter(status__in=(ScanRun.Status.QUEUED, ScanRun.Status.RUNNING)).count()
    if active_scans:
        labels.append(f"{active_scans} running scan(s)")
    return labels


def _verify_connection(
    *,
    endpoint_url: str,
    endpoint_name: str,
    trust_profile: TrustProfile,
    credential: ProxmoxCredential,
    expected_certificate_fingerprint: str,
) -> VerifiedConnection:
    if not credential.token_id or not credential.token_secret:
        raise ClusterOnboardingError("Both API token ID and token secret are required.")
    certificate = inspect_transport(endpoint_url)
    expected = str(expected_certificate_fingerprint or "").strip().lower()
    if expected and certificate.sha256_fingerprint.lower() != expected:
        raise ClusterOnboardingError(
            "The endpoint certificate changed after inspection. Inspect it again before sending credentials."
        )
    try:
        trust_profile.build_verify()
        client = ProxmoxClient(endpoint_url, credential=credential, trust_profile=trust_profile)
        version_data = client.get("version")
        version = ""
        if isinstance(version_data, dict):
            version = str(version_data.get("version") or version_data.get("release") or "")
        _assert_supported_proxmox_version(version)
        nodes_data = client.get("nodes")
        permissions = client.get("access/permissions")
        administrator_role = client.get("access/roles/Administrator")
    except (ProxmoxAPIError, TransportTrustError) as exc:
        raise ClusterOnboardingError(
            f"Verified Proxmox connection failed: {_reason(exc, 'verify_connection')}"
        ) from exc

    node_names = (
        tuple(
            sorted(
                {str(row.get("node") or "").strip() for row in nodes_data if isinstance(row, dict) and row.get("node")}
            )
        )
        if isinstance(nodes_data, list)
        else ()
    )
    if not node_names:
        raise ClusterOnboardingError("The token could connect, but Proxmox returned no visible nodes.")
    _assert_administrator_permissions(permissions, administrator_role)
    identity = None
    identity_errors: list[str] = []
    for node_name in node_names:
        try:
            identity = discover_cluster_identity(client, node_name)
        except (ClusterIdentityError, ProxmoxAPIError) as exc:
            identity_errors.append(f"{node_name}: {_reason(exc, 'discover_identity')}")
            continue
        break
    if identity is None:
        raise ClusterOnboardingError(
            "Could not verify the Proxmox cluster identity through any visible node: " + "; ".join(identity_errors)
        )
    try:
        status = client.get("cluster/status")
    except ProxmoxAPIError as exc:
        raise ClusterOnboardingError(
            f"Could not read Proxmox cluster metadata: {_reason(exc, 'cluster_status')}"
        ) from exc

    discovered_name = ""
    if isinstance(status, list):
        discovered_name = next(
            (
                str(row.get("name") or "").strip()
                for row in status
                if isinstance(row, dict) and row.get("type") == "cluster"
            ),
            "",
        )
    required = tuple(sorted(key for key, value in administrator_role.items() if value))
    return VerifiedConnection(
        certificate=certificate,
        identity=identity,
        node_names=node_names,
        version=version,
        discovered_name=discovered_name,
        administrator_privileges=required,
    )


def _assert_supported_proxmox_version(version: str) -> None:
    match = _PROXMOX_VERSION_RE.match(str(version or ""))
    if match is None:
        raise ClusterOnboardingError("Could not verify the Proxmox VE version. Proxmox VE 9.2 or later is required.")
    observed = (int(match.group(1)), int(match.group(2)))
    if observed < _MINIMUM_PROXMOX_VERSION:
        raise ClusterOnboardingError(f"Proxmox VE 9.2 or later is required; the endpoint reports {version}.")


def _assert_administrator_permissions(permissions, administrator_role) -> None:
    if not isinstance(permissions, dict) or not isinstance(administrator_role, dict):
        raise ClusterOnboardingError("Proxmox returned an invalid permission response.")
    root = permissions.get("/")
    if not isinstance(root, dict):
        raise ClusterOnboardingError(
            "The token has no effective permissions at '/'. Assign Administrator on '/' to the API token."
        )
    required = {key for key, value in administrator_role.items() if value}
    effective = {key for key, value in root.items() if value}
    missing = sorted(required - effective)
    if missing:
        preview = ", ".join(missing[:8])
        suffix = f" and {len(missing) - 8} more" if len(missing) > 8 else ""
        raise ClusterOnboardingError(
            f"The API token does not have effective Administrator permissions on '/'. Missing: {preview}{suffix}."
        )


def _trust_profile(mode: str, ca_pem: str) -> TrustProfile:
    return TrustProfile(mode=TRUST_CA_PEM, ca_pem=ca_pem) if mode == TRUST_CA_PEM else TrustProfile(mode=TRUST_PUBLIC)


def _validate_endpoint_only(endpoint_url: str, endpoint_name: str) -> None:
    placeholder = ClusterCandidate(
        key="candidate",
        display_name="Candidate",
        endpoint_url=endpoint_url,
        endpoint_name=endpoint_name,
        trust_mode=TRUST_PUBLIC,
        token_id="candidate@pve!candidate",
        token_secret="candidate",
    )
    normalize_candidate(placeholder)


def _assert_endpoint_available(endpoint_url: str) -> None:
    normalized = normalize_endpoint_url(endpoint_url)
    owner = ProxmoxEndpoint.objects.filter(normalized_url=normalized).select_related("cluster").first()
    if owner is not None:
        raise ClusterOnboardingError(
            f"Endpoint {normalized} is already registered as '{owner.name}' on cluster '{owner.cluster.key}'."
        )

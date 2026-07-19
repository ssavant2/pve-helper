"""Explicit, cluster-scoped client selection.

Endpoints inside one Proxmox cluster are alternative transports to the same control
plane, not inventory shards. This replaces the "loop over every configured endpoint
and merge" convention: a cluster-wide read takes one authoritative answer from one
endpoint, and failover stays inside the selected cluster. Cross-cluster fallback by
VMID or node name is forbidden — it is precisely how a write reaches the wrong
cluster.

Credentials and TLS trust are resolved per cluster in `client_for_endpoint`: the
credential seals the request header, the trust profile selects the pooled TLS
client. Before their cutovers both fall back to the global compatibility settings.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.db.models import Case, IntegerField, Value, When

from core.models import ProxmoxCluster, ProxmoxEndpoint
from core.services.proxmox import ProxmoxAPIError, ProxmoxClient, ProxmoxTransportError

logger = logging.getLogger(__name__)


class ClusterResolutionError(RuntimeError):
    """No usable cluster could be selected for an operation."""


class ClusterDisabledError(ClusterResolutionError):
    """Acquisition was attempted against a cluster an operator has disabled."""


class ClusterQuarantinedError(ClusterResolutionError):
    """Acquisition was attempted against a cluster whose identity is in doubt."""


def _require_acquirable(cluster: ProxmoxCluster) -> None:
    """Refuse live acquisition against a disabled or quarantined cluster.

    Both block new provider writes, scheduled executions, consoles and refresh
    acquisition immediately, while last-known read models and history stay readable
    as visibly stale. Disabling is an operator choice; quarantine is automatic on a
    cluster-CA mismatch, where ingesting would merge a different cluster's guests.

    The check lives at each acquisition entry point, not inside the endpoint query,
    because verification flows legitimately talk to a cluster that is neither enabled
    nor cleared: onboarding, re-verifying identity, and the CA discovery that lifts a
    quarantine all build clients via `client_for_endpoint`, which stays ungated.
    """
    if not cluster.enabled:
        raise ClusterDisabledError(
            f"Cluster '{cluster.key}' is disabled. Re-enable it, which re-verifies its "
            "identity and trust, before reading from or writing to it."
        )
    if cluster.ingestion_quarantined:
        raise ClusterQuarantinedError(
            f"Cluster '{cluster.key}' is quarantined: {cluster.quarantine_reason} "
            "Re-approve its identity before reading from or writing to it."
        )


@dataclass(frozen=True)
class EndpointAttempt:
    endpoint_name: str
    ok: bool
    error: str = ""


@dataclass(frozen=True)
class ClusterReadResult:
    """One cluster-wide read: what was tried, what answered, and whether the
    cluster's logical coverage is complete.

    `complete` describes the *cluster*, not the endpoints. One authoritative
    response is complete coverage even if a redundant endpoint failed first: those
    endpoints are alternative transports, so a failed one degrades endpoint health
    without making the cluster's answer partial.
    """

    cluster_key: str
    value: Any
    answering_endpoint: str
    attempted: tuple[EndpointAttempt, ...]
    complete: bool
    # The client that answered. Follow-up node-local reads must go through the
    # endpoint that proved reachable, not through a fresh pick that might not be.
    client: ProxmoxClient | None = None

    @property
    def errors(self) -> list[str]:
        return [attempt.error for attempt in self.attempted if not attempt.ok]


def enabled_endpoints(cluster: ProxmoxCluster) -> list[ProxmoxEndpoint]:
    """Enabled endpoints of exactly this cluster, healthy transports first.

    A known-offline member must not impose its connect timeout on every request
    while another endpoint of the same control plane is healthy. New/unknown
    endpoints still get tried before known failures, with deterministic ordering
    inside each health tier.
    """
    health_rank = Case(
        When(last_health_status="ok", then=Value(0)),
        When(last_health_status="", then=Value(1)),
        default=Value(2),
        output_field=IntegerField(),
    )
    return list(
        ProxmoxEndpoint.objects.filter(cluster=cluster, enabled=True)
        .annotate(_health_rank=health_rank)
        .order_by("_health_rank", "name")
    )


def client_for_endpoint(endpoint: ProxmoxEndpoint) -> ProxmoxClient:
    """Build a client for one endpoint, carrying its cluster's own identity.

    The credential is resolved here, per client, rather than cached on one: a
    rotation then takes effect on the next call with no pool to invalidate. The
    secret is unsealed only to build the request's header.

    Phase 1d adds the cluster's transport-trust profile alongside this.
    """
    from core.services.cluster_credentials import resolve_credential
    from core.services.cluster_trust import resolve_trust_profile

    if endpoint.cluster_id is None:
        raise ClusterResolutionError(f"Endpoint '{endpoint.name}' has no cluster identity and cannot be used.")
    credential = resolve_credential(endpoint.cluster)
    trust_profile = resolve_trust_profile(endpoint.cluster)
    return ProxmoxClient(endpoint.url, credential=credential, trust_profile=trust_profile)


def cluster_clients(cluster: ProxmoxCluster) -> list[ProxmoxClient]:
    """Clients for this cluster's enabled endpoints, and no other cluster's."""
    _require_acquirable(cluster)
    return [client_for_endpoint(endpoint) for endpoint in enabled_endpoints(cluster)]


def cluster_wide_read(
    cluster: ProxmoxCluster,
    *,
    operation: str,
    call: Callable[[ProxmoxClient], Any],
) -> ClusterReadResult:
    """Read a cluster-wide response from one endpoint, failing over within the cluster.

    Deduplication is by construction: `cluster/resources`, the tag registry and guest
    inventory are cluster-wide responses regardless of which member answers, so
    taking the first authoritative answer stores each object once instead of once
    per endpoint.

    Only ProxmoxAPIError is caught. An unexpected exception is a bug and stays
    visible to tests and monitoring rather than being reported as degradation.
    """
    _require_acquirable(cluster)
    attempts: list[EndpointAttempt] = []

    for endpoint in enabled_endpoints(cluster):
        client = client_for_endpoint(endpoint)
        try:
            value = call(client)
        except ProxmoxAPIError as exc:
            logger.warning(
                "Proxmox read failed: cluster=%s endpoint=%s operation=%s error=%s",
                cluster.key,
                endpoint.name,
                operation,
                exc,
            )
            attempts.append(EndpointAttempt(endpoint.name, False, str(exc)))
            continue

        attempts.append(EndpointAttempt(endpoint.name, True))
        return ClusterReadResult(
            cluster_key=cluster.key,
            value=value,
            answering_endpoint=endpoint.name,
            attempted=tuple(attempts),
            complete=True,
            client=client,
        )

    return ClusterReadResult(
        cluster_key=cluster.key,
        value=None,
        answering_endpoint="",
        attempted=tuple(attempts),
        complete=False,
    )


def pin_cluster_write_client(cluster: ProxmoxCluster) -> tuple[ProxmoxEndpoint, ProxmoxClient]:
    """Pin exactly one endpoint for a write, before preflight."""
    _require_acquirable(cluster)
    endpoints = enabled_endpoints(cluster)
    if not endpoints:
        raise ClusterResolutionError(f"Cluster '{cluster.key}' has no enabled endpoint to write through.")
    endpoint = endpoints[0]
    return endpoint, client_for_endpoint(endpoint)


@dataclass(frozen=True)
class ClusterWriteResult:
    cluster_key: str
    value: Any
    answering_endpoint: str
    client: ProxmoxClient | None
    error: str
    attempted: tuple[EndpointAttempt, ...]

    @property
    def ok(self) -> bool:
        return not self.error


def cluster_write(
    cluster: ProxmoxCluster,
    *,
    operation: str,
    call: Callable[[ProxmoxClient], Any],
    error_message: Callable[[ProxmoxAPIError], str],
) -> ClusterWriteResult:
    """Perform one mutation inside exactly this cluster, never replaying an
    ambiguous attempt.

    The old convention looped over every configured endpoint and retried the
    mutation on the next one after any failure. That is unsafe twice over: with two
    clusters it can mutate a same-VMID guest in the wrong one, and even within a
    cluster it replays a write whose outcome is unknown, so a timed-out shutdown or
    snapshot could be applied twice.

    Advancing to the next endpoint is allowed only when the failure *proves* the
    request never left — a refused connection. A request that may have been applied
    stops here and is reported; whether it can be retried is an operation-specific
    idempotency/postcondition decision, not something transport may assume. An HTTP
    error is the server's answer and is never re-asked elsewhere.
    """
    _require_acquirable(cluster)
    attempts: list[EndpointAttempt] = []
    endpoints = enabled_endpoints(cluster)
    if not endpoints:
        raise ClusterResolutionError(f"Cluster '{cluster.key}' has no enabled endpoint to write through.")

    error = ""
    for endpoint in endpoints:
        client = client_for_endpoint(endpoint)
        try:
            value = call(client)
        except ProxmoxAPIError as exc:
            unsent = isinstance(exc, ProxmoxTransportError) and not exc.request_sent
            logger.warning(
                "Proxmox write failed: cluster=%s endpoint=%s operation=%s unsent=%s error=%s",
                cluster.key,
                endpoint.name,
                operation,
                unsent,
                exc,
            )
            attempts.append(EndpointAttempt(endpoint.name, False, str(exc)))
            error = error_message(exc)
            if unsent:
                continue
            break

        attempts.append(EndpointAttempt(endpoint.name, True))
        return ClusterWriteResult(
            cluster_key=cluster.key,
            value=value,
            answering_endpoint=endpoint.name,
            client=client,
            error="",
            attempted=tuple(attempts),
        )

    return ClusterWriteResult(
        cluster_key=cluster.key,
        value=None,
        answering_endpoint="",
        client=None,
        error=error or "No Proxmox endpoint in this cluster could reach the guest.",
        attempted=tuple(attempts),
    )

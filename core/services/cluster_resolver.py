"""Explicit, cluster-scoped client selection.

Endpoints inside one Proxmox cluster are alternative transports to the same control
plane, not inventory shards. This replaces the "loop over every configured endpoint
and merge" convention: a cluster-wide read takes one authoritative answer from one
endpoint, and failover stays inside the selected cluster. Cross-cluster fallback by
VMID or node name is forbidden — it is precisely how a write reaches the wrong
cluster.

Credentials and TLS trust still come from the global compatibility settings via
ProxmoxClient, because only one cluster can be enabled before activation. Phase 1c
and 1d move those onto the cluster; this module is where they will be injected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from core.models import ProxmoxCluster, ProxmoxEndpoint, RuntimeConfigurationState
from core.services.proxmox import ProxmoxAPIError, ProxmoxClient, ProxmoxTransportError


logger = logging.getLogger(__name__)


class ClusterResolutionError(RuntimeError):
    """No usable cluster could be selected for an operation."""


class LegacyClusterScopeError(ClusterResolutionError):
    """A legacy caller could not be given an unambiguous cluster."""


class ClusterDisabledError(ClusterResolutionError):
    """Acquisition was attempted against a cluster an operator has disabled."""


def _require_enabled(cluster: ProxmoxCluster) -> None:
    """Refuse live acquisition against a disabled cluster.

    Disabling blocks new provider writes, scheduled executions, consoles and
    refresh acquisition immediately, while last-known read models and history stay
    readable as visibly stale. The check lives at each acquisition entry point
    rather than inside the endpoint query, because verification flows — onboarding
    a cluster, or re-verifying identity before re-enabling one — legitimately talk
    to a cluster that is not enabled yet.

    Today the legacy adapter only ever yields an enabled cluster, so nothing
    reaches this. Phase 3 hands callers an explicit cluster, which is exactly when
    a disabled one could arrive here.
    """
    if not cluster.enabled:
        raise ClusterDisabledError(
            f"Cluster '{cluster.key}' is disabled. Re-enable it, which re-verifies its "
            "identity and trust, before reading from or writing to it."
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
    """Enabled endpoints of exactly this cluster, in stable order."""
    return list(ProxmoxEndpoint.objects.filter(cluster=cluster, enabled=True).order_by("name"))


def client_for_endpoint(endpoint: ProxmoxEndpoint) -> ProxmoxClient:
    """Build a client for one endpoint.

    Phase 1c/1d replace the global credential and trust settings baked into
    ProxmoxClient with the cluster's own; this is the single seam where that lands.
    """
    return ProxmoxClient(endpoint.url)


def cluster_clients(cluster: ProxmoxCluster) -> list[ProxmoxClient]:
    """Clients for this cluster's enabled endpoints, and no other cluster's."""
    _require_enabled(cluster)
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
    _require_enabled(cluster)
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
    _require_enabled(cluster)
    endpoints = enabled_endpoints(cluster)
    if not endpoints:
        raise ClusterResolutionError(
            f"Cluster '{cluster.key}' has no enabled endpoint to write through."
        )
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
    _require_enabled(cluster)
    attempts: list[EndpointAttempt] = []
    endpoints = enabled_endpoints(cluster)
    if not endpoints:
        raise ClusterResolutionError(
            f"Cluster '{cluster.key}' has no enabled endpoint to write through."
        )

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


def require_sole_enabled_cluster_for_legacy_caller() -> ProxmoxCluster:
    """Resolve the cluster for a caller that does not yet carry explicit scope.

    Deliberately awkward to name and to call: it exists only to migrate Phase 1b
    callers that have no GuestRef, NodeRef or path scope yet, and it is removed in
    Phase 4 before activation. Entry points resolve at their boundary and pass the
    cluster explicitly down the call chain; the resolver itself never calls this.

    It fails closed rather than guessing: at identity contract version 1 there may
    be several clusters and no caller may silently get one.
    """
    state = RuntimeConfigurationState.objects.filter(pk=RuntimeConfigurationState.SINGLETON_PK).first()
    version = state.identity_contract_version if state is not None else 0
    if version >= 1:
        raise LegacyClusterScopeError(
            "A legacy caller requested an implicit cluster at identity contract version "
            f"{version}. Every read, write, URL and payload must carry explicit cluster "
            "scope once multi-cluster identity is active."
        )

    clusters = list(ProxmoxCluster.objects.filter(enabled=True).order_by("key")[:2])
    if not clusters:
        raise LegacyClusterScopeError(
            "No enabled Proxmox cluster is configured. Configure one before using this feature."
        )
    if len(clusters) > 1:
        raise LegacyClusterScopeError(
            "More than one cluster is enabled, so an implicit cluster would be a guess. "
            "This caller must pass an explicit cluster."
        )
    return clusters[0]

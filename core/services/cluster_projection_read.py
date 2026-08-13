"""Presentation-free reads of the persisted Hosts & Clusters projection.

This is the consumer boundary for membership and node runtime. It deliberately
imports no provider adapter, refresher, cache or task module: a passive read can
describe missing or stale state, but it cannot repair it or contact Proxmox.

Guest placement remains owned by ``CurrentGuestInventory`` and storage by the
storage catalog. Surfaces may compose those owners with this result; they must not
turn this service into a second guest or storage authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from django.conf import settings
from django.utils import timezone

from core.models import ClusterMembershipState, ClusterNodeState, ClusterProjectionCoverage
from core.services.cluster_scopes import managed_clusters
from core.services.refs import NodeRef


class MembershipReadStatus(StrEnum):
    MISSING = "missing"
    CURRENT = "current"
    INCOMPLETE = "incomplete"
    STALE = "stale"


class NodeRuntimeReadStatus(StrEnum):
    UNKNOWN = "unknown"
    CURRENT = "current"
    STALE = "stale"
    OFFLINE_SKIPPED = "offline_skipped"
    DEPARTED = "departed"
    FAILED = "failed"


@dataclass(frozen=True)
class ProjectionCoverageRead:
    generation: int
    based_on_generation: int | None
    complete: bool
    attempted_at: datetime | None
    observed_at: datetime | None
    error_code: str
    fresh: bool
    current: bool


@dataclass(frozen=True)
class NodeRuntimeMetrics:
    cpu_usage: float | None
    cpu_wait: float | None
    cpu_model: str
    cpu_sockets: int | None
    cpu_cores: int | None
    memory_total_bytes: int | None
    memory_used_bytes: int | None
    swap_total_bytes: int | None
    swap_used_bytes: int | None
    rootfs_total_bytes: int | None
    rootfs_used_bytes: int | None
    load_average_1m: float | None
    load_average_5m: float | None
    load_average_15m: float | None
    uptime_seconds: int | None
    pve_version: str
    kernel_version: str
    current_kernel_release: str
    boot_mode: str
    secure_boot_enabled: bool | None


@dataclass(frozen=True)
class ClusterNodeProjectionRead:
    node_ref: str
    node_name: str
    nodeid: int | None
    present: bool
    online: bool
    reported_ring_address: str
    first_discovered_at: datetime | None
    last_discovered_at: datetime | None
    membership_generation: int
    runtime_generation: int
    runtime_status: NodeRuntimeReadStatus
    runtime_coverage: ProjectionCoverageRead | None
    runtime: NodeRuntimeMetrics

    @property
    def runtime_current(self) -> bool:
        return self.runtime_status is NodeRuntimeReadStatus.CURRENT


@dataclass(frozen=True)
class ClusterProjectionRead:
    cluster_key: str
    display_name: str
    enabled: bool
    ingestion_quarantined: bool
    topology_role: str
    topology_role_readable: bool
    transition_pending: bool
    pending_topology_role: str
    pending_role_is_readable: bool
    membership_generation: int
    member_count: int
    quorate: bool
    observed_from: str
    membership_status: MembershipReadStatus
    membership_coverage: ProjectionCoverageRead | None
    nodes: tuple[ClusterNodeProjectionRead, ...]

    @property
    def membership_current(self) -> bool:
        return self.membership_status is MembershipReadStatus.CURRENT


class ClusterProjectionNotFound(LookupError):
    """The requested key is not in the managed (non-retired) cluster scope."""


def _fresh(observed_at: datetime | None, *, now: datetime, max_age: timedelta) -> bool:
    if observed_at is None:
        return False
    age = now - observed_at
    return timedelta(0) <= age <= max_age


def _coverage_read(
    coverage: ClusterProjectionCoverage | None,
    *,
    fresh: bool,
    current: bool,
) -> ProjectionCoverageRead | None:
    if coverage is None:
        return None
    return ProjectionCoverageRead(
        generation=coverage.generation,
        based_on_generation=coverage.based_on_generation,
        complete=coverage.complete,
        attempted_at=coverage.attempted_at,
        observed_at=coverage.observed_at,
        error_code=coverage.error_code,
        fresh=fresh,
        current=current,
    )


def _membership_read(
    state: ClusterMembershipState | None,
    coverage: ClusterProjectionCoverage | None,
    *,
    now: datetime,
    max_age: timedelta,
) -> tuple[MembershipReadStatus, ProjectionCoverageRead | None]:
    fresh = _fresh(coverage.observed_at if coverage else None, now=now, max_age=max_age)
    generation_aligned = bool(
        state and state.membership_generation > 0 and coverage and coverage.generation == state.membership_generation
    )
    current = bool(coverage and coverage.complete and generation_aligned and fresh)
    if state is None or coverage is None:
        status = MembershipReadStatus.MISSING
    elif not coverage.complete:
        status = MembershipReadStatus.INCOMPLETE
    elif current:
        status = MembershipReadStatus.CURRENT
    else:
        status = MembershipReadStatus.STALE
    return status, _coverage_read(coverage, fresh=fresh, current=current)


def _runtime_status(
    row: ClusterNodeState,
    coverage: ClusterProjectionCoverage | None,
    *,
    membership_generation: int | None,
    now: datetime,
    max_age: timedelta,
) -> tuple[NodeRuntimeReadStatus, ProjectionCoverageRead | None]:
    fresh = _fresh(coverage.observed_at if coverage else None, now=now, max_age=max_age)
    bound_to_membership = bool(coverage and coverage.based_on_generation == membership_generation)
    membership_aligned = row.membership_generation == membership_generation

    if (
        coverage
        and coverage.error_code == "node_absent_from_membership"
        and not coverage.complete
        and bound_to_membership
        and membership_aligned
        and not row.present
    ):
        status = NodeRuntimeReadStatus.DEPARTED
    elif (
        coverage
        and coverage.error_code == "node_offline_by_membership"
        and not coverage.complete
        and bound_to_membership
        and membership_aligned
        and row.present
        and not row.online
    ):
        status = NodeRuntimeReadStatus.OFFLINE_SKIPPED
    elif coverage is None:
        status = NodeRuntimeReadStatus.UNKNOWN
    elif not coverage.complete and bound_to_membership:
        status = NodeRuntimeReadStatus.FAILED
    else:
        current = bool(
            row.present
            and row.online
            and membership_aligned
            and coverage.complete
            and coverage.error_code == ""
            and bound_to_membership
            and coverage.generation == row.runtime_generation
            and fresh
        )
        status = NodeRuntimeReadStatus.CURRENT if current else NodeRuntimeReadStatus.STALE

    current = status is NodeRuntimeReadStatus.CURRENT
    return status, _coverage_read(coverage, fresh=fresh, current=current)


def _runtime_metrics(row: ClusterNodeState) -> NodeRuntimeMetrics:
    return NodeRuntimeMetrics(
        cpu_usage=row.cpu_usage,
        cpu_wait=row.cpu_wait,
        cpu_model=row.cpu_model,
        cpu_sockets=row.cpu_sockets,
        cpu_cores=row.cpu_cores,
        memory_total_bytes=row.memory_total_bytes,
        memory_used_bytes=row.memory_used_bytes,
        swap_total_bytes=row.swap_total_bytes,
        swap_used_bytes=row.swap_used_bytes,
        rootfs_total_bytes=row.rootfs_total_bytes,
        rootfs_used_bytes=row.rootfs_used_bytes,
        load_average_1m=row.load_average_1m,
        load_average_5m=row.load_average_5m,
        load_average_15m=row.load_average_15m,
        uptime_seconds=row.uptime_seconds,
        pve_version=row.pve_version,
        kernel_version=row.kernel_version,
        current_kernel_release=row.current_kernel_release,
        boot_mode=row.boot_mode,
        secure_boot_enabled=row.secure_boot_enabled,
    )


def read_cluster_projection(cluster_key: str, *, now: datetime | None = None) -> ClusterProjectionRead:
    """Read one managed cluster in four database queries and zero provider calls."""

    at = now or timezone.now()
    if timezone.is_naive(at):
        raise ValueError("Projection read time must be timezone-aware.")
    max_age = timedelta(minutes=max(1, settings.HOST_PROJECTION_REFRESH_INTERVAL_MINUTES) * 2)

    try:
        cluster = managed_clusters().get(key=cluster_key)  # query 1: exact managed identity
    except managed_clusters().model.DoesNotExist as exc:
        raise ClusterProjectionNotFound(cluster_key) from exc

    membership_state = ClusterMembershipState.objects.filter(cluster_id=cluster.pk).first()  # query 2
    node_rows = list(ClusterNodeState.objects.filter(cluster_id=cluster.pk).order_by("node_name"))  # query 3
    # Query 4, filtered by domain rather than loading every coverage row. 5a4B-i adds
    # one `node_network` row per node per cluster; unfiltered, they would be fetched
    # into this read, inflate its pinned row budget, and sit one predicate away from
    # entering a status derivation that knows nothing about them.
    coverages = list(
        ClusterProjectionCoverage.objects.filter(
            cluster_id=cluster.pk,
            domain__in=(
                ClusterProjectionCoverage.DOMAIN_MEMBERSHIP,
                ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME,
            ),
        )
    )

    membership_coverage = next(
        (item for item in coverages if item.domain == ClusterProjectionCoverage.DOMAIN_MEMBERSHIP),
        None,
    )
    runtime_coverages = {
        item.node_name: item
        for item in coverages
        if item.domain == ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME and item.node_name is not None
    }
    membership_status, membership_read = _membership_read(
        membership_state,
        membership_coverage,
        now=at,
        max_age=max_age,
    )
    published_membership_generation = (
        membership_state.membership_generation
        if membership_state and membership_state.membership_generation > 0
        else None
    )
    membership_generation = published_membership_generation or 0

    nodes = []
    for row in node_rows:
        runtime_status, runtime_coverage = _runtime_status(
            row,
            runtime_coverages.get(row.node_name),
            membership_generation=published_membership_generation,
            now=at,
            max_age=max_age,
        )
        nodes.append(
            ClusterNodeProjectionRead(
                node_ref=NodeRef(cluster.key, row.node_name).serialize(),
                node_name=row.node_name,
                nodeid=row.nodeid,
                present=row.present,
                online=row.online,
                reported_ring_address=row.reported_ring_address,
                first_discovered_at=row.first_discovered_at,
                last_discovered_at=row.last_discovered_at,
                membership_generation=row.membership_generation,
                runtime_generation=row.runtime_generation,
                runtime_status=runtime_status,
                runtime_coverage=runtime_coverage,
                runtime=_runtime_metrics(row),
            )
        )

    return ClusterProjectionRead(
        cluster_key=cluster.key,
        display_name=cluster.display_name,
        enabled=cluster.enabled,
        ingestion_quarantined=cluster.ingestion_quarantined,
        topology_role=membership_state.topology_role if membership_state else "unknown",
        topology_role_readable=membership_state.role_is_readable if membership_state else True,
        transition_pending=membership_state.transition_pending if membership_state else False,
        pending_topology_role=membership_state.pending_topology_role if membership_state else "unknown",
        pending_role_is_readable=membership_state.pending_role_is_readable if membership_state else True,
        membership_generation=membership_generation,
        member_count=membership_state.member_count if membership_state else 0,
        quorate=membership_state.quorate if membership_state else False,
        observed_from=membership_state.observed_from if membership_state else "",
        membership_status=membership_status,
        membership_coverage=membership_read,
        nodes=tuple(nodes),
    )

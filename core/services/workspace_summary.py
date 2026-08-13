"""Composition for the two Summary bodies (5a2C/5a2D).

Presentation-free. The views render what this returns; nothing here formats, and
nothing here reads a provider.

**The rule this module exists to hold: a cluster aggregate never speaks for a node
it could not read.** Capacity and usage are sums over nodes whose *own* runtime is
current, and the result carries how many nodes contributed out of how many exist.
A three-node cluster with one failed node reports two-node totals and says so — it
does not scale them up, hide the gap, or borrow the cluster's membership freshness
to call the third node's stale numbers current.

The same rule read from the other end is why Node Summary takes its freshness from
its own ``runtime_status`` and never from the cluster: one failed sibling must stay
isolated.

Field selection is bounded by what an accepted projection owns. HA manager state,
update rollups, subscription and EVC baselines are named in the tab mapping but
belong to 5d1, 5b1 and 5a4 — they are absent here rather than guessed at, because a
placeholder that invents a field is the live-fallback mistake in another shape.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Count, Q

from core.services.cluster_projection_read import (
    ClusterNodeProjectionRead,
    ClusterProjectionRead,
)
from core.services.current_guest_inventory import published_guest_queryset

#: The provider's running literal, matched once here rather than at each caller.
_RUNNING = Q(status="running")


@dataclass(frozen=True)
class CapacityRoll:
    """Summed runtime capacity, with the coverage that produced it.

    ``contributing`` and ``total`` are part of the value, not decoration. A caller
    that renders the bytes without the ratio is reporting a partial sum as a whole,
    which is the failure this dataclass shape makes awkward to commit.
    """

    contributing: int
    total: int
    cpu_cores: int
    memory_total_bytes: int
    memory_used_bytes: int
    rootfs_total_bytes: int
    rootfs_used_bytes: int

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.contributing == self.total

    @property
    def missing(self) -> int:
        return max(0, self.total - self.contributing)


@dataclass(frozen=True)
class NodePlacement:
    """Guests currently placed on one node, from the published projection."""

    total: int
    running: int


@dataclass(frozen=True)
class ClusterNodeRow:
    """One node with its placement, paired here so the template does no lookup.

    A dict indexed from the template needs a custom filter and fails silently when
    the key is missing; pairing in the composer makes an absent placement impossible
    rather than invisible.
    """

    node: ClusterNodeProjectionRead
    placement: NodePlacement


@dataclass(frozen=True)
class ClusterSummaryRead:
    projection: ClusterProjectionRead
    rows: tuple[ClusterNodeRow, ...]
    node_count: int
    online_nodes: int
    capacity: CapacityRoll
    guests_total: int
    guests_running: int
    #: Published guests whose node is not one this page lists -- a node that left
    #: membership while its guest rows still exist, or one hidden between the
    #: reconcile and the read. Counted rather than dropped: silently losing them
    #: would make the total disagree with the rows for no stated reason.
    guests_off_listed_nodes: int


@dataclass(frozen=True)
class NodeSummaryRead:
    node: ClusterNodeProjectionRead
    placement: NodePlacement


def _sum_or_zero(value: int | None) -> int:
    return int(value or 0)


def guest_placement(cluster, *, nodes: tuple[str, ...] | None = None) -> dict[str, NodePlacement]:
    """Guests per node for one cluster, in **one** aggregate query.

    Counted from the published projection, so a hidden node's guests are not in the
    numbers an operator reads — the same boundary the tree and the target lists use.

    ``nodes`` restricts the result to the keys the caller will render; a node with no
    guests is still present, as an explicit zero rather than a missing key, because
    "no guests" and "not counted" are different answers.
    """

    rows = (
        published_guest_queryset()
        .filter(cluster=cluster)
        .values("node")
        .order_by()
        .annotate(total=Count("pk"), running=Count("pk", filter=_RUNNING))
    )
    placement = {row["node"]: NodePlacement(total=row["total"], running=row["running"]) for row in rows}
    for node_name in nodes or ():
        placement.setdefault(node_name, NodePlacement(total=0, running=0))
    return placement


def _capacity(nodes: tuple[ClusterNodeProjectionRead, ...]) -> CapacityRoll:
    contributing = [node for node in nodes if node.runtime_current]
    return CapacityRoll(
        contributing=len(contributing),
        total=len(nodes),
        cpu_cores=sum(_sum_or_zero(node.runtime.cpu_cores) for node in contributing),
        memory_total_bytes=sum(_sum_or_zero(node.runtime.memory_total_bytes) for node in contributing),
        memory_used_bytes=sum(_sum_or_zero(node.runtime.memory_used_bytes) for node in contributing),
        rootfs_total_bytes=sum(_sum_or_zero(node.runtime.rootfs_total_bytes) for node in contributing),
        rootfs_used_bytes=sum(_sum_or_zero(node.runtime.rootfs_used_bytes) for node in contributing),
    )


def cluster_summary(cluster, projection: ClusterProjectionRead, nodes) -> ClusterSummaryRead:
    """Compose Cluster Summary from the projection the view already read.

    ``nodes`` is the published subset the view resolved for the tab strip, so the
    aggregate covers exactly the nodes the page lists — no second filter, and no
    chance of a total that includes a node the page does not show.
    """

    nodes = tuple(nodes)
    listed = tuple(node.node_name for node in nodes)
    placement = guest_placement(cluster, nodes=listed)
    on_listed = {name: placement[name] for name in listed}
    return ClusterSummaryRead(
        projection=projection,
        rows=tuple(ClusterNodeRow(node=node, placement=on_listed[node.node_name]) for node in nodes),
        node_count=len(nodes),
        online_nodes=sum(1 for node in nodes if node.online),
        capacity=_capacity(nodes),
        guests_total=sum(entry.total for entry in on_listed.values()),
        guests_running=sum(entry.running for entry in on_listed.values()),
        guests_off_listed_nodes=sum(entry.total for name, entry in placement.items() if name not in on_listed),
    )


def node_summary(cluster, node: ClusterNodeProjectionRead) -> NodeSummaryRead:
    """Compose Node Summary for one exact NodeRef.

    Takes no cluster freshness argument on purpose: there is nothing for this view
    to borrow it from.
    """

    placement = guest_placement(cluster, nodes=(node.node_name,))
    return NodeSummaryRead(node=node, placement=placement[node.node_name])

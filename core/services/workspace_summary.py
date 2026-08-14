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

**A meter has three states, and "unknown" is not the low end of the scale** (5a2G).
A node that did not report its memory and a node using none of it are the same
number on a bar and opposite facts, so :class:`Meter` answers ``known`` before it
answers ``percent``, and ``percent`` is ``None`` rather than ``0.0`` when the node
said nothing. The same rule one level up is why a Cluster Summary row for a node
whose runtime is not current carries no meters at all: that node is already
excluded from the totals beside it, and drawing its last-known bars would make the
breakdown disagree with the sum it is supposed to explain.

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
class Meter:
    """One ``used`` of ``total``, or the fact that nobody said.

    Presentation-free on purpose: the unit is the caller's business, because bytes
    want ``filesizeformat`` and cores want a percentage, and a formatter here would
    have to guess which. What is *not* the caller's business is the unknown case —
    ``percent`` refuses to answer rather than returning a zero a template would
    happily draw as an empty bar.

    ``percent`` is not clamped. Used never exceeds total for the fields this phase
    meters, and if that ever stops being true the honest response is a number over
    100 rather than a bar that quietly stops at the end of its track; the stylesheet
    caps the drawn width so the layout survives either way.
    """

    used: float | None
    total: float | None

    @property
    def known(self) -> bool:
        return self.used is not None and self.total is not None and self.total > 0

    @property
    def percent(self) -> float | None:
        if not self.known:
            return None
        return 100.0 * self.used / self.total


@dataclass(frozen=True)
class NodeMeters:
    """The four utilisation bars one node's runtime can fill.

    Every member is a :class:`Meter`, so a node reporting memory but not swap
    produces one bar and one absence rather than one bar and a zero.
    """

    cpu: Meter
    memory: Meter
    swap: Meter
    rootfs: Meter


def node_meters(runtime) -> NodeMeters:
    """Meters for one node's runtime metrics.

    CPU is expressed as busy cores out of total cores rather than as the provider's
    bare 0..1 fraction, so it shares the used-of-total shape with the other three and
    aggregates by addition at the cluster level. A node that reports usage but not a
    core count yields an unknown CPU meter: a fraction with no denominator is a
    percentage, not a capacity, and this phase draws capacity.
    """

    cores = runtime.cpu_cores
    usage = runtime.cpu_usage
    return NodeMeters(
        cpu=Meter(used=usage * cores if usage is not None and cores else None, total=cores),
        memory=Meter(used=runtime.memory_used_bytes, total=runtime.memory_total_bytes),
        swap=Meter(used=runtime.swap_used_bytes, total=runtime.swap_total_bytes),
        rootfs=Meter(used=runtime.rootfs_used_bytes, total=runtime.rootfs_total_bytes),
    )


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
    #: Cores busy across the contributing nodes, summed as ``usage x cores`` per node
    #: rather than averaged over the fractions. A 4-core node at 100% and a 64-core
    #: node at 0% is 4 busy cores of 68, not 50%.
    cpu_used_cores: float
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

    @property
    def cpu(self) -> Meter:
        return Meter(used=self.cpu_used_cores, total=self.cpu_cores)

    @property
    def memory(self) -> Meter:
        return Meter(used=self.memory_used_bytes, total=self.memory_total_bytes)

    @property
    def rootfs(self) -> Meter:
        return Meter(used=self.rootfs_used_bytes, total=self.rootfs_total_bytes)


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

    @property
    def meters(self) -> NodeMeters | None:
        """This node's bars, or ``None`` when its own runtime is not current.

        `_capacity` excludes exactly these nodes from the totals in the panel above,
        so drawing their last-known bars here would put a figure in the breakdown
        that the sum it explains does not contain. The row still renders — with its
        runtime badge, which is the honest answer to what the bars would have shown.
        """

        return node_meters(self.node.runtime) if self.node.runtime_current else None


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
    #: Cluster members this connection does not list, because they are hidden or
    #: unenrolled. Quorum is corosync's answer about *every* member, while the node
    #: count beside it covers only the listed ones -- two denominators in one panel.
    #: Enrollment is a pve-helper decision and changes nothing in Proxmox, so a
    #: hidden node keeps voting; a page reading "2 of 2, quorate" while a third
    #: member carries the quorum states neither fact wrongly and the pair
    #: misleadingly. This is what lets the panel name the gap instead.
    members_not_listed: int


@dataclass(frozen=True)
class NodeSummaryRead:
    node: ClusterNodeProjectionRead
    placement: NodePlacement

    @property
    def meters(self) -> NodeMeters:
        """Always present, unlike the cluster row's.

        This page is *about* this node and already carries a banner naming a
        non-current runtime, so its bars are read in the presence of that statement.
        The cluster row has no such place to say it and therefore withholds them.
        """

        return node_meters(self.node.runtime)


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
        cpu_used_cores=sum(
            node.runtime.cpu_usage * node.runtime.cpu_cores
            for node in contributing
            if node.runtime.cpu_usage is not None and node.runtime.cpu_cores
        ),
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
        # Only from a current membership read. A stale or never-published generation
        # makes `member_count` a number from some earlier shape of the cluster, and
        # subtracting today's listed nodes from it would invent a hidden member —
        # exactly the kind of confident arithmetic over mixed-vintage inputs this
        # panel is being fixed for. Clamped because the two sides are published by
        # different passes and may cross.
        members_not_listed=max(0, projection.member_count - len(nodes)) if projection.membership_current else 0,
    )


def node_summary(cluster, node: ClusterNodeProjectionRead) -> NodeSummaryRead:
    """Compose Node Summary for one exact NodeRef.

    Takes no cluster freshness argument on purpose: there is nothing for this view
    to borrow it from.
    """

    placement = guest_placement(cluster, nodes=(node.node_name,))
    return NodeSummaryRead(node=node, placement=placement[node.node_name])


@dataclass(frozen=True)
class WorkspaceGuestRow:
    """One guest as the workspace tables render it.

    Carries the Module 3 link rather than the pieces to build it, so a template
    cannot assemble a URL that disagrees with the canonical route. Module 5 does not
    re-own guest state: every row is a link into the guest workspace.
    """

    cluster_key: str
    object_type: str
    vmid: int
    name: str
    node: str
    status: str
    url: str
    #: Empty when the guest sits on a node this connection no longer lists. Linking
    #: it would 404: the row is real, the node page is not.
    node_url: str

    @property
    def running(self) -> bool:
        return self.status == "running"


def workspace_guest_rows(
    cluster, *, node: str = "", listed_nodes: tuple[str, ...] | None = None
) -> tuple[WorkspaceGuestRow, ...]:
    """Published guests of one cluster, optionally narrowed to one node.

    **One query, no provider call.** The workspace tables are a read of the current
    projection; the live-inventory and guest-lock fan-outs that any *guest* page can
    reach are deliberately not on this path, because a table that walks every node
    to draw a status column is the render-path fan-out Module 5 exists to remove.

    Ordered by identity rather than by name so two guests that share a display name
    keep a stable, distinguishable order, and duplicate ``(object_type, vmid)``
    across clusters stays visible as two rows rather than collapsing into one.
    """

    from django.urls import reverse

    rows = published_guest_queryset().filter(cluster=cluster)
    if node:
        rows = rows.filter(node=node)
    linkable = frozenset(listed_nodes) if listed_nodes is not None else None
    return tuple(
        WorkspaceGuestRow(
            cluster_key=cluster.key,
            object_type=row.object_type,
            vmid=row.vmid,
            name=row.name,
            node=row.node,
            status=row.status,
            url=reverse(
                "core:guest_summary",
                kwargs={"cluster_key": cluster.key, "object_type": row.object_type, "vmid": row.vmid},
            ),
            node_url=(
                reverse("core:node_summary", kwargs={"cluster_key": cluster.key, "node": row.node})
                if row.node and (linkable is None or row.node in linkable)
                else ""
            ),
        )
        for row in rows.only("object_type", "vmid", "name", "node", "status").order_by("node", "object_type", "vmid")
    )

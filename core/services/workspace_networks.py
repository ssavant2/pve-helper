"""Composition for the workspace Networks tab (phase 5a4B-ii).

Presentation-free, and it originates no read of its own: every value comes from the
node-network projection through :mod:`core.services.cluster_projection_read`, which
5a4B-i publishes on its own cadence. Rendering this tab makes zero provider calls,
whatever the node count.

Three decisions the shape below encodes:

**The grain is (node, interface), and it never collapses.** A bridge name is not a
cluster object. `vmbr0` on pve1 and `vmbr0` on pve2 are two devices that happen to
share a name — different ports, possibly different VLAN configuration — and merging
them into one cluster-wide row would put one node's ports under the other's name.
The cluster scope therefore groups by node; it does not deduplicate across nodes.

**A node with no rows is not a node with no network.** `MISSING`, `FAILED` and
`NOT_PUBLISHED` each produce an empty interface list and mean entirely different
things, so each group carries its status and the publisher's own reason rather than
rendering as an empty table.

**An unreachable row is shown, not filtered.** When a node does not answer, 5a4B-i
keeps its last known interfaces and marks them unknown; rendering nothing would say
"no bridges", which is the one thing an unanswered node must never be read as. A row
the node *stopped reporting* under a complete read is a different case and no longer
reaches this module at all — it is deleted at the source, because coverage already
separates "gone" from "never swept" and Proxmox keeps no such history either.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.services.cluster_projection_read import (
    NodeInterfaceRead,
    NodeNetworkRead,
    NodeNetworkReadStatus,
    read_node_networks,
)
from core.services.node_networks import node_network_reason
from core.services.publication_scope import PublicationScope, publication_scope


@dataclass(frozen=True)
class NetworkNodeGroup:
    """One node's interfaces, plus why the list is what it is."""

    node: str
    status: NodeNetworkReadStatus
    #: Empty when the node's state is current; otherwise the publisher's reason in
    #: the operator's words, from the same map the migrate dialog uses.
    reason: str
    interfaces: tuple[NodeInterfaceRead, ...]
    generation: int
    observed_at: object

    @property
    def known(self) -> bool:
        return self.status is NodeNetworkReadStatus.CURRENT

    @property
    def attachable(self) -> tuple[NodeInterfaceRead, ...]:
        return tuple(row for row in self.interfaces if row.attachable and row.present)


@dataclass(frozen=True)
class NetworkPanel:
    groups: tuple[NetworkNodeGroup, ...]
    #: Discovered members this connection does not read, so the tab can say what it
    #: is not showing rather than quietly showing less. Same footnote as Datastores.
    unread_nodes: tuple[str, ...]

    @property
    def interface_count(self) -> int:
        return sum(len(group.interfaces) for group in self.groups)

    @property
    def unknown_nodes(self) -> tuple[str, ...]:
        return tuple(group.node for group in self.groups if not group.known)


def _group(read: NodeNetworkRead) -> NetworkNodeGroup:
    return NetworkNodeGroup(
        node=read.node_name,
        status=read.status,
        reason=node_network_reason(read),
        interfaces=read.interfaces,
        generation=read.coverage.generation if read.coverage else 0,
        observed_at=read.coverage.observed_at if read.coverage else None,
    )


def network_panel(
    cluster,
    *,
    node: str = "",
    scope: PublicationScope | None = None,
    members: tuple[str, ...] = (),
) -> NetworkPanel:
    """Every published member's interfaces, or one node's.

    `members` is the cluster's discovered node names, passed in rather than read:
    the workspace shell has already loaded the membership projection, and re-reading
    it here would buy a query for a value sitting in the caller.

    A fixed number of queries regardless of how many nodes are asked about — the
    per-node fan-out this phase removed from the migrate dialog is not reintroduced
    on a page that renders every node at once.
    """

    if scope is None:
        scope = publication_scope(cluster)
    wanted = [node] if node else [name for name in members if scope.publishes(name)]
    reads = read_node_networks(cluster.key, wanted, scope=scope)
    return NetworkPanel(
        groups=tuple(_group(read) for read in sorted(reads, key=lambda read: read.node_name)),
        unread_nodes=tuple(sorted(name for name in members if not scope.observes(name))),
    )

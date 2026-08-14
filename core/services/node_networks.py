"""What a guest NIC may attach to on a given node.

Two surfaces ask this question — the migrate dialog's per-target check and the
create form's bridge list — and they used to answer it independently. Both were
wrong against live data, in opposite directions, which is the drift this module
exists to prevent: one reader, one verdict.

Since 5a4B-ii the answer is a **database read of the node-network projection**, not
a provider call. `GET nodes/<node>/network?type=any_bridge` is still the source of
truth; `core.services.cluster_node_networks` is the only thing that calls it, on
its own cadence, and this module reads what it published. The migrate dialog opened
one such call per candidate node on every open.

The provider answer is what it is because nothing else reproduces it: it returns
Linux/OVS bridges together with the SDN vnets actually realized on that node, and
it honours a zone's node restriction — a vnet in a zone scoped to `pve1,pve2` is
simply absent from pve3's answer. Filtering the plain interface listing by type and
merging in `cluster/sdn/vnets` does not reproduce it and cannot be repaired: a
realized vnet with no address is absent from the plain listing entirely, one that
has an address comes back `type=unknown`, and the cluster-wide vnet list carries no
node opinion at all.

**An empty answer is never an answer here.** The live version returned `[]` for a
node it could not reach, and both consumers rendered that as "no bridges" — a
proven-absent claim built from silence. Every result below therefore carries
`known`, and the bridge list is empty on every path where `known` is false.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from core.services.cluster_projection_read import (
    ClusterProjectionNotFound,
    NodeNetworkRead,
    NodeNetworkReadStatus,
    read_node_networks,
)

#: Why a node's network state is not current, in the operator's words. Keyed by the
#: publisher's coverage `error_code`.
#:
#: Spelled out here rather than imported from `cluster_node_networks`: this is the
#: read path, and a display map is not a reason to pull a publisher into it. The
#: risk that buys — a code the publisher writes and this map has never heard of —
#: is closed by `NodeNetworkReasonCoverageTests`, which imports both and fails on
#: any unmapped code, so the duplication cannot rot silently.
_REFUSAL_REASONS = {
    "node_not_published": "this connection is not set to publish that node",
    "node_absent_from_membership": "the node is no longer a member of this cluster",
    "node_offline_by_membership": "the cluster reports the node offline",
    "node_not_a_member": "the node is not a member of this cluster",
    "acquisition_disabled": "this connection is disabled",
    "acquisition_quarantined": "this connection is quarantined",
    "acquisition_retired": "this connection has been retired",
    "topology_transition_pending": "the cluster's topology is changing",
    "no_enabled_endpoint": "this connection has no enabled endpoint",
    "endpoints_exhausted": "no endpoint answered during the last sweep",
    "membership_not_published": "this cluster's membership has not been published yet",
    "provider_unauthorized": "the connection's token was refused",
    "provider_timeout": "the node timed out",
    "provider_error": "the node could not be read",
    "invalid_payload": "the node returned something that is not an interface list",
}

_MISSING_REASON = "its network has not been read yet"
_UNMAPPED_REASON = "its network could not be read"


@dataclass(frozen=True)
class NodeBridges:
    """Attachable bridges on one node, and whether that list means anything.

    `known` is not derivable from `bridges`: a node with genuinely no bridges and a
    node nobody could ask both produce an empty tuple, and they are opposite
    instructions to a caller deciding whether a migration target is usable.
    """

    node: str
    bridges: tuple[str, ...]
    known: bool
    #: A sentence fragment naming why, empty when `known`. Reads as "<node>'s
    #: network state is unknown: <reason>."
    reason: str

    def __contains__(self, bridge: object) -> bool:
        return bridge in self.bridges


def node_network_reason(read: NodeNetworkRead) -> str:
    """Why this node's network state is not current, in one sentence fragment.

    Public because the Networks tab renders the same sentence the migrate dialog
    blocks a target with. Two spellings of one reason is how a surface ends up
    contradicting the surface next to it.
    """

    if read.status is NodeNetworkReadStatus.CURRENT:
        return ""
    if read.status is NodeNetworkReadStatus.MISSING:
        return _MISSING_REASON
    if read.status is NodeNetworkReadStatus.NOT_PUBLISHED:
        return _REFUSAL_REASONS["node_not_published"]
    code = read.coverage.error_code if read.coverage else ""
    return _REFUSAL_REASONS.get(code, _UNMAPPED_REASON)


def _bridges(read: NodeNetworkRead) -> NodeBridges:
    return NodeBridges(
        node=read.node_name,
        bridges=read.attachable_bridges,
        known=read.known,
        reason=node_network_reason(read),
    )


def attachable_bridges_by_node(cluster, nodes: Sequence[str]) -> dict[str, NodeBridges]:
    """Every requested node's answer in one bulk read, whatever the node count.

    The migrate dialog asks for every candidate target at once; doing this per node
    is what the phase removed. A retired cluster — one that lost its managed scope
    between the caller's read and this one — yields *unknown* for every node rather
    than an exception, because the dialog's job at that point is to refuse targets,
    not to 500.
    """

    names = [name for name in dict.fromkeys(nodes) if name]
    if cluster is None or not names:
        return {}
    try:
        reads = read_node_networks(cluster.key, names)
    except ClusterProjectionNotFound:
        return {name: NodeBridges(name, (), False, _REFUSAL_REASONS["acquisition_retired"]) for name in names}
    return {read.node_name: _bridges(read) for read in reads}


def attachable_bridges(cluster, node: str) -> NodeBridges:
    """One node's answer. `attachable_bridges_by_node` for more than one.

    An empty node name needs no guard of its own: the bulk read drops it, and the
    default below is the same unknown answer a guard would have returned.
    """

    return attachable_bridges_by_node(cluster, [node]).get(node, NodeBridges(node, (), False, _MISSING_REASON))

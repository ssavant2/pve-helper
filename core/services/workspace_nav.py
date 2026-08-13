"""The Hosts & Clusters sidebar tree: which objects and nodes an operator may open.

Three sibling groups under one root, per the agreed shape:

``clusters``
    Corosync clusters, each expanding to its member nodes. A one-node corosync
    cluster is still a cluster and stays here.
``hosts``
    True standalone installations, as first-class visual siblings of clusters.
    **This is presentation, not identity.** Each remains a one-node
    ``ProxmoxCluster`` and its node is the same ``NodeRef(cluster_key, node)``;
    routes, projections, audit and actions stay cluster-qualified. There is no
    bare-node identity and no second standalone-host model.
``connections``
    A sibling leaf, never the tree's parent — rendered by the template, not here.

Two exclusions are load-bearing:

* **Retired objects never enter the tree.** ``managed_clusters()`` already drops
  them; they live on in Connections and Audit, which is where an operator goes to
  see a decommissioned cluster. Disabled and quarantined clusters *do* stay, with
  a reason, because disabling is how a verified retirement is prepared and
  removing them would delete the very inventory the operator is deciding about.
* **Only ``managed`` nodes are listed** (node-enrollment N6). A ``safety_only`` or
  unenrolled node is not a workspace object; it is visible in Connections. The
  filter is ``publication_scope``'s, not a second copy of the rule.

No provider call and no live fallback: an empty projection renders an empty group.
A cluster that has published no membership yet shows with no children rather than
falling back to a raw node list, because a raw list is exactly the unfiltered
answer enrollment exists to stop.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.urls import reverse

from core.models import ClusterMembershipState, ClusterNodeState
from core.services.cluster_scopes import managed_clusters
from core.services.cluster_state_labels import cluster_degraded_label
from core.services.cluster_topology_role import TopologyRole
from core.services.publication_scope import publication_scopes


@dataclass(frozen=True)
class WorkspaceNodeEntry:
    """One member node, as the tree renders it."""

    node_name: str
    url: str
    nav_key: str
    online: bool


@dataclass(frozen=True)
class WorkspaceObjectEntry:
    """One cluster or standalone host, with the nodes an operator may open."""

    cluster_key: str
    display_name: str
    url: str
    nav_key: str
    degraded: str
    nodes: tuple[WorkspaceNodeEntry, ...]


def cluster_nav_key(cluster_key: str) -> str:
    """The identity a cluster leaf is highlighted by."""

    return f"cluster:{cluster_key}"


def node_nav_key(cluster_key: str, node_name: str) -> str:
    """The identity a node leaf is highlighted by.

    Cluster-qualified because two clusters routinely have a ``pve1``, and a bare
    node-name comparison would light both.
    """

    return f"node:{cluster_key}:{node_name}"


def _published_nodes_by_cluster(clusters) -> dict[int, list[tuple[str, bool]]]:
    """Every listable node of every cluster, in **two** bulk queries.

    Flat in node count and flat in cluster count, which is the property the shell
    budget exists to hold. The obvious shape — resolve the publication scope inside
    a per-cluster loop — is 2N queries on a surface every HTML response renders, the
    exact quadratic-at-real-scale failure the budget was written against.
    """

    scopes = publication_scopes(clusters)
    nodes: dict[int, list[tuple[str, bool]]] = {}
    rows = (
        ClusterNodeState.objects.filter(cluster_id__in=list(scopes), present=True)
        .order_by("node_name")
        .values_list("cluster_id", "node_name", "online")
    )
    for cluster_id, node_name, online in rows:
        if not scopes[cluster_id].publishes(node_name):
            continue
        nodes.setdefault(cluster_id, []).append((node_name, online))
    return nodes


def workspace_nav(clusters=None) -> dict[str, list[WorkspaceObjectEntry]]:
    """Both groups of the tree, built from the projection alone.

    ``clusters`` lets the shell pass the managed list it has already read, so the
    tree costs the sidebar two queries rather than three.

    Deliberately uncached. The datastore tree caches because building it walks the
    storage catalog per cluster; this is two indexed bulk reads over rows the page
    beside it usually renders anyway, and a cached sidebar that disagrees with that
    page about which nodes exist is worse than the query.
    """

    if clusters is None:
        clusters = list(managed_clusters().order_by("display_name", "key"))
    membership_roles = dict(
        ClusterMembershipState.objects.filter(cluster_id__in=[cluster.pk for cluster in clusters]).values_list(
            "cluster_id", "topology_role"
        )
    )
    nodes_by_cluster = _published_nodes_by_cluster(clusters)

    grouped: dict[str, list[WorkspaceObjectEntry]] = {"clusters": [], "hosts": []}
    for cluster in clusters:
        entry = WorkspaceObjectEntry(
            cluster_key=cluster.key,
            display_name=cluster.display_name,
            url=reverse("core:cluster_summary", kwargs={"cluster_key": cluster.key}),
            nav_key=cluster_nav_key(cluster.key),
            degraded=cluster_degraded_label(cluster),
            nodes=tuple(
                WorkspaceNodeEntry(
                    node_name=node_name,
                    url=reverse("core:node_summary", kwargs={"cluster_key": cluster.key, "node": node_name}),
                    nav_key=node_nav_key(cluster.key, node_name),
                    online=online,
                )
                for node_name, online in nodes_by_cluster.get(cluster.pk, ())
            ),
        )
        # An unread or in-transition topology is not evidence of standalone. Only a
        # positively observed standalone role moves an object out of Clusters; the
        # unknown case stays where its routes and projections already point.
        role = membership_roles.get(cluster.pk, TopologyRole.UNKNOWN)
        grouped["hosts" if role == TopologyRole.STANDALONE else "clusters"].append(entry)
    return grouped

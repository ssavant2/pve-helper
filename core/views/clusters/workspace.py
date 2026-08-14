"""The Hosts & Clusters workspace shell: canonical routes and tab ownership.

Phase 5a2A+B. This module owns *where an operator can be* — the cluster and node
detail routes, which tab is active, and which tabs exist but are not built yet. The
tab bodies are separate phases: Summary's content is 5a2C/5a2D, Hosts and VMs are
5a2E/5a2F, and everything after Monitor belongs to its own family.

Two rules the whole workspace inherits:

* **Passive rendering makes no provider call.** Every value comes from the accepted
  projection through :mod:`core.services.cluster_projection_read`. A missing card
  is missing; there is no live fallback, because a fallback is exactly the raw,
  unfiltered answer the enrollment boundary exists to prevent.
* **A node route is cluster-qualified.** `/clusters/<key>/nodes/<node>/<tab>/`
  serves clustered and standalone nodes alike. There is no `/hosts/` alias and no
  bare-node route: "host" is a tree grouping, not an identity.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.http import Http404
from django.shortcuts import render
from django.urls import reverse

from core.services.cluster_projection_read import (
    ClusterProjectionNotFound,
    read_cluster_projection,
)
from core.services.cluster_state_labels import cluster_degraded_label
from core.services.publication_scope import publication_scope
from core.services.workspace_datastores import datastore_panel
from core.services.workspace_nav import cluster_nav_key, node_nav_key
from core.services.workspace_networks import network_panel
from core.services.workspace_summary import cluster_summary as compose_cluster_summary
from core.services.workspace_summary import node_summary as compose_node_summary
from core.views.cluster_scope import managed_cluster_from_path
from core.views.common import app_login_required, navigation_context

#: Cluster tabs in vSphere order. Only `summary` is routed; the rest render as
#: disabled labels so the shell states the intended shape without pretending the
#: views exist. A tab becomes enabled by gaining a route, not by editing a flag.
CLUSTER_TABS = (
    ("summary", "Summary"),
    ("monitor", "Monitor"),
    ("configure", "Configure"),
    ("permissions", "Permissions"),
    ("hosts", "Hosts"),
    ("vms", "VMs"),
    ("datastores", "Datastores"),
    ("networks", "Networks"),
    ("updates", "Updates"),
)

#: Node tabs. Same order minus Hosts, which is a cluster-only view.
NODE_TABS = (
    ("summary", "Summary"),
    ("monitor", "Monitor"),
    ("configure", "Configure"),
    ("permissions", "Permissions"),
    ("vms", "VMs"),
    ("datastores", "Datastores"),
    ("networks", "Networks"),
    ("updates", "Updates"),
)

#: The tabs that have a route today. Kept as one set rather than a per-tab flag so
#: a later phase enables its tab by adding the route and the name here together.
ROUTED_CLUSTER_TABS = {
    "summary": "core:cluster_summary",
    "hosts": "core:cluster_hosts",
    "vms": "core:cluster_vms",
    "datastores": "core:cluster_datastores",
    "networks": "core:cluster_networks",
}
ROUTED_NODE_TABS = {
    "summary": "core:node_summary",
    "vms": "core:node_vms",
    "datastores": "core:node_datastores",
    "networks": "core:node_networks",
}


@dataclass(frozen=True)
class WorkspaceTab:
    key: str
    label: str
    url: str
    active: bool
    enabled: bool


def _tabs(specs, routed, *, active: str, **kwargs) -> tuple[WorkspaceTab, ...]:
    return tuple(
        WorkspaceTab(
            key=key,
            label=label,
            url=reverse(routed[key], kwargs=kwargs) if key in routed else "",
            active=key == active,
            enabled=key in routed,
        )
        for key, label in specs
    )


def _published_nodes(projection, cluster) -> tuple:
    """The nodes this connection may show, in projection order.

    One helper rather than the same comprehension at each tab: a tab that resolved
    its own subset could disagree with the tree about which nodes exist, and the
    disagreement would look like a data problem rather than a code one.
    """

    scope = publication_scope(cluster)
    return tuple(node for node in projection.nodes if node.present and scope.publishes(node.node_name))


def _published_node_or_404(projection, cluster, node: str):
    """Resolve one node inside the publication boundary, or 404.

    An unpublished node has no workspace page on any tab. Rendering one would be the
    leak the boundary exists to stop: the operator hid the node, and a typed URL
    would still show its guests. It is 404, not a refusal, because from the
    workspace's point of view the object is not there.
    """

    match = next((row for row in _published_nodes(projection, cluster) if row.node_name == node), None)
    if match is None:
        raise Http404("Proxmox node not found")
    return match


def _projection_or_404(cluster):
    """The accepted read, or 404 for a key outside the managed scope.

    `managed_cluster_from_path` has already refused a retired key. This second
    lookup can still miss when a cluster is retired between the two reads, and a
    half-rendered workspace for an object that no longer exists is worse than a 404.
    """

    try:
        return read_cluster_projection(cluster.key)
    except ClusterProjectionNotFound as exc:
        raise Http404("Proxmox cluster not found") from exc


@app_login_required
def cluster_summary(request, cluster_key: str):
    cluster = managed_cluster_from_path(cluster_key)
    projection = _projection_or_404(cluster)
    published = _published_nodes(projection, cluster)
    context = {
        "cluster": cluster,
        "projection": projection,
        "summary": compose_cluster_summary(cluster, projection, published),
        "cluster_degraded": cluster_degraded_label(cluster),
        "workspace_object": projection.display_name,
        "workspace_kind": "cluster",
        "published_nodes": published,
        "tabs": _tabs(CLUSTER_TABS, ROUTED_CLUSTER_TABS, active="summary", cluster_key=cluster.key),
        "workspace_nav_key": cluster_nav_key(cluster.key),
        **navigation_context("hosts_clusters", page_title=(projection.display_name, "Hosts & Clusters")),
    }
    return render(request, "core/cluster_summary.html", context)


@app_login_required
def node_summary(request, cluster_key: str, node: str):
    cluster = managed_cluster_from_path(cluster_key)
    projection = _projection_or_404(cluster)
    match = _published_node_or_404(projection, cluster, node)
    context = {
        "cluster": cluster,
        "projection": projection,
        "node": match,
        "summary": compose_node_summary(cluster, match),
        "workspace_object": match.node_name,
        "workspace_kind": "node",
        "tabs": _tabs(
            NODE_TABS,
            ROUTED_NODE_TABS,
            active="summary",
            cluster_key=cluster.key,
            node=match.node_name,
        ),
        "workspace_nav_key": node_nav_key(cluster.key, match.node_name),
        **navigation_context(
            "hosts_clusters",
            page_title=(match.node_name, projection.display_name, "Hosts & Clusters"),
        ),
    }
    return render(request, "core/node_summary.html", context)


@app_login_required
def cluster_hosts(request, cluster_key: str):
    """The Hosts tab: this cluster's enrolled members, membership-grained.

    Composes membership and per-node runtime only. Health, version and guest counts
    come from projections this module already owns; HA role and update rollups are
    named in the tab mapping and belong to 5d1 and 5b1, so they are absent rather
    than rendered empty.
    """

    cluster = managed_cluster_from_path(cluster_key)
    projection = _projection_or_404(cluster)
    published = _published_nodes(projection, cluster)
    context = {
        "cluster": cluster,
        "projection": projection,
        "summary": compose_cluster_summary(cluster, projection, published),
        "cluster_degraded": cluster_degraded_label(cluster),
        "workspace_object": projection.display_name,
        "workspace_kind": "cluster",
        "tabs": _tabs(CLUSTER_TABS, ROUTED_CLUSTER_TABS, active="hosts", cluster_key=cluster.key),
        "workspace_nav_key": cluster_nav_key(cluster.key),
        **navigation_context("hosts_clusters", page_title=("Hosts", projection.display_name)),
    }
    return render(request, "core/cluster_hosts.html", context)


@app_login_required
def cluster_vms(request, cluster_key: str):
    """The shared VM Overview, locked to one cluster's published guests."""

    cluster = managed_cluster_from_path(cluster_key)
    projection = _projection_or_404(cluster)
    from core.services.current_guest_inventory import published_guest_queryset
    from core.views.guests.read_model_support import _vms_workspace_context

    overview = _vms_workspace_context(
        "vms_overview",
        current_guests=published_guest_queryset().filter(cluster=cluster).select_related("cluster"),
        show_cluster_filter=False,
    )
    context = {
        **overview,
        "cluster": cluster,
        "projection": projection,
        "workspace_object": projection.display_name,
        "workspace_kind": "cluster",
        "workspace_scope_label": projection.display_name,
        "tabs": _tabs(CLUSTER_TABS, ROUTED_CLUSTER_TABS, active="vms", cluster_key=cluster.key),
        "workspace_nav_key": cluster_nav_key(cluster.key),
        **navigation_context("hosts_clusters", page_title=("VMs", projection.display_name)),
    }
    return render(request, "core/workspace_vms.html", context)


@app_login_required
def node_vms(request, cluster_key: str, node: str):
    """The shared VM Overview, locked to one exact published NodeRef."""

    cluster = managed_cluster_from_path(cluster_key)
    projection = _projection_or_404(cluster)
    match = _published_node_or_404(projection, cluster, node)
    from core.services.current_guest_inventory import published_guest_queryset
    from core.views.guests.read_model_support import _vms_workspace_context

    overview = _vms_workspace_context(
        "vms_overview",
        current_guests=(
            published_guest_queryset().filter(cluster=cluster, node=match.node_name).select_related("cluster")
        ),
        show_cluster_filter=False,
    )
    context = {
        **overview,
        "cluster": cluster,
        "projection": projection,
        "node": match,
        "workspace_object": match.node_name,
        "workspace_kind": "node",
        "workspace_scope_label": match.node_name,
        "tabs": _tabs(
            NODE_TABS,
            ROUTED_NODE_TABS,
            active="vms",
            cluster_key=cluster.key,
            node=match.node_name,
        ),
        "workspace_nav_key": node_nav_key(cluster.key, match.node_name),
        **navigation_context("hosts_clusters", page_title=("VMs", match.node_name, projection.display_name)),
    }
    return render(request, "core/workspace_vms.html", context)


@app_login_required
def cluster_datastores(request, cluster_key: str):
    """The Datastores tab: this cluster's published datastores and their reachability.

    Composition only — the catalog is refreshed by its own worker lane and by the
    Refresh button on a datastore's own page, never by rendering this list.
    """

    cluster = managed_cluster_from_path(cluster_key)
    projection = _projection_or_404(cluster)
    scope = publication_scope(cluster)
    context = {
        "cluster": cluster,
        "projection": projection,
        "panel": datastore_panel(cluster, scope=scope, members=_member_names(projection)),
        "cluster_degraded": cluster_degraded_label(cluster),
        "workspace_object": projection.display_name,
        "workspace_kind": "cluster",
        "tabs": _tabs(CLUSTER_TABS, ROUTED_CLUSTER_TABS, active="datastores", cluster_key=cluster.key),
        "workspace_nav_key": cluster_nav_key(cluster.key),
        **navigation_context("hosts_clusters", page_title=("Datastores", projection.display_name)),
    }
    return render(request, "core/cluster_datastores.html", context)


@app_login_required
def node_datastores(request, cluster_key: str, node: str):
    """The same composition, locked to what one published node sees."""

    cluster = managed_cluster_from_path(cluster_key)
    projection = _projection_or_404(cluster)
    match = _published_node_or_404(projection, cluster, node)
    scope = publication_scope(cluster)
    context = {
        "cluster": cluster,
        "projection": projection,
        "node": match,
        "panel": datastore_panel(
            cluster,
            node=match.node_name,
            scope=scope,
            members=_member_names(projection),
        ),
        "workspace_object": match.node_name,
        "workspace_kind": "node",
        "tabs": _tabs(
            NODE_TABS,
            ROUTED_NODE_TABS,
            active="datastores",
            cluster_key=cluster.key,
            node=match.node_name,
        ),
        "workspace_nav_key": node_nav_key(cluster.key, match.node_name),
        **navigation_context(
            "hosts_clusters",
            page_title=("Datastores", match.node_name, projection.display_name),
        ),
    }
    return render(request, "core/node_datastores.html", context)


def _member_names(projection) -> tuple[str, ...]:
    """Discovered members, from the read the shell already paid for."""

    return tuple(node.node_name for node in projection.nodes if node.present)


def workspace_object_urls(cluster, node: str = "") -> dict[str, str]:
    """Canonical workspace links for one guest's cluster and node, or blanks.

    Module 3's Related Objects card links back here now that these routes exist.
    Both values are conditional and blank rather than guessed: a retired cluster and
    an unpublished node each have no workspace page, and a link to one would 404 from
    a card whose whole job is to be a reliable jumping-off point.

    Lives in the workspace module because it owns these routes; the guest read model
    consumes it rather than assembling URLs from parts.
    """

    from core.services.cluster_scopes import managed_clusters

    if cluster is None or not managed_clusters().filter(pk=cluster.pk).exists():
        return {"cluster_url": "", "node_url": ""}
    cluster_url = reverse("core:cluster_summary", kwargs={"cluster_key": cluster.key})
    node_url = ""
    if node and publication_scope(cluster).publishes(node):
        node_url = reverse("core:node_summary", kwargs={"cluster_key": cluster.key, "node": node})
    return {"cluster_url": cluster_url, "node_url": node_url}


def _network_panel_or_404(cluster, **kwargs):
    """The Networks composition, or 404 for a cluster retired mid-request.

    Same reasoning as `_projection_or_404`, one read later: the panel resolves the
    managed cluster again, and a retirement landing between the two turns a rendered
    tab into a page about an object that no longer exists.
    """

    try:
        return network_panel(cluster, **kwargs)
    except ClusterProjectionNotFound as exc:
        raise Http404("Proxmox cluster not found") from exc


@app_login_required
def cluster_networks(request, cluster_key: str):
    """The Networks tab: what each published node reports about its interfaces.

    Grouped by node and never merged: `vmbr0` on two nodes is two devices sharing a
    name. Composition only — the projection is refreshed by its own worker lane, and
    rendering this page issues no provider call at any node count.
    """

    cluster = managed_cluster_from_path(cluster_key)
    projection = _projection_or_404(cluster)
    scope = publication_scope(cluster)
    context = {
        "cluster": cluster,
        "projection": projection,
        "panel": _network_panel_or_404(cluster, scope=scope, members=_member_names(projection)),
        "cluster_degraded": cluster_degraded_label(cluster),
        "workspace_object": projection.display_name,
        "workspace_kind": "cluster",
        "tabs": _tabs(CLUSTER_TABS, ROUTED_CLUSTER_TABS, active="networks", cluster_key=cluster.key),
        "workspace_nav_key": cluster_nav_key(cluster.key),
        **navigation_context("hosts_clusters", page_title=("Networks", projection.display_name)),
    }
    return render(request, "core/cluster_networks.html", context)


@app_login_required
def node_networks(request, cluster_key: str, node: str):
    """The same composition, locked to one published node."""

    cluster = managed_cluster_from_path(cluster_key)
    projection = _projection_or_404(cluster)
    match = _published_node_or_404(projection, cluster, node)
    scope = publication_scope(cluster)
    context = {
        "cluster": cluster,
        "projection": projection,
        "node": match,
        "panel": _network_panel_or_404(cluster, node=match.node_name, scope=scope, members=_member_names(projection)),
        "workspace_object": match.node_name,
        "workspace_kind": "node",
        "tabs": _tabs(
            NODE_TABS,
            ROUTED_NODE_TABS,
            active="networks",
            cluster_key=cluster.key,
            node=match.node_name,
        ),
        "workspace_nav_key": node_nav_key(cluster.key, match.node_name),
        **navigation_context(
            "hosts_clusters",
            page_title=("Networks", match.node_name, projection.display_name),
        ),
    }
    return render(request, "core/node_networks.html", context)

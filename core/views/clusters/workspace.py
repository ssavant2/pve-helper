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
from core.services.publication_scope import publication_scope
from core.services.workspace_nav import cluster_nav_key, node_nav_key
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
ROUTED_CLUSTER_TABS = {"summary": "core:cluster_summary"}
ROUTED_NODE_TABS = {"summary": "core:node_summary"}


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
    scope = publication_scope(cluster)
    published = tuple(node for node in projection.nodes if node.present and scope.publishes(node.node_name))
    context = {
        "cluster": cluster,
        "projection": projection,
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
    scope = publication_scope(cluster)
    # An unpublished node has no workspace page. Rendering one would be the exact
    # leak the boundary exists to stop: the operator hid the node, and a typed URL
    # would still show its runtime. It is 404, not a refusal page, because from the
    # workspace's point of view the object is not there.
    match = next(
        (row for row in projection.nodes if row.node_name == node and row.present and scope.publishes(row.node_name)),
        None,
    )
    if match is None:
        raise Http404("Proxmox node not found")
    context = {
        "cluster": cluster,
        "projection": projection,
        "node": match,
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

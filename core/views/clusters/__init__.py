"""Public Connections views.

Implementation details live in :mod:`core.views.clusters.connections`.  Keeping
this facade limited to URL-facing callables prevents later operational cluster
workspace views from inheriting the Connections module's private helpers.
"""

from .connections import (
    cluster_add,
    cluster_connection,
    cluster_connection_action,
    cluster_endpoint_action,
    cluster_endpoint_add,
    clusters_overview,
)
from .enrollment import cluster_enrollment_activate, cluster_node_action, cluster_node_add
from .workspace import cluster_summary, node_summary

__all__ = [
    "cluster_add",
    "cluster_connection",
    "cluster_connection_action",
    "cluster_endpoint_action",
    "cluster_endpoint_add",
    "cluster_enrollment_activate",
    "cluster_node_action",
    "cluster_node_add",
    "cluster_summary",
    "clusters_overview",
    "node_summary",
]

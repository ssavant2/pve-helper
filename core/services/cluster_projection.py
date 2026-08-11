"""Lifecycle owner for Module 5's mutable Hosts & Clusters projection.

Membership, node runtime and their coverage are reconstructible current state.
Retirement removes them together so a retired connection cannot keep looking
partly live, while hard deletion accounts for the same three relations through
its generic reconstructible-state teardown.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from core.models import ClusterMembershipState, ClusterNodeState, ClusterProjectionCoverage
from core.services.cluster_footprint import FOOTPRINT_HOST_PROJECTION, stamp_operational_footprint


@dataclass(frozen=True)
class ClusterProjectionRetirementResult:
    membership_rows_deleted: int
    node_rows_deleted: int
    coverage_rows_deleted: int


def stamp_cluster_projection_footprint(cluster) -> bool:
    """Declare the reconstructible footprint used by 5a1B/5a1C publishers."""

    return stamp_operational_footprint(cluster, reason=FOOTPRINT_HOST_PROJECTION)


@transaction.atomic
def retire_cluster_projection(cluster) -> ClusterProjectionRetirementResult:
    """Remove one cluster's complete mutable host projection, with counts."""

    coverage_rows_deleted = ClusterProjectionCoverage._base_manager.filter(cluster_id=cluster.pk).delete()[0]
    node_rows_deleted = ClusterNodeState._base_manager.filter(cluster_id=cluster.pk).delete()[0]
    membership_rows_deleted = ClusterMembershipState._base_manager.filter(cluster_id=cluster.pk).delete()[0]
    return ClusterProjectionRetirementResult(
        membership_rows_deleted=membership_rows_deleted,
        node_rows_deleted=node_rows_deleted,
        coverage_rows_deleted=coverage_rows_deleted,
    )

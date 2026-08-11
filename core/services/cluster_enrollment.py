"""Lifecycle owner for operator-owned cluster node enrollments."""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from core.models import ClusterNodeEnrollment

ENROLLMENT_RETIREMENT_DETAIL_LIMIT = 100


@dataclass(frozen=True)
class EnrollmentRetirementResult:
    enrollment_rows_deleted: int
    enrollments: tuple[dict[str, str], ...]
    enrollments_omitted: int


@transaction.atomic
def retire_cluster_enrollments(cluster) -> EnrollmentRetirementResult:
    """Snapshot non-secret configuration and remove every enrollment row."""

    enrollments = tuple(
        ClusterNodeEnrollment._base_manager.filter(cluster_id=cluster.pk)
        .order_by("node_name")
        .values("node_ref_snapshot", "mode")[:ENROLLMENT_RETIREMENT_DETAIL_LIMIT]
    )
    total = ClusterNodeEnrollment._base_manager.filter(cluster_id=cluster.pk).count()
    deleted = ClusterNodeEnrollment._base_manager.filter(cluster_id=cluster.pk).delete()[0]
    return EnrollmentRetirementResult(
        enrollment_rows_deleted=deleted,
        enrollments=enrollments,
        enrollments_omitted=max(0, total - len(enrollments)),
    )

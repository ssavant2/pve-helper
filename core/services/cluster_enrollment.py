"""Lifecycle and write owner for operator-owned cluster node enrollments.

Every enrollment mutation in the application goes through this module. Views never
touch :class:`ClusterNodeEnrollment` directly — pinned by
``EnrollmentWriterInvariantTests`` rather than left as prose, because the 5a1F
projection ratchet does not cover this model.

The generation clock is advanced here and nowhere else. Migration ``0012`` installs
only the identity trigger, so nothing in the database advances
``enrollment_generation``; a single application-side advance per committed change is
correct and is not a double-advance.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from core.models import (
    ClusterNodeEnrollment,
    ConsoleSession,
    CurrentGuestInventory,
    ScheduledAction,
)
from core.services.cluster_lifecycle_lock import acquire_operable_cluster
from core.services.public_errors import PublicMessageError

ENROLLMENT_RETIREMENT_DETAIL_LIMIT = 100

#: How many blocking targets a refusal names before it summarizes the rest. A gate
#: that prints two hundred guest names is not a gate the operator can act on.
BLOCKER_DETAIL_LIMIT = 10


class ClusterEnrollmentError(PublicMessageError, RuntimeError):
    """An enrollment write was refused. The message is safe to show an operator."""


@dataclass(frozen=True)
class EnrollmentWrite:
    """What a committed write did, so the caller can audit it without re-reading."""

    enrollment: ClusterNodeEnrollment
    changed: bool
    generation: int
    previous_mode: str = ""


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


def enrollments_by_node(cluster) -> dict[str, ClusterNodeEnrollment]:
    """Every enrollment on one cluster, keyed by node name.

    The read seam for surfaces. Views compose panels from this rather than querying
    the model, so ``EnrollmentWriterInvariantTests`` can forbid the model in view
    modules outright instead of trying to tell a read from a write in the AST.
    """

    return {
        row.node_name: row
        for row in ClusterNodeEnrollment.objects.filter(cluster=cluster)
        .select_related("onboarded_via_endpoint")
        .order_by("node_name")
    }


def node_change_blockers(cluster, node_name: str) -> list[str]:
    """Durable work that a hide/remove would silently retarget, named for the operator.

    Deliberately **not** ``active_cluster_operation_labels``: that helper is
    cluster-grained and counts every queued/running :class:`AuditEvent` on the
    cluster, including the read-only reconciliation an enrollment itself queues — so
    reusing it would make enrolling one node block every other node until the
    refresh finished.

    ``ScheduledAction.target_node`` is a snapshot written at save time. It is blank
    whenever the guest was not in the current inventory and is never refreshed after
    a migration, so filtering on it alone is both under-inclusive and stale. The
    sound predicate is the guest's *current* placement, with the snapshot as a
    second, independent chance to catch a row whose guest has since disappeared.
    """

    blockers: list[str] = []

    placed_guests = CurrentGuestInventory.objects.filter(cluster=cluster, node=node_name).values_list(
        "object_type", "vmid"
    )
    placed_keys = set(placed_guests)
    schedule_filter = ScheduledAction.objects.filter(cluster=cluster, enabled=True)
    scheduled: set[tuple[str, int]] = set()
    for target_type, target_vmid, target_node in schedule_filter.values_list(
        "target_type", "target_vmid", "target_node"
    ):
        if (target_type, target_vmid) in placed_keys or target_node == node_name:
            scheduled.add((target_type, target_vmid))
    if scheduled:
        blockers.append(_summarize("scheduled action", sorted(f"{kind}/{vmid}" for kind, vmid in scheduled)))

    consoles = ConsoleSession.objects.filter(
        cluster=cluster,
        target_node=node_name,
        expires_at__gt=timezone.now(),
        status__in=(
            ConsoleSession.Status.PENDING,
            ConsoleSession.Status.CONNECTING,
            ConsoleSession.Status.CONNECTED,
        ),
    ).count()
    if consoles:
        blockers.append(f"{consoles} live console session(s) on this node")

    return blockers


def _summarize(noun: str, items: list[str]) -> str:
    shown = items[:BLOCKER_DETAIL_LIMIT]
    remainder = len(items) - len(shown)
    listed = ", ".join(shown)
    if remainder > 0:
        listed = f"{listed} and {remainder} more"
    return f"{len(items)} {noun}(s) targeting this node: {listed}"


def _advance_generation(locked_cluster) -> int:
    """Advance the enrollment clock once, under the caller's already-locked row."""

    locked_cluster.enrollment_generation += 1
    locked_cluster.save(update_fields=["enrollment_generation", "updated_at"])
    return locked_cluster.enrollment_generation


def _validate_mode(mode: str) -> str:
    if mode not in {ClusterNodeEnrollment.Mode.MANAGED, ClusterNodeEnrollment.Mode.SAFETY_ONLY}:
        raise ClusterEnrollmentError("An enrollment mode must be either managed or safety only.")
    return mode


@transaction.atomic
def enroll_node(
    cluster,
    *,
    node_name: str,
    mode: str,
    actor=None,
    endpoint=None,
    require_enabled: bool = True,
) -> EnrollmentWrite:
    """Create one enrollment under the locked cluster row.

    Re-enrolling an already-enrolled node is a no-op that advances nothing; changing
    its mode is :func:`change_enrollment_mode`'s job and says so, because the two
    have different Audit actions and different operator consequences.
    """

    node_name = str(node_name or "").strip()
    if not node_name or ":" in node_name:
        raise ClusterEnrollmentError("A node name is required and may not contain ':'.")
    mode = _validate_mode(mode)
    locked = acquire_operable_cluster(cluster, require_enabled=require_enabled)

    existing = ClusterNodeEnrollment.objects.select_for_update().filter(cluster=locked, node_name=node_name).first()
    if existing is not None:
        return EnrollmentWrite(
            enrollment=existing,
            changed=False,
            generation=locked.enrollment_generation,
            previous_mode=existing.mode,
        )

    enrollment = ClusterNodeEnrollment(
        cluster=locked,
        node_name=node_name,
        mode=mode,
        enrolled_at=timezone.now(),
        enrolled_by=actor,
        onboarded_via_endpoint=endpoint,
    )
    enrollment.save()
    return EnrollmentWrite(enrollment=enrollment, changed=True, generation=_advance_generation(locked))


@transaction.atomic
def change_enrollment_mode(cluster, *, node_name: str, mode: str, actor=None, reason: str = "") -> EnrollmentWrite:
    """Move one enrollment between managed and safety-only."""

    mode = _validate_mode(mode)
    locked = acquire_operable_cluster(cluster)
    enrollment = ClusterNodeEnrollment.objects.select_for_update().filter(cluster=locked, node_name=node_name).first()
    if enrollment is None:
        raise ClusterEnrollmentError(f"Node '{node_name}' is not enrolled on this cluster.")

    previous_mode = enrollment.mode
    if previous_mode == mode:
        return EnrollmentWrite(
            enrollment=enrollment,
            changed=False,
            generation=locked.enrollment_generation,
            previous_mode=previous_mode,
        )

    blockers = node_change_blockers(locked, node_name)
    if blockers:
        raise ClusterEnrollmentError("Resolve this durable work first: " + "; ".join(blockers))

    enrollment.mode = mode
    enrollment.mode_changed_at = timezone.now()
    enrollment.mode_changed_by = actor
    enrollment.mode_change_reason = str(reason or "")[:1000]
    enrollment.save(
        update_fields=["mode", "mode_changed_at", "mode_changed_by", "mode_change_reason", "updated_at"],
    )
    return EnrollmentWrite(
        enrollment=enrollment,
        changed=True,
        generation=_advance_generation(locked),
        previous_mode=previous_mode,
    )


@transaction.atomic
def remove_enrollment(cluster, *, node_name: str) -> EnrollmentWrite:
    """Stop reading a node entirely. The endpoint, if any, is left alone."""

    locked = acquire_operable_cluster(cluster)
    enrollment = ClusterNodeEnrollment.objects.select_for_update().filter(cluster=locked, node_name=node_name).first()
    if enrollment is None:
        raise ClusterEnrollmentError(f"Node '{node_name}' is not enrolled on this cluster.")

    blockers = node_change_blockers(locked, node_name)
    if blockers:
        raise ClusterEnrollmentError("Resolve this durable work first: " + "; ".join(blockers))

    previous_mode = enrollment.mode
    enrollment.delete()
    return EnrollmentWrite(
        enrollment=enrollment,
        changed=True,
        generation=_advance_generation(locked),
        previous_mode=previous_mode,
    )

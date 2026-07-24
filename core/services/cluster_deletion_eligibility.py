"""Fail-closed eligibility for physically deleting an *unused* cluster connection.

This is R4's first independently reviewable slice: a pure read that proves a
connection carries **no** operational footprint before ``Delete unused
connection`` may ever run. It performs no mutation, holds no lock and renders no
UI — the hard-delete transaction and its control are later slices that consume
this verdict.

The rule the eligibility encodes (see ``docs/cluster-retire.local.md`` →
*No general hard delete*): a connection is deletable only when it is not
retired, carries no durable ``operational_footprint_at`` marker, and no reverse
relation that blocks hard deletion holds a row. Two properties matter:

* **It fails closed on the unknown.** The sweep iterates the model's *live*
  reverse relations and blocks on any relation absent from
  ``CLUSTER_REVERSE_RELATIONS`` — a newly added relation is a gap to classify,
  not a default-allow. The row count for a blocking relation uses the base
  manager so a future default manager cannot hide rows from the safety check.
* **It cannot be recovered by waiting.** ``operational_footprint_at`` is the
  durable memory that survives every timed-retention purge, so a connection that
  once ran guests, opened consoles or was scanned stays ineligible even after
  audit/scan/console retention has emptied the relations that first recorded it.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.models import ProxmoxCluster
from core.services.audit_events import CLUSTER_CONFIGURATION_AUDIT_ACTIONS
from core.services.cluster_lifecycle_registry import CLUSTER_REVERSE_RELATIONS

# Synthetic relation names for the two blockers that are not reverse relations.
BLOCKER_RETIRED = "lifecycle.retired"
BLOCKER_FOOTPRINT = "operational_footprint"


@dataclass(frozen=True)
class DeletionBlocker:
    """One reason a connection cannot be hard-deleted.

    ``relation`` is the reverse-relation accessor, or a synthetic name for the
    retired-state and footprint-marker blockers. ``count`` is the blocking row
    count, or 1 for the state/marker blockers that are not row counts.
    """

    relation: str
    kind: str
    count: int
    detail: str


@dataclass(frozen=True)
class DeletionEligibility:
    """Whether ``Delete unused connection`` may run, and why not if it may not."""

    cluster_key: str
    eligible: bool
    blockers: tuple[DeletionBlocker, ...]

    @property
    def blocked(self) -> bool:
        return not self.eligible


def _reverse_relation_fields():
    """The model's live reverse relations, the same set the coverage test uses."""
    return [
        field
        for field in ProxmoxCluster._meta.get_fields()
        if field.is_relation and field.auto_created and not field.concrete
    ]


def unused_connection_deletion_eligibility(cluster: ProxmoxCluster) -> DeletionEligibility:
    """Prove a connection carries no operational footprint, failing closed.

    Eligible only when every check passes: the cluster is not retired, has no
    durable operational-footprint marker, and no reverse relation that blocks
    hard deletion holds a row — configuration rows (credential, trust,
    endpoints) and allowlisted configuration Audit events excepted. Any reverse
    relation not classified by ``CLUSTER_REVERSE_RELATIONS`` blocks.
    """
    blockers: list[DeletionBlocker] = []

    if cluster.retired_at is not None:
        blockers.append(
            DeletionBlocker(
                BLOCKER_RETIRED,
                "retired",
                1,
                "A retired cluster permanently reserves its key and is never hard-deleted.",
            )
        )

    if cluster.operational_footprint_at is not None:
        blockers.append(
            DeletionBlocker(
                BLOCKER_FOOTPRINT,
                "footprint_marker",
                1,
                "Durable operational footprint recorded "
                f"({cluster.operational_footprint_reason or 'unspecified'}); eligibility "
                "can never be recovered by waiting for timed retention to run.",
            )
        )

    for field in _reverse_relation_fields():
        accessor = field.get_accessor_name()
        classification = CLUSTER_REVERSE_RELATIONS.get(accessor)
        if classification is None:
            blockers.append(
                DeletionBlocker(
                    accessor,
                    "unclassified_relation",
                    0,
                    "Reverse relation is not classified by CLUSTER_REVERSE_RELATIONS; "
                    "hard deletion fails closed until it is.",
                )
            )
            continue
        if not classification.blocks_hard_delete:
            # Disposable connection configuration: deleted with the connection.
            continue
        related_model = field.field.model
        fk_name = field.field.name
        # Base manager: a future default manager must not hide a blocking row.
        queryset = related_model._base_manager.filter(**{fk_name: cluster})
        if accessor == "audit_events":
            # Configuration-only lifecycle events are detached and preserved on
            # hard delete; only operational-provider events block.
            queryset = queryset.exclude(action__in=CLUSTER_CONFIGURATION_AUDIT_ACTIONS)
        count = queryset.count()
        if count:
            blockers.append(
                DeletionBlocker(
                    accessor,
                    classification.kind.value,
                    count,
                    classification.note,
                )
            )

    return DeletionEligibility(
        cluster_key=cluster.key,
        eligible=not blockers,
        blockers=tuple(blockers),
    )

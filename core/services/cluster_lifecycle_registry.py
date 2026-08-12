"""The retirement lifecycle registry, stated as data with no behaviour attached.

Two tables live here, both finalized in the R0 executable contract
(``docs/cluster-retire.local.md``) and both consumed — not defined — by later
phases:

* :data:`CLUSTER_REVERSE_RELATIONS` classifies every reverse relation to
  ``ProxmoxCluster`` as configuration, retained/immutable history, current
  projection, storage-owned, operator safety input, a durable operation, or the
  operational (retention-purged) console kind. The whole point is *exhaustiveness*:
  a relation added to a model without a row here fails the coverage test in
  ``core.tests_cluster_retire`` rather than being silently ignored by a retirement
  finalizer or the hard-delete eligibility check. R1a attaches no behaviour to the
  ``blocks_hard_delete`` flag; R2/R3 read it.

* :data:`LIFECYCLE_PARTICIPANTS` is the ``(participant, status)`` table: for each
  durable row a retirement can meet, whether it is not-started, active,
  history or an unclassified blocker, and the abandonment code forced retirement
  records when it may abandon it. **Classification is static per pair and never
  takes elapsed time as an input** — a queued row is not-started whether it was
  queued a second or a month ago.

There is deliberately **no third table reserving future participants.** R1a shipped
one, holding a single ``standalone_host`` slot, on the assumption that Module 5
would give a standalone installation its own cluster relation. Module 5 phase 5a0B
settled the opposite: a standalone installation *is* a :class:`~core.models.ProxmoxCluster`
domain and its node is an ordinary ``NodeRef``, so no standalone-host model will
ever exist and the slot could only ever be closed, never filled. Reserving a name
for a relation that will not be added is not a safety net — it is a promise a
reader has to check against source before trusting, which is precisely the failure
mode Module 5's review passes kept finding.

The obligation that actually has teeth is unaffected: the exhaustiveness assertion
in ``core.tests_cluster_retire`` fails on any new reverse relation to
``ProxmoxCluster`` that has no row in :data:`CLUSTER_REVERSE_RELATIONS`. Module 5's
own relations (node enrollment and the membership projection,
``docs/node-enrollment.local.md`` N1) are classified in the migration that adds
them, because that test demands it — not because a slot was held open here.

**Be precise about what that does not cover**, because the deleted reservation's
docstring was not. It tied itself to :data:`LIFECYCLE_PARTICIPANTS` — "naming it
here is what keeps *a status not listed blocks both modes* honest" — and
:data:`LIFECYCLE_PARTICIPANTS` has no consumers and no completeness test at all.
Relation classification and participant-status completeness are two different
obligations, and only the first is enforced. The reservation enforced neither, so
deleting it regresses nothing; the point is that "already enforced" is true of the
relation registry and false of the participant table. R2/R3 read the participant
table when they land, and the completeness rule is prose until then.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from core.services.cluster_footprint import (
    FOOTPRINT_CONSOLE_SESSION,
    FOOTPRINT_GUEST_PROJECTION,
    FOOTPRINT_HOST_PROJECTION,
    FOOTPRINT_SCAN_OBSERVATION,
    FOOTPRINT_STORAGE_PROJECTION,
)


class RelationClass(enum.Enum):
    """How a reverse relation to ``ProxmoxCluster`` participates in retirement."""

    CONFIG = "config"  # disposable connection config; snapshot to Audit, then delete
    RETAINED_HISTORY = "retained_history"  # preserved; hard delete may detach allowlisted rows
    # Preserved; any row blocks hard delete. Currently unused, deliberately: every
    # relation that once carried it turned out to be timed-purged (scan metadata,
    # space samples) or rebuilt by the next refresh (scan inventory), which is not
    # what "immutable" claims. Kept as vocabulary for a relation that earns it.
    IMMUTABLE_HISTORY = "immutable_history"
    CURRENT_PROJECTION = "current_projection"  # removed via the inventory owner
    STORAGE_OWNED = "storage_owned"  # the storage adapter decides the transition
    OPERATOR_SAFETY_INPUT = "operator_safety_input"  # explicit resolution; never inferred
    DURABLE_OPERATION = "durable_operation"  # stop future dispatch, preserve history
    OPERATIONAL = "operational"  # retention-purged; durable signal is operational_footprint_at


class FootprintPolicy(enum.Enum):
    """How a relation participates in the durable operational-footprint marker."""

    NONE = "none"  # connection configuration; never operational use
    RECONSTRUCTIBLE = "reconstructible"  # machine-written state with an allowlisted reason
    OPERATOR = "operator"  # operator use with a permanently blocking reason
    ACTION_DEPENDENT = "action_dependent"  # Audit decides from its action registry
    RELATION_BLOCKER = "relation_blocker"  # the durable relation itself blocks hard delete


@dataclass(frozen=True)
class RelationClassification:
    """One reverse relation's static retirement classification.

    ``blocks_hard_delete`` records the R0 inventory table's "Unused hard delete"
    column as data. R1a stores it; it gains behaviour in the R4 eligibility check.
    """

    accessor: str
    kind: RelationClass
    blocks_hard_delete: bool
    footprint_policy: FootprintPolicy
    footprint_reason: str | None
    note: str


# One row per current reverse relation to ProxmoxCluster. The set is asserted
# exhaustive against Django model metadata by
# ``core.tests_cluster_retire.LifecycleParticipantContractTests``; a relation
# added without a row here fails that test rather than slipping past a finalizer.
CLUSTER_REVERSE_RELATIONS: dict[str, RelationClassification] = {
    "credential": RelationClassification(
        "credential",
        RelationClass.CONFIG,
        blocks_hard_delete=False,
        footprint_policy=FootprintPolicy.NONE,
        footprint_reason=None,
        note="CASCADE; snapshot non-secret fields to Audit, then delete.",
    ),
    "transport_trust": RelationClassification(
        "transport_trust",
        RelationClass.CONFIG,
        blocks_hard_delete=False,
        footprint_policy=FootprintPolicy.NONE,
        footprint_reason=None,
        note="CASCADE; snapshot non-secret fields to Audit, then delete.",
    ),
    "endpoints": RelationClassification(
        "endpoints",
        RelationClass.CONFIG,
        blocks_hard_delete=False,
        footprint_policy=FootprintPolicy.NONE,
        footprint_reason=None,
        note="Deleted so their URLs are freed to re-register.",
    ),
    "node_enrollments": RelationClassification(
        "node_enrollments",
        RelationClass.CONFIG,
        blocks_hard_delete=False,
        footprint_policy=FootprintPolicy.NONE,
        footprint_reason=None,
        note="Module 5 5a1J. Operator-owned publication configuration; snapshotted "
        "to Audit and removed by its lifecycle owner. It never changes Proxmox.",
    ),
    "topology_handoff_storage_bindings": RelationClassification(
        "topology_handoff_storage_bindings",
        RelationClass.CONFIG,
        blocks_hard_delete=False,
        footprint_policy=FootprintPolicy.NONE,
        footprint_reason=None,
        note="Module 5 5a1G. Operator-confirmed replacement binding intent; included "
        "in the storage retirement digest and removed by the storage lifecycle owner.",
    ),
    "audit_events": RelationClassification(
        "audit_events",
        RelationClass.RETAINED_HISTORY,
        blocks_hard_delete=True,
        footprint_policy=FootprintPolicy.ACTION_DEPENDENT,
        footprint_reason=None,
        note="Preserved on retirement. Hard delete keeps only allowlisted config "
        "events and detaches the relation; any operational event blocks.",
    ),
    "proxmox_objects": RelationClassification(
        "proxmox_objects",
        RelationClass.CURRENT_PROJECTION,
        blocks_hard_delete=False,
        footprint_policy=FootprintPolicy.RECONSTRUCTIBLE,
        footprint_reason=FOOTPRINT_SCAN_OBSERVATION,
        note="Preserved by retirement, but not a series: scan retention keeps only the "
        "latest completed scan per cluster, and the next scan rebuilds it. Hard delete "
        "removes this cluster's rows.",
    ),
    "scan_observations": RelationClassification(
        "scan_observations",
        RelationClass.OPERATIONAL,
        blocks_hard_delete=False,
        footprint_policy=FootprintPolicy.RECONSTRUCTIBLE,
        footprint_reason=FOOTPRINT_SCAN_OBSERVATION,
        note="Per-scan coverage metadata; CASCADEs with its ScanRun after "
        "SCAN_METADATA_RETENTION_DAYS, so the durable signal is operational_footprint_at. "
        "Hard delete removes this cluster's rows.",
    ),
    "storage_space_snapshots": RelationClassification(
        "storage_space_snapshots",
        RelationClass.OPERATIONAL,
        blocks_hard_delete=False,
        footprint_policy=FootprintPolicy.RECONSTRUCTIBLE,
        footprint_reason=FOOTPRINT_STORAGE_PROJECTION,
        note="Storage-space samples, purged at SPACE_SNAPSHOT_RETENTION_DAYS on every "
        "recording run, so the durable signal is operational_footprint_at. Hard delete "
        "removes this cluster's rows.",
    ),
    "current_guests": RelationClassification(
        "current_guests",
        RelationClass.CURRENT_PROJECTION,
        blocks_hard_delete=False,
        footprint_policy=FootprintPolicy.RECONSTRUCTIBLE,
        footprint_reason=FOOTPRINT_GUEST_PROJECTION,
        note="Removed via the current-inventory owner (R2 retire_cluster_guest_inventory); "
        "hard delete removes the rows. Rebuilt by the next refresh.",
    ),
    "inventory_state": RelationClassification(
        "inventory_state",
        RelationClass.CURRENT_PROJECTION,
        blocks_hard_delete=False,
        footprint_policy=FootprintPolicy.RECONSTRUCTIBLE,
        footprint_reason=FOOTPRINT_GUEST_PROJECTION,
        note="Current-inventory state row; removed via the inventory owner. Rebuilt by the next refresh.",
    ),
    "storage_catalog_state": RelationClassification(
        "storage_catalog_state",
        RelationClass.STORAGE_OWNED,
        blocks_hard_delete=False,
        footprint_policy=FootprintPolicy.RECONSTRUCTIBLE,
        footprint_reason=FOOTPRINT_STORAGE_PROJECTION,
        note="CASCADE; the storage adapter invalidates it. Publication state only, "
        "rebuilt by the next catalog refresh.",
    ),
    "storage_definitions": RelationClassification(
        "storage_definitions",
        RelationClass.STORAGE_OWNED,
        blocks_hard_delete=False,
        footprint_policy=FootprintPolicy.RECONSTRUCTIBLE,
        footprint_reason=FOOTPRINT_STORAGE_PROJECTION,
        note="Tombstoned via unmanaged_at by the storage adapter on retirement; hard "
        "delete removes them and CASCADEs their node states, mount bindings, coverages "
        "and observations. Rebuilt by the next catalog refresh.",
    ),
    "storage_consumers": RelationClassification(
        "storage_consumers",
        RelationClass.OPERATOR_SAFETY_INPUT,
        blocks_hard_delete=True,
        footprint_policy=FootprintPolicy.RELATION_BLOCKER,
        footprint_reason=None,
        note="PROTECT; explicit per-consumer resolution, never inferred. Any row blocks.",
    ),
    "scheduled_actions": RelationClassification(
        "scheduled_actions",
        RelationClass.DURABLE_OPERATION,
        blocks_hard_delete=True,
        footprint_policy=FootprintPolicy.RELATION_BLOCKER,
        footprint_reason=None,
        note="Stop future dispatch (soft-delete), preserve run history. Any row blocks.",
    ),
    "membership_state": RelationClassification(
        "membership_state",
        RelationClass.CURRENT_PROJECTION,
        blocks_hard_delete=False,
        footprint_policy=FootprintPolicy.RECONSTRUCTIBLE,
        footprint_reason=FOOTPRINT_HOST_PROJECTION,
        note="Module 5 5a1A. CASCADE; one row per cluster, rebuilt by the next membership "
        "refresh. Hard delete removes it. Holds no operator decision -- enrollment does, and "
        "that is a different table.",
    ),
    "node_states": RelationClassification(
        "node_states",
        RelationClass.CURRENT_PROJECTION,
        blocks_hard_delete=False,
        footprint_policy=FootprintPolicy.RECONSTRUCTIBLE,
        footprint_reason=FOOTPRINT_HOST_PROJECTION,
        note="Module 5 5a1A. CASCADE; the discovery/membership projection per NodeRef. "
        "Current state, not history: an absent node is marked present=False rather than "
        "deleted, but the whole set is rebuilt by the next complete generation.",
    ),
    "projection_coverage": RelationClassification(
        "projection_coverage",
        RelationClass.CURRENT_PROJECTION,
        blocks_hard_delete=False,
        footprint_policy=FootprintPolicy.RECONSTRUCTIBLE,
        footprint_reason=FOOTPRINT_HOST_PROJECTION,
        note="Module 5 5a1A. CASCADE; what the last refresh of each scope proved. Rebuilt "
        "by the next refresh and meaningless without the projections it describes.",
    ),
    "console_sessions": RelationClassification(
        "console_sessions",
        RelationClass.OPERATIONAL,
        blocks_hard_delete=True,
        footprint_policy=FootprintPolicy.OPERATOR,
        footprint_reason=FOOTPRINT_CONSOLE_SESSION,
        note="SET_NULL and retention-purged, so the durable signal is "
        "operational_footprint_at; remaining rows have provider secrets blanked. Any row blocks.",
    ),
}


class ParticipantLifecycleClass(enum.Enum):
    """Where a durable row sits when retirement meets it."""

    NOT_STARTED = "not_started"  # cancellable through its owner before it runs
    ACTIVE = "active"  # in flight; verified blocks, forced may abandon
    RETAINED_HISTORY = "retained_history"  # terminal; preserved by both modes
    BLOCKER = "blocker"  # unclassified/unknown; blocks both modes


# Abandonment codes forced retirement records; verified retirement uses the first
# only for a not-yet-started row and never abandons an active one.
CODE_RETIRED_BEFORE_START = "cluster_retired_before_start"
CODE_FORCE_RETIRED_UNRESOLVABLE = "cluster_force_retired_unresolvable"


@dataclass(frozen=True)
class ParticipantStatus:
    """One ``(participant, status)`` row of the lifecycle table."""

    participant: str
    status: str
    lifecycle_class: ParticipantLifecycleClass
    #: Abandonment code, or "" for history/soft-delete rows that are never abandoned.
    code: str
    note: str = ""


# The R0 executable contract's lifecycle-participant table, as data. A status a
# retirement can meet that is not represented here blocks both modes by the
# contract's completeness rule.
#
# That rule is currently prose, not coverage. The previous comment here credited
# "the reverse-relation coverage test" with keeping this list honest; it does not
# -- it asserts that every *relation* is classified, which is a different
# obligation, and this table has no consumers and no completeness test yet. R2/R3
# read it when they land and own making the rule executable.
LIFECYCLE_PARTICIPANTS: tuple[ParticipantStatus, ...] = (
    ParticipantStatus(
        "audit_events (provider op)", "queued", ParticipantLifecycleClass.NOT_STARTED, CODE_RETIRED_BEFORE_START
    ),
    ParticipantStatus(
        "audit_events (provider op)",
        "running",
        ParticipantLifecycleClass.ACTIVE,
        CODE_FORCE_RETIRED_UNRESOLVABLE,
        "verified blocks",
    ),
    ParticipantStatus(
        "audit_events (provider op)",
        "success|failed|failure|refused|skipped|warning|cancelled|missed",
        ParticipantLifecycleClass.RETAINED_HISTORY,
        "",
    ),
    ParticipantStatus("audit_events (config, allowlist)", "any", ParticipantLifecycleClass.RETAINED_HISTORY, ""),
    ParticipantStatus(
        "scheduled_actions",
        "deleted_at IS NULL",
        ParticipantLifecycleClass.NOT_STARTED,
        "",
        "soft-delete, stop dispatch; not abandonment",
    ),
    ParticipantStatus("scheduled_actions", "deleted_at set", ParticipantLifecycleClass.RETAINED_HISTORY, ""),
    ParticipantStatus(
        "scheduled_actions.runs", "queued|preflight", ParticipantLifecycleClass.NOT_STARTED, CODE_RETIRED_BEFORE_START
    ),
    ParticipantStatus(
        "scheduled_actions.runs",
        "submitted|polling",
        ParticipantLifecycleClass.ACTIVE,
        CODE_FORCE_RETIRED_UNRESOLVABLE,
        "verified blocks",
    ),
    ParticipantStatus(
        "scheduled_actions.runs",
        "completed|failed|skipped|missed|timeout|stale|cancelled",
        ParticipantLifecycleClass.RETAINED_HISTORY,
        "",
    ),
    ParticipantStatus("console_sessions", "pending", ParticipantLifecycleClass.NOT_STARTED, CODE_RETIRED_BEFORE_START),
    ParticipantStatus(
        "console_sessions",
        "connecting|connected",
        ParticipantLifecycleClass.ACTIVE,
        CODE_FORCE_RETIRED_UNRESOLVABLE,
        "verified blocks",
    ),
    ParticipantStatus("console_sessions", "closed|failed|expired", ParticipantLifecycleClass.RETAINED_HISTORY, ""),
    ParticipantStatus(
        "ScanRun (installation-wide)",
        "queued|running",
        ParticipantLifecycleClass.BLOCKER,
        "",
        "blocks both until scan scope is persisted narrowly",
    ),
    ParticipantStatus(
        "ScanRun (installation-wide)", "completed|failed|cancelled", ParticipantLifecycleClass.RETAINED_HISTORY, ""
    ),
    ParticipantStatus(
        "storage_consumers",
        "any",
        ParticipantLifecycleClass.ACTIVE,
        CODE_FORCE_RETIRED_UNRESOLVABLE,
        "verified blocks until each consumer resolved",
    ),
)

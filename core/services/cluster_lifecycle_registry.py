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

:data:`FUTURE_PARTICIPANTS` reserves the Module 5 standalone-host participant
slot. It has no model yet, so it cannot be a reverse relation; naming it here is
what keeps "a status not listed blocks both modes" honest once Module 5 lands.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class RelationClass(enum.Enum):
    """How a reverse relation to ``ProxmoxCluster`` participates in retirement."""

    CONFIG = "config"  # disposable connection config; snapshot to Audit, then delete
    RETAINED_HISTORY = "retained_history"  # preserved; hard delete may detach allowlisted rows
    IMMUTABLE_HISTORY = "immutable_history"  # preserved; any row blocks hard delete
    CURRENT_PROJECTION = "current_projection"  # removed via the inventory owner
    STORAGE_OWNED = "storage_owned"  # the storage adapter decides the transition
    OPERATOR_SAFETY_INPUT = "operator_safety_input"  # explicit resolution; never inferred
    DURABLE_OPERATION = "durable_operation"  # stop future dispatch, preserve history
    OPERATIONAL = "operational"  # retention-purged; durable signal is operational_footprint_at


@dataclass(frozen=True)
class RelationClassification:
    """One reverse relation's static retirement classification.

    ``blocks_hard_delete`` records the R0 inventory table's "Unused hard delete"
    column as data. R1a stores it; it gains behaviour in the R4 eligibility check.
    """

    accessor: str
    kind: RelationClass
    blocks_hard_delete: bool
    note: str


# One row per current reverse relation to ProxmoxCluster. The set is asserted
# exhaustive against Django model metadata by
# ``core.tests_cluster_retire.LifecycleParticipantContractTests``; a fifteenth
# relation added without a row here fails that test rather than slipping past a
# finalizer.
CLUSTER_REVERSE_RELATIONS: dict[str, RelationClassification] = {
    "credential": RelationClassification(
        "credential",
        RelationClass.CONFIG,
        blocks_hard_delete=False,
        note="CASCADE; snapshot non-secret fields to Audit, then delete.",
    ),
    "transport_trust": RelationClassification(
        "transport_trust",
        RelationClass.CONFIG,
        blocks_hard_delete=False,
        note="CASCADE; snapshot non-secret fields to Audit, then delete.",
    ),
    "endpoints": RelationClassification(
        "endpoints",
        RelationClass.CONFIG,
        blocks_hard_delete=False,
        note="Deleted so their URLs are freed to re-register.",
    ),
    "audit_events": RelationClassification(
        "audit_events",
        RelationClass.RETAINED_HISTORY,
        blocks_hard_delete=True,
        note="Preserved on retirement. Hard delete keeps only allowlisted config "
        "events and detaches the relation; any operational event blocks.",
    ),
    "proxmox_objects": RelationClassification(
        "proxmox_objects",
        RelationClass.IMMUTABLE_HISTORY,
        blocks_hard_delete=True,
        note="Immutable scan evidence; preserved. Any row blocks hard delete.",
    ),
    "scan_observations": RelationClassification(
        "scan_observations",
        RelationClass.IMMUTABLE_HISTORY,
        blocks_hard_delete=True,
        note="Immutable scan evidence; preserved. Any row blocks hard delete.",
    ),
    "storage_space_snapshots": RelationClassification(
        "storage_space_snapshots",
        RelationClass.IMMUTABLE_HISTORY,
        blocks_hard_delete=True,
        note="Historical storage-space samples; preserved. Any row blocks hard delete.",
    ),
    "current_guests": RelationClassification(
        "current_guests",
        RelationClass.CURRENT_PROJECTION,
        blocks_hard_delete=True,
        note="Removed via the current-inventory owner (R2 retire_cluster_guest_inventory).",
    ),
    "inventory_state": RelationClassification(
        "inventory_state",
        RelationClass.CURRENT_PROJECTION,
        blocks_hard_delete=True,
        note="Current-inventory state row; removed via the inventory owner.",
    ),
    "storage_catalog_state": RelationClassification(
        "storage_catalog_state",
        RelationClass.STORAGE_OWNED,
        blocks_hard_delete=True,
        note="CASCADE; the storage adapter invalidates it. Any published state blocks.",
    ),
    "storage_definitions": RelationClassification(
        "storage_definitions",
        RelationClass.STORAGE_OWNED,
        blocks_hard_delete=True,
        note="Tombstoned via unmanaged_at by the storage adapter. Any defined state blocks.",
    ),
    "storage_consumers": RelationClassification(
        "storage_consumers",
        RelationClass.OPERATOR_SAFETY_INPUT,
        blocks_hard_delete=True,
        note="PROTECT; explicit per-consumer resolution, never inferred. Any row blocks.",
    ),
    "scheduled_actions": RelationClassification(
        "scheduled_actions",
        RelationClass.DURABLE_OPERATION,
        blocks_hard_delete=True,
        note="Stop future dispatch (soft-delete), preserve run history. Any row blocks.",
    ),
    "console_sessions": RelationClassification(
        "console_sessions",
        RelationClass.OPERATIONAL,
        blocks_hard_delete=True,
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
# contract's completeness rule; the reverse-relation coverage test is what keeps
# this list honest as models grow.
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
        "audit_events (provider op)", "success|failure|refused|skipped", ParticipantLifecycleClass.RETAINED_HISTORY, ""
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


@dataclass(frozen=True)
class FutureParticipant:
    """A lifecycle participant whose model does not exist yet.

    It cannot appear in :data:`CLUSTER_REVERSE_RELATIONS` (no relation to
    introspect), so it is reserved here. When Module 5 lands, its relation must
    move into the reverse-relation registry and the coverage test will demand a
    classification for it.
    """

    name: str
    owning_module: str
    note: str


FUTURE_PARTICIPANTS: tuple[FutureParticipant, ...] = (
    FutureParticipant(
        name="standalone_host",
        owning_module="module5",
        note="Standalone-host identity (Module 5 phase 5a0) will add a cluster "
        "relation; until then it is a reserved participant, not a live row.",
    ),
)

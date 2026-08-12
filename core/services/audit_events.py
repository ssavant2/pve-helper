from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from core.models import AuditEvent, ProxmoxCluster
from core.services.cluster_footprint import (
    FOOTPRINT_INVENTORY_BOOTSTRAP,
    FOOTPRINT_PROVIDER_OPERATION,
    stamp_operational_footprint,
)
from core.services.cluster_lifecycle_registry import (
    CODE_FORCE_RETIRED_UNRESOLVABLE,
    CODE_RETIRED_BEFORE_START,
)
from core.services.refs import GuestRef, NodeRef
from core.services.request_metadata import client_ip

# Configuration-only events are retained during retirement and may later be
# detached by the unused-connection hard-delete path. Everything else claiming
# queued/running is operational and must be registered below or fail closed.
#
# This is the single source of truth for "cluster-attached, but connection or
# lifecycle management rather than operational footprint". It gates three things
# that must agree: which Audit events block retirement (below), which stamp
# ``operational_footprint_at`` (they must not — see ``record_audit_event``), and
# which block unused-connection hard deletion (they must not — see
# ``cluster_deletion_eligibility``). The retirement/deletion lifecycle actions
# belong here too: a *failed* verified-retirement attempt on an otherwise-unused
# connection must not brand it operational, and the final deletion event is
# preserved with its relation detached, never as a blocker.
CLUSTER_CONFIGURATION_AUDIT_ACTIONS = frozenset(
    {
        "cluster.add",
        "cluster.added",
        "cluster.credential.cutover",
        "cluster.credential.rotate",
        "cluster.credential.set",
        "cluster.credential_removed",
        "cluster.credential_rotated",
        "cluster.disabled",
        "cluster.display_name_changed",
        "cluster.enabled",
        "cluster.endpoint_added",
        "cluster.endpoint_disabled",
        "cluster.endpoint_enabled",
        "cluster.force_retired",
        "cluster.identity.reapprove",
        "cluster.identity_reapproved",
        "cluster.initial_key.set",
        "cluster.node.enrolled",
        "cluster.node.enrollment_failed",
        "cluster.node.mode_changed",
        "cluster.node.removed",
        "cluster.retired",
        "cluster.retirement_preflight_identity_mismatch",
        "cluster.retirement_refused",
        "cluster.retirement_verification_failed",
        "cluster.transport.approve",
        "cluster.trust.cutover",
        "cluster.unused_connection_deleted",
        "cluster.updated",
    }
)
# Provider work an operator asked for. Somebody pressed something, so the event is
# evidence the connection was *used*: it stamps ``provider_operation``, and its rows
# block unused-connection hard deletion for good.
CLUSTER_OPERATOR_INITIATED_AUDIT_ACTIONS = frozenset(
    {
        "cluster.host_projection.refresh",
        "storage.catalog.refresh",
        "tag.bulk_operation",
        "tag.inventory.refresh",
    }
)

# Provider work the app starts by itself, and the reconstructible footprint reason
# it stamps instead of ``provider_operation``. It is still provider work — a
# running one participates in retirement below — but it is not evidence that
# anybody *used* the connection, and every row it writes is a projection the next
# refresh rebuilds. Stamping it operator-grade would make the first inventory of a
# newly added connection permanently block ``Delete unused connection``, which is
# the exact failure that check was rescued from. Anything absent here is
# operator-grade by default; this is a deliberate exception list, not a default.
MACHINE_INITIATED_FOOTPRINT_REASONS = {
    "cluster.inventory.bootstrap": FOOTPRINT_INVENTORY_BOOTSTRAP,
}
# The same actions as a set, for the readers that only need membership. Their rows
# are excluded alongside the configuration allowlist when deciding whether an
# unused connection may be hard-deleted: an event nobody asked for is not evidence
# that anybody used the connection. Like a configuration event, it is detached and
# preserved rather than deleted, so the Audit trail still records that the app
# collected an inventory before the connection went away.
CLUSTER_MACHINE_INITIATED_AUDIT_ACTIONS = frozenset(MACHINE_INITIATED_FOOTPRINT_REASONS)

# Derived, deliberately: the two intents above are the only way into this set, so a
# new provider action cannot be registered without someone deciding which one it is.
# Getting that wrong in the silent direction — a background job classified as
# operator work — kills ``Delete unused connection`` for every connection within
# seconds of it being added, and nothing about the app looks broken afterwards.
# ``ProviderAuditActionIntentCoverageTests`` is what makes the choice unavoidable.
CLUSTER_PROVIDER_AUDIT_ACTIONS = CLUSTER_OPERATOR_INITIATED_AUDIT_ACTIONS | CLUSTER_MACHINE_INITIATED_AUDIT_ACTIONS
# Guest actions are operator-initiated by construction: every one of them is a
# mutation somebody requested against a named guest.
CLUSTER_PROVIDER_AUDIT_ACTION_PREFIXES = ("guest.",)
_RETIREMENT_ACTIVE_OUTCOMES = {"queued", "running"}
_RETIREMENT_TERMINAL_OUTCOMES = {
    "cancelled",
    "failed",
    "failure",
    "missed",
    "refused",
    "skipped",
    "success",
    "warning",
}
_RETIREMENT_DETAIL_LIMIT = 100


@dataclass(frozen=True)
class AuditRetirementParticipant:
    pk: int
    action: str
    outcome: str
    object_type: str
    object_id: str


@dataclass(frozen=True)
class AuditRetirementPreflight:
    mode: str
    queued_count: int
    running_count: int
    unknown_count: int
    participants: tuple[AuditRetirementParticipant, ...]
    participant_count: int
    participants_omitted: int

    @property
    def gate_clear(self) -> bool:
        return not self.unknown_count and (self.mode == ProxmoxCluster.RetirementMode.FORCED or not self.running_count)


@dataclass(frozen=True)
class AuditRetirementResult:
    queued_cancelled: int
    running_abandoned: int
    participants: tuple[AuditRetirementParticipant, ...]
    participant_count: int
    participants_omitted: int


class AuditRetirementBlocked(RuntimeError):
    def __init__(self, preflight: AuditRetirementPreflight):
        self.preflight = preflight
        super().__init__("Audit retirement is blocked by active or unclassified operations.")


def _retirement_mode(mode: str) -> str:
    value = str(getattr(mode, "value", mode))
    if value not in {
        ProxmoxCluster.RetirementMode.VERIFIED,
        ProxmoxCluster.RetirementMode.FORCED,
    }:
        raise ValueError(f"Unsupported retirement mode: {value!r}")
    return value


def _provider_action_query() -> Q:
    query = Q(action__in=CLUSTER_PROVIDER_AUDIT_ACTIONS)
    for prefix in CLUSTER_PROVIDER_AUDIT_ACTION_PREFIXES:
        query |= Q(action__startswith=prefix)
    return query


def _is_provider_action(action: str) -> bool:
    return action in CLUSTER_PROVIDER_AUDIT_ACTIONS or any(
        action.startswith(prefix) for prefix in CLUSTER_PROVIDER_AUDIT_ACTION_PREFIXES
    )


def _audit_retirement_preflight(
    cluster,
    *,
    mode: str,
    lock: bool,
) -> AuditRetirementPreflight:
    provider_action = _provider_action_query()
    candidates = (
        AuditEvent.objects.filter(cluster_id=cluster.pk)
        .exclude(action__in=CLUSTER_CONFIGURATION_AUDIT_ACTIONS)
        .filter(
            Q(outcome__in=_RETIREMENT_ACTIVE_OUTCOMES)
            | (provider_action & ~Q(outcome__in=_RETIREMENT_TERMINAL_OUTCOMES))
        )
    )
    if lock:
        candidates = candidates.select_for_update()
        rows = list(
            candidates.order_by("pk").values_list(
                "pk",
                "action",
                "outcome",
                "object_type",
                "object_id",
            )
        )
        participant_count = len(rows)
        queued_count = sum(
            _is_provider_action(action) and outcome == "queued"
            for _pk, action, outcome, _object_type, _object_id in rows
        )
        running_count = sum(
            _is_provider_action(action) and outcome == "running"
            for _pk, action, outcome, _object_type, _object_id in rows
        )
    else:
        counts = candidates.aggregate(
            total=Count("pk"),
            queued=Count("pk", filter=_provider_action_query() & Q(outcome="queued")),
            running=Count("pk", filter=_provider_action_query() & Q(outcome="running")),
        )
        participant_count = counts["total"]
        queued_count = counts["queued"]
        running_count = counts["running"]
        rows = list(
            candidates.order_by("pk").values_list(
                "pk",
                "action",
                "outcome",
                "object_type",
                "object_id",
            )[:_RETIREMENT_DETAIL_LIMIT]
        )
    participants = tuple(AuditRetirementParticipant(*row) for row in rows)
    unknown_count = participant_count - queued_count - running_count
    bounded = participants[:_RETIREMENT_DETAIL_LIMIT]
    return AuditRetirementPreflight(
        mode=mode,
        queued_count=queued_count,
        running_count=running_count,
        unknown_count=unknown_count,
        participants=bounded,
        participant_count=participant_count,
        participants_omitted=participant_count - len(bounded),
    )


def cluster_retirement_audit_preflight(
    cluster,
    *,
    mode: str,
) -> AuditRetirementPreflight:
    """Classify active cluster Audit operations without contacting Proxmox."""
    return _audit_retirement_preflight(cluster, mode=_retirement_mode(mode), lock=False)


@transaction.atomic
def finalize_cluster_retirement_audit_operations(
    cluster,
    *,
    mode: str,
    retired_at=None,
) -> AuditRetirementResult:
    """Terminalize registered provider operations and preserve all Audit rows."""
    mode = _retirement_mode(mode)
    retired_at = retired_at or timezone.now()
    preflight = _audit_retirement_preflight(cluster, mode=mode, lock=True)
    if not preflight.gate_clear:
        raise AuditRetirementBlocked(preflight)

    participant_ids = [item.pk for item in preflight.participants]
    if preflight.participants_omitted:
        participant_ids = list(
            AuditEvent.objects.filter(cluster_id=cluster.pk)
            .filter(_provider_action_query(), outcome__in=_RETIREMENT_ACTIVE_OUTCOMES)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
    events = list(AuditEvent.objects.filter(pk__in=participant_ids).order_by("pk"))
    queued_cancelled = 0
    running_abandoned = 0
    changed: list[AuditEvent] = []
    for event in events:
        if event.outcome == "running":
            if mode != ProxmoxCluster.RetirementMode.FORCED:
                continue
            code = CODE_FORCE_RETIRED_UNRESOLVABLE
            running_abandoned += 1
        elif event.outcome == "queued":
            code = (
                CODE_FORCE_RETIRED_UNRESOLVABLE
                if mode == ProxmoxCluster.RetirementMode.FORCED
                else CODE_RETIRED_BEFORE_START
            )
            queued_cancelled += 1
        else:
            continue
        details = dict(event.details) if isinstance(event.details, dict) else {}
        event.outcome = "cancelled"
        event.details = {
            **details,
            "stage": "cancelled",
            "retirement_code": code,
            "finished_at": retired_at.isoformat(),
        }
        changed.append(event)
    if changed:
        AuditEvent.objects.bulk_update(changed, ["outcome", "details"])
    return AuditRetirementResult(
        queued_cancelled=queued_cancelled,
        running_abandoned=running_abandoned,
        participants=preflight.participants,
        participant_count=preflight.participant_count,
        participants_omitted=preflight.participants_omitted,
    )


def audit_module_key(action: str, object_type: str = "", details: Any = None) -> str:
    """Return the persisted Audit module for one normalized event."""
    details = details if isinstance(details, dict) else {}
    action = action or ""
    object_type = object_type or ""

    if action.startswith("auth."):
        return "auth"
    if action.startswith("network.") or object_type.startswith("network"):
        return "network"
    if (
        action.startswith("vm.")
        or action.startswith("scheduled_action.")
        or object_type in {"vm", "ct", "guest", "scheduled_action", "scheduled_action_run"}
    ):
        return "vms"
    if action.startswith("cluster.") or object_type.startswith("cluster"):
        return "clusters"
    if (
        action.startswith("scan.")
        or action.startswith("file.")
        or action.startswith("trash.")
        or object_type in {"scan_run", "scan_schedule", "storage", "file"}
        or details.get("target_storage")
    ):
        return "storage"
    return "system"


def record_audit_event(
    request=None,
    *,
    user=None,
    username: str = "",
    source_ip: str | None = None,
    action: str,
    object_type: str = "",
    object_id: str = "",
    outcome: str = "success",
    details: dict | None = None,
    system_username: str = "",
    cluster: ProxmoxCluster | None = None,
    cluster_key_snapshot: str = "",
    guest_ref: GuestRef | None = None,
    node_ref: NodeRef | None = None,
) -> AuditEvent:
    """Create a normalized Audit event for an HTTP request or background actor.

    Request identity is authoritative when supplied. Workers, signals and
    management commands instead pass ``user`` and/or ``username`` explicitly.
    All callers share module classification and model-level detail
    denormalization.
    """
    details = dict(details) if isinstance(details, dict) else {}
    if guest_ref is not None:
        if node_ref is not None and guest_ref.node_ref not in {None, node_ref}:
            raise ValueError("GuestRef and NodeRef identify different current nodes.")
        node_ref = node_ref or guest_ref.node_ref
        details["guest_ref"] = guest_ref.serialize()
        object_id = guest_ref.without_node().serialize()
        cluster_key_snapshot = cluster_key_snapshot or guest_ref.cluster_key
    if node_ref is not None:
        details["node_ref"] = node_ref.serialize()
        cluster_key_snapshot = cluster_key_snapshot or node_ref.cluster_key

    if cluster is not None:
        if cluster_key_snapshot and cluster.key != cluster_key_snapshot:
            raise ValueError("Audit cluster relation and durable key snapshot disagree.")
        cluster_key_snapshot = cluster.key
    elif cluster_key_snapshot:
        cluster = ProxmoxCluster.objects.filter(key=cluster_key_snapshot).first()
    resolved_user = user
    resolved_username = str(username or "")
    resolved_source_ip = source_ip

    if request is not None:
        request_user = getattr(request, "user", None)
        if request_user is not None and getattr(request_user, "is_authenticated", False):
            resolved_user = request_user
            resolved_username = request_user.get_username()
        elif resolved_user is not None and getattr(resolved_user, "is_authenticated", False):
            if not resolved_username:
                resolved_username = resolved_user.get_username()
        else:
            resolved_user = None
            resolved_username = str(system_username or resolved_username)
        resolved_source_ip = client_ip(request)
    elif resolved_user is not None:
        if getattr(resolved_user, "is_authenticated", False) and not resolved_username:
            resolved_username = resolved_user.get_username()
        elif not getattr(resolved_user, "is_authenticated", False):
            resolved_user = None

    event = AuditEvent.objects.create(
        user=resolved_user,
        username=resolved_username,
        source_ip=resolved_source_ip,
        action=action,
        object_type=object_type,
        object_id=object_id,
        outcome=outcome,
        module=audit_module_key(action, object_type, details),
        cluster=cluster,
        cluster_key_snapshot=cluster_key_snapshot,
        details=details,
    )
    # A cluster-attached provider operation is operational footprint. Connection
    # and lifecycle events (the allowlist) are not, so retiring or reconfiguring
    # a connection never makes it look operational.
    if cluster is not None and action not in CLUSTER_CONFIGURATION_AUDIT_ACTIONS:
        stamp_operational_footprint(
            cluster,
            reason=MACHINE_INITIATED_FOOTPRINT_REASONS.get(action, FOOTPRINT_PROVIDER_OPERATION),
        )
    return event

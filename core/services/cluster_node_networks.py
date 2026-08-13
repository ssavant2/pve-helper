"""Per-node network interface publication. Module 5 phase 5a4B-i.

This is the projection behind "what can a guest NIC attach to on this node". Two
consumers used to answer that question independently, from a plain interface listing
plus a cluster-wide SDN vnet list, and both were wrong against live production in
opposite directions. `core.services.node_networks` unified the live read; this module
moves the answer off the request path entirely.

**Two reads per node, and a node's pass is atomic across both.**
``nodes/<node>/network`` is the host's interface configuration -- bonds, physical
NICs, bridge attributes -- and ``nodes/<node>/network?type=any_bridge`` is the
attachability answer, zone node-restrictions included. Neither substitutes for the
other: a realized vnet with no address is absent from the plain listing entirely, and
one with an address comes back typed ``unknown``. If either read fails, **nothing is
published for that node**. Writing the plain read's rows with ``attachable=False``
after the second read failed would publish a covered node with proven-zero bridges,
which is precisely the false-absence this phase exists to prevent, and one
``complete`` flag per node cannot represent the difference.

**The boundary narrows N5 rather than borrowing it.** Storage observes
``safety_only`` nodes because a hidden node's disk must still block a destructive
file action. A hidden node's bridge list has no such consumer, so this domain
contacts ``managed`` nodes only: cost is 2 x managed nodes per pass, and a hidden
node buys no rows that nothing may read.

**Its own lane, on purpose.** The host-projection task holds a non-blocking
per-cluster lock at a one-minute cadence and skips the whole cycle when the previous
one is still running. A third domain in that lane lets a slow network read blank
membership and node runtime -- pass-grain poisoning of exactly the kind this phase's
per-node coverage is designed to avoid.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from django.db import transaction
from django.utils import timezone

from core.models import (
    ClusterMembershipState,
    ClusterNodeInterface,
    ClusterNodeState,
    ClusterProjectionCoverage,
    ProxmoxCluster,
)
from core.services.cluster_lifecycle_lock import cluster_lifecycle_lock
from core.services.cluster_projection import stamp_cluster_projection_footprint
from core.services.cluster_resolver import client_for_endpoint
from core.services.cluster_scopes import historical_clusters
from core.services.proxmox import ProxmoxAPIError
from core.services.publication_scope import publication_scope

# Reused from 5a1C rather than re-implemented. The cluster-grain refusal *order* is
# load-bearing -- retirement deletes the endpoints and coverage a later test would
# read -- and a second copy of it here is a drift defect waiting for the two to be
# corrected separately.
from core.services.cluster_node_runtime import (  # isort: skip
    ERROR_ACQUISITION_DISABLED,
    ERROR_ACQUISITION_QUARANTINED,
    ERROR_ACQUISITION_RETIRED,
    ERROR_ENDPOINTS_EXHAUSTED,
    ERROR_NODE_ABSENT,
    ERROR_NODE_NOT_A_MEMBER,
    ERROR_NODE_OFFLINE,
    ERROR_PROVIDER,
    ERROR_TOPOLOGY_TRANSITION_PENDING,
    SweepEndpointHealth,
    _acquisition_refusal,
    _cluster_gate,
    _provider_error_code,
)

logger = logging.getLogger(__name__)

#: The node is enrolled but not `managed`, so this domain never contacts it. Distinct
#: from `node_absent_from_membership`: the node exists and is healthy, pve-helper is
#: simply not publishing its network. A consumer must read this as *unknown*.
ERROR_NODE_NOT_PUBLISHED = "node_not_published"

#: The provider answered, but with something that is not a list of interfaces. Kept
#: apart from `provider_error` because it needs a different repair.
ERROR_INVALID_PAYLOAD = "invalid_payload"

#: Columns a publish writes. Named explicitly for the same reason 5a1C names its own:
#: a bare `save()` carries whatever the in-memory snapshot held.
INTERFACE_FIELDS = (
    "interface_type",
    "attachable",
    "active",
    "autostart",
    "method",
    "address",
    "cidr",
    "gateway",
    "bridge_ports",
    "bridge_vids",
    "bridge_vlan_aware",
    "bond_mode",
    "bond_slaves",
    "comments",
    "observed_generation",
    "based_on_enrollment_generation",
    "last_seen_at",
    "present",
    "unreachable",
    "updated_at",
)


@dataclass(frozen=True)
class NodeNetworkResult:
    node_name: str
    published: bool
    error_code: str
    generation: int = 0
    interfaces: int = 0
    called_provider: bool = False


@dataclass(frozen=True)
class NodeNetworkSweepResult:
    cluster_key: str
    ran: bool
    refusal: str = ""
    results: list[NodeNetworkResult] = field(default_factory=list)
    retracted: int = 0

    @property
    def targets(self) -> int:
        return len(self.results)

    @property
    def published(self) -> int:
        return sum(1 for item in self.results if item.published)

    @property
    def failed(self) -> int:
        return sum(1 for item in self.results if not item.published)


def _text(value: Any, limit: int) -> str:
    """A provider string, truncated to its column. Absent is empty, never ``None``."""
    if value is None:
        return ""
    return str(value)[:limit]


def _flag(value: Any) -> bool | None:
    """Proxmox writes these as ``1``/``"1"``/``0``; absent stays unknown.

    The live probe found the same logical flag typed differently by row kind -- a
    vnet carries ``active`` as the string ``"1"`` and a bridge as the integer ``1``
    -- so a truthiness test on the raw value is not enough and ``bool("0")`` is
    ``True``.
    """
    if value is None or value == "":
        return None
    return str(value) not in ("0", "False", "false")


def _row_key(row: object) -> str:
    if not isinstance(row, Mapping):
        return ""
    return _text(row.get("iface"), 120)


def _read_interfaces(cluster, node_name: str, endpoints, endpoint_health) -> tuple[list, list, str]:
    """Both per-node reads, from one endpoint. Returns ``(plain, any_bridge, error)``.

    Both reads go to the *same* endpoint. Splitting them across relays would let the
    two halves describe different moments and, worse, let a node's attachability come
    from a relay that cannot see the node's own configuration.
    """
    usable = [item for item in endpoints if not endpoint_health.is_condemned(item.name)]
    if not usable:
        return [], [], ERROR_ENDPOINTS_EXHAUSTED

    path = f"nodes/{quote(node_name, safe='')}/network"
    last_code = ERROR_PROVIDER
    for endpoint in usable:
        client = client_for_endpoint(endpoint)
        try:
            plain = client.get(path)
            attachable = client.get(f"{path}?type=any_bridge")
        except ProxmoxAPIError as exc:
            code = _provider_error_code(exc)
            logger.warning(
                "Node network read failed: cluster=%s node=%s endpoint=%s error_type=%s",
                cluster.key,
                node_name,
                endpoint.name,
                exc.__class__.__name__,
                exc_info=True,
            )
            last_code = code
            continue
        endpoint_health.record_success(endpoint.name)
        if not isinstance(plain, list) or not isinstance(attachable, list):
            # The endpoint is healthy; the body is not usable. Trying the next relay
            # would ask a working endpoint the same question and get the same answer.
            return [], [], ERROR_INVALID_PAYLOAD
        return plain, attachable, ""
    return [], [], last_code


def _compose(plain: list, attachable: list) -> dict[str, dict]:
    """One row per interface name, attachability from the `any_bridge` answer.

    ``attachable`` is set from membership of the second read and from nothing else.
    Type is taken from the ``any_bridge`` answer wherever the two overlap, so a
    realized vnet is never stored as ``unknown`` -- the plain listing types it that
    way, and a consumer filtering on type is how the under-reporting bug worked.
    """
    composed: dict[str, dict] = {}
    for row in plain:
        name = _row_key(row)
        if not name:
            continue
        composed[name] = {
            "interface_type": _text(row.get("type"), 32),
            "attachable": False,
            "active": _flag(row.get("active")),
            "autostart": _flag(row.get("autostart")),
            "method": _text(row.get("method"), 32),
            "address": _text(row.get("address"), 64),
            "cidr": _text(row.get("cidr"), 64),
            "gateway": _text(row.get("gateway"), 64),
            "bridge_ports": _text(row.get("bridge_ports"), 255),
            "bridge_vids": _text(row.get("bridge_vids"), 255),
            "bridge_vlan_aware": _flag(row.get("bridge_vlan_aware")),
            "bond_mode": _text(row.get("bond_mode"), 64),
            "bond_slaves": _text(row.get("slaves"), 255),
            "comments": _text(row.get("comments"), 4096),
        }
    for row in attachable:
        name = _row_key(row)
        if not name:
            continue
        entry = composed.setdefault(
            name,
            {
                "interface_type": "",
                "attachable": False,
                "active": None,
                "autostart": None,
                "method": "",
                "address": "",
                "cidr": "",
                "gateway": "",
                "bridge_ports": "",
                "bridge_vids": "",
                "bridge_vlan_aware": None,
                "bond_mode": "",
                "bond_slaves": "",
                "comments": "",
            },
        )
        entry["attachable"] = True
        entry["interface_type"] = _text(row.get("type"), 32) or entry["interface_type"]
        if entry["active"] is None:
            entry["active"] = _flag(row.get("active"))
        if entry["bridge_vlan_aware"] is None:
            entry["bridge_vlan_aware"] = _flag(row.get("vlanaware"))
        if not entry["comments"]:
            entry["comments"] = _text(row.get("comments"), 4096)
    return composed


def _coverage_for(cluster: ProxmoxCluster, node_name: str, based_on_generation: int):
    # `based_on_generation` is supplied at creation, not afterwards:
    # `core_projection_coverage_scope` requires it non-null for this domain, so a
    # bare get_or_create would fail the insert.
    coverage, _created = ClusterProjectionCoverage.objects.select_for_update().get_or_create(
        cluster=cluster,
        domain=ClusterProjectionCoverage.DOMAIN_NODE_NETWORK,
        node_name=node_name,
        defaults={"based_on_generation": based_on_generation},
    )
    return coverage


def _record_attempt(coverage, *, when, error_code: str, based_on_generation: int) -> None:
    """Record a failed attempt without retracting the previous good answer.

    ``observed_at`` and ``generation`` keep their last authoritative values, so a
    consumer comparing a row's ``observed_generation`` against coverage sees the rows
    go non-current rather than seeing them vanish.
    """
    coverage.complete = False
    coverage.attempted_at = when
    coverage.error_code = error_code
    coverage.based_on_generation = based_on_generation
    coverage.save()


def _mark_unreachable(cluster, node_name: str, *, when) -> int:
    """Flip this node's rows to unknown. Ordered, for the deadlock reason below."""
    rows = (
        ClusterNodeInterface.objects.select_for_update()
        .filter(cluster=cluster, node_name=node_name, unreachable=False)
        .order_by("iface")
    )
    touched = 0
    for row in rows:
        row.present = False
        row.unreachable = True
        row.attachable = False
        row.last_seen_at = row.last_seen_at
        row.save(update_fields=["present", "unreachable", "attachable", "updated_at"])
        touched += 1
    return touched


def _refresh_one_node(
    cluster: ProxmoxCluster,
    node_name: str,
    *,
    endpoint_health,
    scope,
    observed_at=None,
) -> NodeNetworkResult:
    """Acquire and publish one node's interfaces, in its own transaction.

    The provider calls sit inside the transaction for 5a1C's reason: moving them out
    puts the lifecycle re-check in a different transaction than the write and
    reopens the TOCTOU window the lifecycle lock closes.
    """
    with transaction.atomic():
        with cluster_lifecycle_lock(cluster):
            locked = historical_clusters().select_for_update().get(pk=cluster.pk)
            gate = _cluster_gate(locked)
            if gate.refusal:
                return NodeNetworkResult(node_name, False, gate.refusal)

            state = ClusterMembershipState.objects.filter(cluster=locked).first()
            membership_generation = state.membership_generation if state is not None else 0
            if state is not None and state.transition_pending:
                return NodeNetworkResult(node_name, False, ERROR_TOPOLOGY_TRANSITION_PENDING)

            row = ClusterNodeState.objects.filter(cluster=locked, node_name=node_name).first()
            if row is None:
                # Zero-call, zero-row. Coverage for a name with no member row would
                # orphan a row nothing prunes before cluster retirement.
                return NodeNetworkResult(node_name, False, ERROR_NODE_NOT_A_MEMBER)

            when = observed_at or timezone.now()

            # The enrollment boundary, applied before any call. A node outside
            # `managed` is never contacted for this domain.
            if not scope.publishes(node_name):
                coverage = _coverage_for(locked, node_name, membership_generation)
                _record_attempt(
                    coverage,
                    when=when,
                    error_code=ERROR_NODE_NOT_PUBLISHED,
                    based_on_generation=membership_generation,
                )
                # A node that *was* published keeps rows that now claim to be current.
                # Retract them rather than leaving them looking authoritative.
                _mark_unreachable(locked, node_name, when=when)
                stamp_cluster_projection_footprint(locked)
                return NodeNetworkResult(node_name, False, ERROR_NODE_NOT_PUBLISHED, coverage.generation)

            if not row.present:
                coverage = _coverage_for(locked, node_name, membership_generation)
                _record_attempt(
                    coverage, when=when, error_code=ERROR_NODE_ABSENT, based_on_generation=membership_generation
                )
                _mark_unreachable(locked, node_name, when=when)
                stamp_cluster_projection_footprint(locked)
                return NodeNetworkResult(node_name, False, ERROR_NODE_ABSENT, coverage.generation)

            if gate.membership_complete and not row.online:
                # Membership is the availability oracle; the network endpoint has no
                # online field of its own. Gated on complete coverage only, so a
                # failed membership refresh does not stop the node being attempted.
                coverage = _coverage_for(locked, node_name, membership_generation)
                _record_attempt(
                    coverage, when=when, error_code=ERROR_NODE_OFFLINE, based_on_generation=membership_generation
                )
                _mark_unreachable(locked, node_name, when=when)
                stamp_cluster_projection_footprint(locked)
                return NodeNetworkResult(node_name, False, ERROR_NODE_OFFLINE, coverage.generation)

            plain, attachable, error_code = _read_interfaces(locked, node_name, gate.endpoints, endpoint_health)
            coverage = _coverage_for(locked, node_name, membership_generation)
            if error_code:
                # Both reads are one unit: the rows keep their previous values and go
                # non-current via coverage. They are *not* flipped to unreachable
                # here -- a single failed pass is not proof the node is gone, and the
                # generation comparison already tells a consumer they are stale.
                _record_attempt(coverage, when=when, error_code=error_code, based_on_generation=membership_generation)
                stamp_cluster_projection_footprint(locked)
                return NodeNetworkResult(
                    node_name,
                    False,
                    error_code,
                    coverage.generation,
                    called_provider=error_code != ERROR_ENDPOINTS_EXHAUSTED,
                )

            generation = coverage.generation + 1
            composed = _compose(plain, attachable)
            existing = {
                item.iface: item
                for item in ClusterNodeInterface.objects.select_for_update()
                .filter(cluster=locked, node_name=node_name)
                .order_by("iface")
            }
            for iface in sorted(composed):
                values = composed[iface]
                target = existing.get(iface) or ClusterNodeInterface(cluster=locked, node_name=node_name, iface=iface)
                for name, value in values.items():
                    setattr(target, name, value)
                target.observed_generation = generation
                target.based_on_enrollment_generation = scope.generation
                target.last_seen_at = when
                target.present = True
                target.unreachable = False
                if target.pk is None:
                    target.save()
                else:
                    target.save(update_fields=list(INTERFACE_FIELDS))

            # Absence is only publishable under a complete read, which this is: both
            # endpoints answered. An interface the node no longer reports is proven
            # gone, so `unreachable` stays False -- that is the whole point of the
            # two flags.
            for iface, target in existing.items():
                if iface in composed:
                    continue
                if not target.present and not target.unreachable:
                    continue
                target.present = False
                target.unreachable = False
                target.attachable = False
                target.observed_generation = generation
                target.based_on_enrollment_generation = scope.generation
                target.save(
                    update_fields=[
                        "present",
                        "unreachable",
                        "attachable",
                        "observed_generation",
                        "based_on_enrollment_generation",
                        "updated_at",
                    ]
                )

            coverage.generation = generation
            coverage.complete = True
            coverage.attempted_at = when
            coverage.observed_at = when
            coverage.error_code = ""
            coverage.based_on_generation = membership_generation
            coverage.save()
            stamp_cluster_projection_footprint(locked)
            return NodeNetworkResult(node_name, True, "", generation, len(composed), called_provider=True)


def refresh_node_network(cluster: ProxmoxCluster, node_name: str, *, observed_at=None) -> NodeNetworkResult:
    """Refresh exactly one node. Adds no gate of its own; every refusal is shared."""
    return _refresh_one_node(
        cluster,
        node_name,
        endpoint_health=SweepEndpointHealth(),
        scope=publication_scope(cluster),
        observed_at=observed_at,
    )


def _retract_departed_nodes(cluster: ProxmoxCluster, *, observed_at) -> int:
    """Stop presenting interfaces for nodes membership no longer lists.

    Without this, a node that leaves the cluster keeps rows saying ``vmbr0`` is
    attachable there, and 5a4B-ii offers a migration target that is gone. Runs under
    the lifecycle lock because `cluster_membership._publish_complete` rewrites node
    rows from an earlier snapshot, and ordered because unordered multi-row
    ``SELECT ... FOR UPDATE`` is a deadlock shape.
    """
    retracted = 0
    with transaction.atomic():
        with cluster_lifecycle_lock(cluster):
            locked = historical_clusters().select_for_update().get(pk=cluster.pk)
            if _acquisition_refusal(locked):
                return 0
            state = ClusterMembershipState.objects.filter(cluster=locked).first()
            if state is not None and state.transition_pending:
                return 0
            membership_generation = state.membership_generation if state is not None else 0

            departed = list(
                ClusterNodeState.objects.filter(cluster=locked, present=False)
                .order_by("node_name")
                .values_list("node_name", flat=True)
            )
            for node_name in departed:
                touched = _mark_unreachable(locked, node_name, when=observed_at)
                if not touched:
                    # Idempotent: a node handled by an earlier pass has no rows left
                    # to flip, so this counts newly departed nodes only.
                    continue
                coverage = _coverage_for(locked, node_name, membership_generation)
                _record_attempt(
                    coverage,
                    when=observed_at,
                    error_code=ERROR_NODE_ABSENT,
                    based_on_generation=membership_generation,
                )
                retracted += 1
            if retracted:
                stamp_cluster_projection_footprint(locked)
    return retracted


def refresh_cluster_node_networks(cluster: ProxmoxCluster, *, observed_at=None) -> NodeNetworkSweepResult:
    """Publish node interfaces for every published member of one cluster.

    Deliberately not one transaction: per-node blocks would become savepoints, so a
    worker death would roll back every node published so far, and the cluster's
    advisory lock would be held across every provider call.
    """
    with transaction.atomic():
        with cluster_lifecycle_lock(cluster):
            locked = historical_clusters().select_for_update().get(pk=cluster.pk)
            gate = _cluster_gate(locked)
            if gate.refusal:
                return NodeNetworkSweepResult(locked.key, False, gate.refusal)
            targets = list(
                ClusterNodeState.objects.filter(cluster=locked, present=True)
                .order_by("node_name")
                .values_list("node_name", flat=True)
            )
            scope = publication_scope(locked)

    when = observed_at or timezone.now()
    endpoint_health = SweepEndpointHealth()
    results = [
        _refresh_one_node(cluster, node_name, endpoint_health=endpoint_health, scope=scope, observed_at=when)
        for node_name in targets
    ]
    retracted = _retract_departed_nodes(cluster, observed_at=when)
    return NodeNetworkSweepResult(cluster.key, True, "", results, retracted)


__all__ = [
    "ERROR_ACQUISITION_DISABLED",
    "ERROR_ACQUISITION_QUARANTINED",
    "ERROR_ACQUISITION_RETIRED",
    "ERROR_INVALID_PAYLOAD",
    "ERROR_NODE_NOT_PUBLISHED",
    "NodeNetworkResult",
    "NodeNetworkSweepResult",
    "refresh_cluster_node_networks",
    "refresh_node_network",
]

"""Canonical ``nodes/<node>/status`` normalization and runtime publication.

Module 5 phase 5a1C owns this provider boundary. One independently published
scope per ``NodeRef``: a node's failure writes that node's coverage row and
nothing else, so a dead node can never blank its siblings.

This module records **provenance, not currency**. Every published row carries the
membership generation it was actually observed under, and 5a1C takes no position
on what "current" means -- freshness composition is 5a1F's, descoped by owner
decision on 2026-08-11. Nodes of one sweep may legitimately carry different
``based_on_generation`` values when 5a1B republishes mid-sweep; that is a normal
state, not a fault.

Two rules here exist because the provider lies in a specific way. An absent
metric key is *unknown*, never zero -- U1 measured Proxmox filtering metric
fields out of a permission-reduced response while leaving identity intact, so
``row.get("cpu", 0)`` publishes an idle, fully-functional-looking node that is
actually invisible. And the required-key floor refuses a response that carries
none of the fields the columns are fed from, so an empty body cannot be
published as complete coverage of a node we know nothing about.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Any
from urllib.parse import quote

import httpx
from django.db import transaction
from django.utils import timezone

from core.models import (
    ClusterMembershipState,
    ClusterNodeState,
    ClusterProjectionCoverage,
    ProxmoxCluster,
)
from core.services.cluster_lifecycle_lock import cluster_lifecycle_lock
from core.services.cluster_projection import stamp_cluster_projection_footprint
from core.services.cluster_resolver import client_for_endpoint, enabled_endpoints
from core.services.cluster_scopes import historical_clusters
from core.services.proxmox import (
    ProxmoxAPIError,
    ProxmoxInvalidResponseError,
    ProxmoxTransportError,
)

logger = logging.getLogger(__name__)

ERROR_NODE_OFFLINE = "node_offline_by_membership"
ERROR_NODE_ABSENT = "node_absent_from_membership"
ERROR_PROVIDER_UNAUTHORIZED = "provider_unauthorized"
ERROR_PROVIDER_TIMEOUT = "provider_timeout"
ERROR_INVALID_PAYLOAD = "invalid_payload"
ERROR_PROVIDER = "provider_error"

ERROR_ACQUISITION_DISABLED = "acquisition_disabled"
ERROR_ACQUISITION_QUARANTINED = "acquisition_quarantined"
ERROR_ACQUISITION_RETIRED = "acquisition_retired"
ERROR_NO_ENABLED_ENDPOINT = "no_enabled_endpoint"
ERROR_MEMBERSHIP_NOT_PUBLISHED = "membership_not_published"
ERROR_NODE_NOT_A_MEMBER = "node_not_a_member"

#: All endpoints already failed this sweep. Distinct from ``no_enabled_endpoint``,
#: which is a *configuration* fact checked once per cluster: reusing that code
#: here would tell 5a1F a cluster has no endpoint when it has one that is merely
#: unreachable, and the two need different repairs.
ERROR_ENDPOINTS_EXHAUSTED = "endpoints_exhausted"

#: Ambiguous failures **in a row** an endpoint must produce before the sweep
#: stops trying it. One is the target node's fault as readily as the relay's; a
#: run of them, uninterrupted by any successful read, is evidence about the
#: relay. A success resets the streak. See ``SweepEndpointHealth``.
CONSECUTIVE_AMBIGUOUS_FAILURES_BEFORE_CONDEMNED = 2

#: Columns this phase owns. Every write names them explicitly: a bare ``save()``
#: would carry membership columns from a stale in-memory snapshot, which is the
#: exact mechanism by which 5a1B's own full-row save can resurrect nulled runtime
#: (see ``cluster_membership._publish_complete``).
RUNTIME_FIELDS = (
    "runtime_generation",
    "cpu_usage",
    "cpu_wait",
    "cpu_model",
    "cpu_sockets",
    "cpu_cores",
    "memory_total_bytes",
    "memory_used_bytes",
    "swap_total_bytes",
    "swap_used_bytes",
    "rootfs_total_bytes",
    "rootfs_used_bytes",
    "load_average_1m",
    "load_average_5m",
    "load_average_15m",
    "uptime_seconds",
    "pve_version",
    "kernel_version",
    "current_kernel_release",
    "boot_mode",
    "secure_boot_enabled",
    "updated_at",
)

_MAX_BIGINT = 2**63 - 1
_MAX_SMALLINT = 32767


class InvalidNodeStatusPayload(ValueError):
    """A successful HTTP response was not a usable node-status snapshot."""


@dataclass(frozen=True)
class NormalizedNodeRuntime:
    """The typed subset of ``nodes/<node>/status`` this projection publishes."""

    cpu_usage: float
    cpu_wait: float | None
    cpu_model: str
    cpu_sockets: int | None
    cpu_cores: int | None
    memory_total_bytes: int
    memory_used_bytes: int
    swap_total_bytes: int | None
    swap_used_bytes: int | None
    rootfs_total_bytes: int
    rootfs_used_bytes: int
    load_average_1m: float
    load_average_5m: float
    load_average_15m: float
    uptime_seconds: int
    pve_version: str
    kernel_version: str
    current_kernel_release: str
    boot_mode: str
    secure_boot_enabled: bool | None


@dataclass(frozen=True)
class NodeRuntimeResult:
    """One node's outcome. ``complete`` is the only authority for its coverage."""

    node_name: str
    complete: bool
    error_code: str
    generation: int
    based_on_generation: int | None = None
    called_provider: bool = False


@dataclass(frozen=True)
class NodeRuntimeSweepResult:
    """One cluster's sweep.

    ``complete`` means only **the sweep was not refused** -- it says nothing about
    how the individual nodes fared, and a sweep in which every node timed out is
    still ``complete=True`` with an empty ``published``. Per-node authority lives
    in each node's coverage row, as the phase's grain requires. ``published`` and
    ``failed`` are exposed so a scheduler has something to act on without
    re-deriving it from ``nodes``.
    """

    cluster_key: str
    complete: bool
    error_code: str = ""
    nodes: tuple[NodeRuntimeResult, ...] = ()
    departed: int = 0

    @property
    def targets(self) -> int:
        return len(self.nodes)

    @property
    def published(self) -> int:
        return sum(1 for node in self.nodes if node.complete)

    @property
    def failed(self) -> int:
        return sum(1 for node in self.nodes if not node.complete)


def _truncate(value: str, limit: int) -> str:
    """Fit a display string to its column, marking that it was cut.

    Display strings truncate rather than refuse: a long CPU model must not make a
    node permanently unpublishable. Decision fields take the opposite rule below.
    """
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _required(payload: Mapping[str, Any], key: str) -> Any:
    if key not in payload:
        raise InvalidNodeStatusPayload(f"Node status is missing required key {key!r}.")
    return payload[key]


def _number(value: Any, key: str) -> float:
    # `cpu` and `wait` arrive as int 0 or float depending on load; both are valid
    # observations. `bool` is an int subclass and is not.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidNodeStatusPayload(f"Node status field {key!r} is not a number.")
    return float(value)


def _byte_count(value: Any, key: str) -> int:
    if isinstance(value, bool) or type(value) is not int or value < 0 or value > _MAX_BIGINT:
        raise InvalidNodeStatusPayload(f"Node status field {key!r} is not a byte count.")
    return value


def _small_count(value: Any, key: str) -> int:
    if isinstance(value, bool) or type(value) is not int or value < 0 or value > _MAX_SMALLINT:
        raise InvalidNodeStatusPayload(f"Node status field {key!r} is not a small count.")
    return value


def _text(value: Any, key: str) -> str:
    if not isinstance(value, str):
        raise InvalidNodeStatusPayload(f"Node status field {key!r} is not a string.")
    return value.strip()


def _mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = _required(payload, key)
    if not isinstance(value, Mapping):
        raise InvalidNodeStatusPayload(f"Node status field {key!r} is not an object.")
    return value


def _optional_number(payload: Mapping[str, Any], key: str) -> float | None:
    if key not in payload:
        return None
    return _number(payload[key], key)


def _optional_bytes(section: Mapping[str, Any], key: str, label: str) -> int | None:
    if key not in section:
        return None
    return _byte_count(section[key], label)


def _optional_small(section: Mapping[str, Any], key: str, label: str) -> int | None:
    if key not in section:
        return None
    return _small_count(section[key], label)


def _optional_text(payload: Mapping[str, Any], key: str, limit: int) -> str:
    if key not in payload:
        return ""
    return _truncate(_text(payload[key], key), limit)


def _load_average(value: Any) -> tuple[float, float, float]:
    # Live-observed trap: `loadavg` is a list of *strings*, not numbers.
    if not isinstance(value, list) or len(value) != 3:
        raise InvalidNodeStatusPayload("Node status field 'loadavg' is not a three-entry list.")
    parsed: list[float] = []
    for entry in value:
        if isinstance(entry, bool) or not isinstance(entry, (str, int, float)):
            raise InvalidNodeStatusPayload("Node status field 'loadavg' holds an unparsable entry.")
        try:
            number = float(entry)
        except ValueError as exc:
            raise InvalidNodeStatusPayload("Node status field 'loadavg' holds an unparsable entry.") from exc
        # `float("nan")` and `float("inf")` parse, and PostgreSQL's double
        # precision stores both, so nothing downstream would object -- a load
        # average of NaN would simply render. Parsable is not the property worth
        # testing; representable as a load is.
        if not isfinite(number):
            raise InvalidNodeStatusPayload("Node status field 'loadavg' holds a non-finite entry.")
        parsed.append(number)
    return parsed[0], parsed[1], parsed[2]


def _secure_boot(boot_info: Mapping[str, Any] | None) -> bool | None:
    if boot_info is None or "secureboot" not in boot_info:
        return None
    value = boot_info["secureboot"]
    if isinstance(value, bool):
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    raise InvalidNodeStatusPayload("Node status field 'boot-info.secureboot' is not binary.")


def normalize_node_status(payload: object) -> NormalizedNodeRuntime:
    """Validate and normalize one complete ``nodes/<node>/status`` body.

    The required-key floor is the point of this function. Proxmox answers a
    permission-reduced request by dropping metric fields rather than refusing, so
    a body that carries identity and no metrics must not publish as complete
    coverage: an absent key is unknown, and a floor key that is absent means the
    response cannot be trusted to describe the node at all.
    """

    if not isinstance(payload, Mapping):
        raise InvalidNodeStatusPayload("Node status payload must be an object.")

    cpuinfo = _mapping(payload, "cpuinfo")
    memory = _mapping(payload, "memory")
    rootfs = _mapping(payload, "rootfs")
    swap = _mapping(payload, "swap") if "swap" in payload else {}
    boot_info = _mapping(payload, "boot-info") if "boot-info" in payload else None

    load_1m, load_5m, load_15m = _load_average(_required(payload, "loadavg"))

    return NormalizedNodeRuntime(
        cpu_usage=_number(_required(payload, "cpu"), "cpu"),
        cpu_wait=_optional_number(payload, "wait"),
        cpu_model=_truncate(_text(_required(cpuinfo, "model"), "cpuinfo.model"), 255),
        cpu_sockets=_optional_small(cpuinfo, "sockets", "cpuinfo.sockets"),
        cpu_cores=_optional_small(cpuinfo, "cores", "cpuinfo.cores"),
        memory_total_bytes=_byte_count(_required(memory, "total"), "memory.total"),
        memory_used_bytes=_byte_count(_required(memory, "used"), "memory.used"),
        swap_total_bytes=_optional_bytes(swap, "total", "swap.total"),
        swap_used_bytes=_optional_bytes(swap, "used", "swap.used"),
        rootfs_total_bytes=_byte_count(_required(rootfs, "total"), "rootfs.total"),
        rootfs_used_bytes=_byte_count(_required(rootfs, "used"), "rootfs.used"),
        load_average_1m=load_1m,
        load_average_5m=load_5m,
        load_average_15m=load_15m,
        uptime_seconds=_byte_count(_required(payload, "uptime"), "uptime"),
        pve_version=_truncate(_text(_required(payload, "pveversion"), "pveversion"), 120),
        kernel_version=_optional_text(payload, "kversion", 255),
        current_kernel_release=_current_kernel(payload),
        boot_mode=_truncate(_text(boot_info["mode"], "boot-info.mode"), 32)
        if boot_info is not None and "mode" in boot_info
        else "",
        secure_boot_enabled=_secure_boot(boot_info),
    )


def _current_kernel(payload: Mapping[str, Any]) -> str:
    if "current-kernel" not in payload:
        return ""
    value = payload["current-kernel"]
    if isinstance(value, str):
        return _truncate(value.strip(), 255)
    if isinstance(value, Mapping):
        release = value.get("release")
        version = value.get("version")
        if isinstance(release, str):
            return _truncate(release.strip(), 255)
        if isinstance(version, str):
            return _truncate(version.strip(), 255)
    raise InvalidNodeStatusPayload("Node status field 'current-kernel' is not a release string.")


def _provider_error_code(exc: ProxmoxAPIError) -> str:
    if isinstance(exc, ProxmoxInvalidResponseError):
        return ERROR_INVALID_PAYLOAD
    if isinstance(exc, ProxmoxTransportError):
        if isinstance(exc.__cause__, httpx.TimeoutException):
            return ERROR_PROVIDER_TIMEOUT
        return ERROR_PROVIDER
    if exc.status_code in (401, 403):
        return ERROR_PROVIDER_UNAUTHORIZED
    cause = exc.__cause__
    if isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code in (401, 403):
        return ERROR_PROVIDER_UNAUTHORIZED
    return ERROR_PROVIDER


@dataclass(frozen=True)
class _ClusterGate:
    """The cluster-grain answer both entry points must give identically.

    ``refusal`` is a zero-call, zero-row code; when it is empty the caller may
    proceed to the node. ``membership_complete`` is carried out of the same
    coverage read that decided ``membership_not_published``, because the offline
    skip needs it and a second query would ask the same row the same question.
    """

    endpoints: list
    membership_complete: bool
    refusal: str


def _cluster_gate(locked: ProxmoxCluster) -> _ClusterGate:
    """Every cluster-grain refusal, in one place, in one order.

    Both entry points call this under the lifecycle lock so they cannot answer
    differently. They did: the per-node seam tested membership *before* the
    lifecycle refusal and outside the lock, and retirement deletes a cluster's
    coverage rows (``cluster_projection.retire_cluster_projection``), so a
    retired cluster answered ``membership_not_published`` on the seam and
    ``acquisition_retired`` on the sweep -- telling an operator to publish
    membership for a connection whose projection is finalized.

    Order is load-bearing. The lifecycle refusal is first because retirement
    deletes both the endpoints and the coverage this function would otherwise
    read, so either later test would describe a finalized cluster as a
    misconfigured one.

    Membership publication is tested on ``observed_at`` alone, deliberately
    **not** on ``complete=True`` as the entry contract first wrote it. The
    coverage row is mutated in place, and ``cluster_membership._publish_incomplete``
    clears ``complete`` while preserving the prior authoritative ``observed_at``.
    Requiring both would let one failed membership refresh retract a whole
    cluster's runtime acquisition. This also matches what 5a1B's own
    ``_accepted_members`` tests, which is ``observed_at`` and not ``complete``.
    """
    refusal = _acquisition_refusal(locked)
    if refusal:
        return _ClusterGate([], False, refusal)

    endpoints = enabled_endpoints(locked)
    if not endpoints:
        return _ClusterGate([], False, ERROR_NO_ENABLED_ENDPOINT)

    coverage = (
        ClusterProjectionCoverage.objects.filter(
            cluster=locked,
            domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP,
            node_name__isnull=True,
        )
        .only("complete", "observed_at")
        .first()
    )
    if coverage is None or coverage.observed_at is None:
        return _ClusterGate([], False, ERROR_MEMBERSHIP_NOT_PUBLISHED)

    return _ClusterGate(endpoints, coverage.complete, "")


def _coverage_for(cluster: ProxmoxCluster, node_name: str, based_on_generation: int):
    # `based_on_generation` is supplied at creation, not assigned afterwards:
    # `core_projection_coverage_scope` requires it non-null for this domain, so a
    # bare get_or_create would fail the insert before the caller could set it.
    coverage, _created = ClusterProjectionCoverage.objects.select_for_update().get_or_create(
        cluster=cluster,
        domain=ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME,
        node_name=node_name,
        defaults={"based_on_generation": based_on_generation},
    )
    return coverage


def _clear_runtime(row: ClusterNodeState) -> None:
    """Blank every runtime column, leaving membership columns untouched."""
    row.cpu_usage = None
    row.cpu_wait = None
    row.cpu_model = ""
    row.cpu_sockets = None
    row.cpu_cores = None
    row.memory_total_bytes = None
    row.memory_used_bytes = None
    row.swap_total_bytes = None
    row.swap_used_bytes = None
    row.rootfs_total_bytes = None
    row.rootfs_used_bytes = None
    row.load_average_1m = None
    row.load_average_5m = None
    row.load_average_15m = None
    row.uptime_seconds = None
    row.pve_version = ""
    row.kernel_version = ""
    row.current_kernel_release = ""
    row.boot_mode = ""
    row.secure_boot_enabled = None


def _apply_runtime(row: ClusterNodeState, runtime: NormalizedNodeRuntime, generation: int) -> None:
    row.runtime_generation = generation
    row.cpu_usage = runtime.cpu_usage
    row.cpu_wait = runtime.cpu_wait
    row.cpu_model = runtime.cpu_model
    row.cpu_sockets = runtime.cpu_sockets
    row.cpu_cores = runtime.cpu_cores
    row.memory_total_bytes = runtime.memory_total_bytes
    row.memory_used_bytes = runtime.memory_used_bytes
    row.swap_total_bytes = runtime.swap_total_bytes
    row.swap_used_bytes = runtime.swap_used_bytes
    row.rootfs_total_bytes = runtime.rootfs_total_bytes
    row.rootfs_used_bytes = runtime.rootfs_used_bytes
    row.load_average_1m = runtime.load_average_1m
    row.load_average_5m = runtime.load_average_5m
    row.load_average_15m = runtime.load_average_15m
    row.uptime_seconds = runtime.uptime_seconds
    row.pve_version = runtime.pve_version
    row.kernel_version = runtime.kernel_version
    row.current_kernel_release = runtime.current_kernel_release
    row.boot_mode = runtime.boot_mode
    row.secure_boot_enabled = runtime.secure_boot_enabled


def _acquisition_refusal(cluster: ProxmoxCluster) -> str:
    if cluster.retired_at is not None:
        return ERROR_ACQUISITION_RETIRED
    if not cluster.enabled:
        return ERROR_ACQUISITION_DISABLED
    if cluster.ingestion_quarantined:
        return ERROR_ACQUISITION_QUARANTINED
    return ""


class SweepEndpointHealth:
    """Per-sweep endpoint condemnation. One instance per sweep, never shared.

    The resolver orders endpoints on ``last_health_status``, which only the scan
    health task ever writes, so a transport that dies at the start of a sweep
    keeps its healthy rank for every remaining node. Without condemnation one
    dead endpoint costs a full client timeout per node instead of once per sweep.

    Condemnation is not one rule but two, because ``nodes/<node>/status`` is
    *proxied*: one member answers for every node, so a failure has two possible
    owners and the client's ``request_sent`` is what separates them.

    - **Proven endpoint-wide** -- a 401/403 (the credential is rejected for every
      node) or a transport failure with ``request_sent=False`` (a connect error
      or pool timeout: the endpoint was never reached). Condemned on sight.
    - **Ambiguous** -- a transport failure that may have been delivered, such as
      a read timeout. It is attributable to the *target node* just as readily as
      to the relay, so condemning on the first one starves every later sibling
      with ``endpoints_exhausted`` and zero calls. Condemned only after
      ``CONSECUTIVE_AMBIGUOUS_FAILURES_BEFORE_CONDEMNED`` failures **in a row**,
      because a run of them is a claim about the relay while an isolated one is a
      claim about the node -- and never when it is the last usable endpoint,
      because ambiguous evidence must not end the sweep.

    **Evidence runs both ways, and the first draft only ran it one.** A
    successful read clears the endpoint's ambiguous streak. Without that, nine
    proven-good answers in one sweep were discarded and the tenth hung node
    condemned an endpoint that had just answered nine times -- and at ``E = 1``
    two unlucky nodes starved every later sibling, deterministically, in the same
    order every cycle. Counting *distinct* nodes rather than a streak did not
    help: each endpoint is tried at most once per node, so distinctness was
    structurally identical to a raw count and carried none of the argument the
    docstring was making with it.
    """

    def __init__(self):
        self._condemned: set[str] = set()
        self._ambiguous: dict[str, int] = {}

    def is_condemned(self, endpoint_name: str) -> bool:
        return endpoint_name in self._condemned

    def condemn(self, endpoint_name: str) -> None:
        self._condemned.add(endpoint_name)

    def record_success(self, endpoint_name: str) -> None:
        """The endpoint answered. Its ambiguous streak is over."""
        self._ambiguous.pop(endpoint_name, None)

    def record_ambiguous(self, endpoint_name: str, *, usable_count: int) -> None:
        """Record an ambiguous failure; condemn only if an alternative survives.

        ``usable_count`` is how many endpoints this node still had to choose
        from. **Condemning the last one is never right**, because the evidence is
        ambiguous by definition: at ``E = 1`` it converts a slow sweep into no
        data at all plus a false diagnosis, marking nodes that would have
        answered as unreachable. Letting each node pay its own timeout is exactly
        the ``(N − F) × 15s`` bound the contract states and 5a1D sizes against.

        A *proven* endpoint-wide failure is different and is condemned even when
        it is the last one: there, skipping is both faster and correct.
        """
        streak = self._ambiguous.get(endpoint_name, 0) + 1
        self._ambiguous[endpoint_name] = streak
        if streak >= CONSECUTIVE_AMBIGUOUS_FAILURES_BEFORE_CONDEMNED and usable_count > 1:
            self._condemned.add(endpoint_name)


def _read_node_status(
    cluster: ProxmoxCluster,
    node_name: str,
    endpoints: list,
    endpoint_health: SweepEndpointHealth,
) -> tuple[NormalizedNodeRuntime | None, str]:
    """Try each usable endpoint once. Returns the runtime or a stable code."""
    usable = [item for item in endpoints if not endpoint_health.is_condemned(item.name)]
    if not usable:
        # The caller proved the cluster has endpoints, so reaching here means every
        # one of them already failed this sweep: unreachable, not unconfigured.
        return None, ERROR_ENDPOINTS_EXHAUSTED

    last_code = ERROR_PROVIDER
    for endpoint in usable:
        client = client_for_endpoint(endpoint)
        try:
            payload = client.get(f"nodes/{quote(node_name, safe='')}/status")
            # Proof the endpoint answers. Without it the streak below only ever
            # grows, and a run of unrelated hung nodes condemns a healthy relay.
            endpoint_health.record_success(endpoint.name)
            return normalize_node_status(payload), ""
        except InvalidNodeStatusPayload:
            # Node-specific: this endpoint answered fine, the body was unusable.
            logger.warning(
                "Invalid node status: cluster=%s node=%s endpoint=%s",
                cluster.key,
                node_name,
                endpoint.name,
                exc_info=True,
            )
            return None, ERROR_INVALID_PAYLOAD
        except ProxmoxAPIError as exc:
            code = _provider_error_code(exc)
            logger.warning(
                "Node status read failed: cluster=%s node=%s endpoint=%s error_type=%s",
                cluster.key,
                node_name,
                endpoint.name,
                exc.__class__.__name__,
                exc_info=True,
            )
            # See `SweepEndpointHealth` for why this splits three ways rather
            # than two. An HTTP status about one node condemns nothing: a 500
            # while reading pve2 says nothing about this endpoint's ability to
            # answer for pve3.
            if code == ERROR_PROVIDER_UNAUTHORIZED or (isinstance(exc, ProxmoxTransportError) and not exc.request_sent):
                endpoint_health.condemn(endpoint.name)
            elif isinstance(exc, ProxmoxTransportError):
                endpoint_health.record_ambiguous(endpoint.name, usable_count=len(usable))
            last_code = code
    return None, last_code


def _refresh_one_node(
    cluster: ProxmoxCluster,
    node_name: str,
    *,
    endpoint_health: SweepEndpointHealth,
    observed_at=None,
) -> NodeRuntimeResult:
    """Acquire and publish one node, in its own transaction under the lock.

    The provider call sits inside this transaction on purpose: the alternative
    puts the lifecycle re-check in a different transaction than the write and
    restores the TOCTOU window the lifecycle lock exists to close. The accepted
    cost is that a retirement can wait one client timeout per node.
    """
    with transaction.atomic():
        with cluster_lifecycle_lock(cluster):
            locked = historical_clusters().select_for_update().get(pk=cluster.pk)
            # Every cluster-grain refusal, resolved once, in the order both entry
            # points share. All of them are zero-call and zero-row.
            gate = _cluster_gate(locked)
            if gate.refusal:
                return NodeRuntimeResult(node_name, False, gate.refusal, 0)

            # Lock the node row *before* reading the generation it will be bound
            # to. The lifecycle lock already serializes this against 5a1B, so the
            # order cannot matter today; it matters if that ever changes, because
            # the reverse order records the generation before the one that proved
            # the node's absence -- the record would then read as stale against
            # the very membership that justified it.
            row = ClusterNodeState.objects.select_for_update().filter(cluster=locked, node_name=node_name).first()

            state = ClusterMembershipState.objects.filter(cluster=locked).first()
            membership_generation = state.membership_generation if state is not None else 0
            membership_complete = gate.membership_complete

            if row is None:
                # Zero-call, zero-row: coverage for a NodeRef with no member row
                # would orphan a row nothing prunes before cluster retirement.
                return NodeRuntimeResult(node_name, False, ERROR_NODE_NOT_A_MEMBER, 0)

            when = observed_at or timezone.now()

            # The target list was read before this transaction opened, so 5a1B may
            # have dropped this node since. Publishing runtime bound to a
            # generation that says it is not a member would be a false provenance
            # claim -- the one property this phase still promises.
            # `membership_generation` is always the last *complete* one:
            # `_publish_incomplete` updates coverage without advancing it. That is
            # what makes both absence writes below genuinely "the generation that
            # proved the absence" without an explicit completeness check here. A
            # change to that rule in 5a1B breaks this silently.
            if not row.present:
                # The columns are nulled; the generation is deliberately NOT
                # reset. `coverage.generation` keeps its last published value, so
                # resetting here would make a returning node publish generation 1
                # after 7 -- backwards, while membership's own generation is
                # strictly increasing. 5a1F would reasonably assume the same of
                # runtime and be wrong.
                _clear_runtime(row)
                row.save(update_fields=list(RUNTIME_FIELDS))
                coverage = _coverage_for(locked, node_name, membership_generation)
                coverage.complete = False
                coverage.attempted_at = when
                coverage.error_code = ERROR_NODE_ABSENT
                coverage.based_on_generation = membership_generation
                coverage.save()
                stamp_cluster_projection_footprint(locked)
                return NodeRuntimeResult(
                    node_name, False, ERROR_NODE_ABSENT, coverage.generation, membership_generation
                )

            # Offline skip: membership is the availability oracle, node status
            # carries no online field of its own. Gated on complete coverage only
            # -- a failed membership refresh flips it and the node is attempted.
            if membership_complete and not row.online:
                coverage = _coverage_for(locked, node_name, membership_generation)
                coverage.complete = False
                coverage.attempted_at = when
                coverage.error_code = ERROR_NODE_OFFLINE
                coverage.based_on_generation = membership_generation
                coverage.save()
                stamp_cluster_projection_footprint(locked)
                return NodeRuntimeResult(
                    node_name, False, ERROR_NODE_OFFLINE, coverage.generation, membership_generation
                )

            runtime, error_code = _read_node_status(locked, node_name, gate.endpoints, endpoint_health)
            coverage = _coverage_for(locked, node_name, membership_generation)
            if runtime is None:
                # Previous-good payload and runtime_generation are preserved; only
                # the attempt is recorded, and observed_at keeps its last
                # authoritative value.
                coverage.complete = False
                coverage.attempted_at = when
                coverage.error_code = error_code
                coverage.based_on_generation = membership_generation
                coverage.save()
                stamp_cluster_projection_footprint(locked)
                return NodeRuntimeResult(
                    node_name,
                    False,
                    error_code,
                    coverage.generation,
                    membership_generation,
                    # Exhausted endpoints means no call was made for this node.
                    called_provider=error_code != ERROR_ENDPOINTS_EXHAUSTED,
                )

            generation = row.runtime_generation + 1
            _apply_runtime(row, runtime, generation)
            row.save(update_fields=list(RUNTIME_FIELDS))

            coverage.generation = generation
            coverage.complete = True
            coverage.attempted_at = when
            coverage.observed_at = when
            coverage.error_code = ""
            coverage.based_on_generation = membership_generation
            coverage.save()
            stamp_cluster_projection_footprint(locked)
            return NodeRuntimeResult(node_name, True, "", generation, membership_generation, called_provider=True)


def refresh_node_runtime(cluster: ProxmoxCluster, node_name: str, *, observed_at=None) -> NodeRuntimeResult:
    """Refresh exactly one node. The seam 5a1E's manual refresh will use.

    Applies every rule the sweep does. It differs only in taking its target by
    name, and in refusing an unknown name with a zero-call, zero-row
    ``node_not_a_member`` -- writing coverage for a NodeRef that has no member row
    would orphan a row nothing prunes before cluster retirement.

    It adds no gate of its own. Every cluster-grain refusal is ``_cluster_gate``'s,
    reached through the same locked path the sweep uses, because a check placed
    here would sit outside the lock and ahead of the lifecycle test -- which is
    exactly how this seam came to answer ``membership_not_published`` for a
    retired cluster.
    """
    return _refresh_one_node(cluster, node_name, endpoint_health=SweepEndpointHealth(), observed_at=observed_at)


def _mark_departed_nodes(cluster: ProxmoxCluster, *, observed_at) -> int:
    """Stop presenting runtime for nodes membership no longer lists.

    Runs after the node loop, and inside the lifecycle lock for a concrete
    reason: ``cluster_membership._publish_complete`` rewrites every node row with
    an argument-less ``save()`` from a snapshot taken earlier in its transaction,
    so an unlocked pass would have its nulled columns restored.

    The predicate is idempotent -- a row it has already handled fails it on the
    next sweep -- so this counts *newly* departed nodes only.
    """
    departed = 0
    with transaction.atomic():
        with cluster_lifecycle_lock(cluster):
            locked = historical_clusters().select_for_update().get(pk=cluster.pk)
            # Same refusal the node loop applies: a cluster retired, disabled or
            # quarantined between the last node and this pass must have no row
            # written. Retirement deletes the projection under this same lock, but
            # nothing deletes rows for a disabled or quarantined one.
            if _acquisition_refusal(locked):
                return 0
            state = ClusterMembershipState.objects.filter(cluster=locked).first()
            membership_generation = state.membership_generation if state is not None else 0

            handled = set(
                ClusterProjectionCoverage.objects.filter(
                    cluster=locked,
                    domain=ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME,
                    error_code=ERROR_NODE_ABSENT,
                    complete=False,
                ).values_list("node_name", flat=True)
            )
            # Ordered because unordered multi-row SELECT ... FOR UPDATE is a deadlock
            # shape. The cluster advisory lock serializes every writer of these
            # rows today, so the order is what keeps that from being the only
            # thing standing between this pass and a deadlock.
            rows = (
                ClusterNodeState.objects.select_for_update().filter(cluster=locked, present=False).order_by("node_name")
            )
            for row in rows:
                if row.node_name in handled:
                    continue
                _clear_runtime(row)
                row.save(update_fields=list(RUNTIME_FIELDS))
                coverage = _coverage_for(locked, row.node_name, membership_generation)
                coverage.complete = False
                coverage.attempted_at = observed_at
                coverage.error_code = ERROR_NODE_ABSENT
                coverage.based_on_generation = membership_generation
                coverage.save()
                departed += 1
            if departed:
                stamp_cluster_projection_footprint(locked)
    return departed


def refresh_cluster_node_runtime(cluster: ProxmoxCluster, *, observed_at=None) -> NodeRuntimeSweepResult:
    """Acquire and publish node runtime for every member of one cluster.

    Each node publishes independently: one node's timeout neither advances its
    own generation nor touches a sibling's row. The sweep is deliberately not
    wrapped in one transaction -- that would make the per-node blocks savepoints,
    so a worker death would roll back every published node, and it would hold the
    cluster's advisory lock across every provider call.
    """
    with transaction.atomic():
        with cluster_lifecycle_lock(cluster):
            locked = historical_clusters().select_for_update().get(pk=cluster.pk)
            gate = _cluster_gate(locked)
            if gate.refusal:
                return NodeRuntimeSweepResult(locked.key, False, gate.refusal)
            targets = list(
                ClusterNodeState.objects.filter(cluster=locked, present=True)
                .order_by("node_name")
                .values_list("node_name", flat=True)
            )

    when = observed_at or timezone.now()
    endpoint_health = SweepEndpointHealth()
    results = [
        _refresh_one_node(cluster, node_name, endpoint_health=endpoint_health, observed_at=when)
        for node_name in targets
    ]
    departed = _mark_departed_nodes(cluster, observed_at=when)
    return NodeRuntimeSweepResult(cluster.key, True, "", tuple(results), departed)

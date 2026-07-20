"""Authoritative, cluster-qualified storage read model.

All broad `/storage`, `nodes/<node>/storage` and `/content` reads belong here.
HTTP views consume the published projection and never fan out to Proxmox merely to
render a page.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import quote

from django.db import connection, models, transaction
from django.utils import timezone

from core.models import (
    ClusterStorage,
    ClusterStorageNodeState,
    ClusterStorageVolumeCoverage,
    ClusterStorageVolumeObservation,
    CurrentGuestInventory,
    FileInventory,
    ProxmoxCluster,
    StorageCatalogState,
    StorageMount,
)
from core.services.classification import ClassificationResult
from core.services.cluster_resolver import ClusterResolutionError, cluster_clients
from core.services.cluster_state_identity import cluster_advisory_lock_id
from core.services.proxmox import ProxmoxAPIError
from core.services.storage_backends import ContentListMode, backend_profile
from core.services.storage_mounts import backend_identity_from_definition, mount_health, scope_conflict

logger = logging.getLogger(__name__)
# A shared definition's volumes exist once, not once per node that can see them.
# The empty node is that single logical scope; node-local instances keep their
# own node name.
SHARED_OBSERVATION_NODE = ""
_METADATA_LOCK_BASE = 0x50564553544D01
_VOLUME_LOCK_BASE = 0x50564553545601


class StorageCatalogError(RuntimeError):
    pass


class UsageState(StrEnum):
    REFERENCED = "referenced"
    UNREFERENCED = "unreferenced"
    UNKNOWN = "unknown"
    REFERENCED_ELSEWHERE = "referenced-elsewhere"


@dataclass(frozen=True)
class StorageCapabilities:
    can_list_volumes: bool
    list_volumes_reason: str
    can_browse_files: bool
    browse_files_reason: str
    can_write_files: bool
    write_files_reason: str


@dataclass(frozen=True)
class CatalogVolume:
    node: str
    volid: str
    vmid: int | None
    content: str
    volume_format: str
    size_bytes: int | None
    used_bytes: int | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class VolumeScope:
    """A published observation set a view's volumes may be read from.

    `node` is the empty string for a shared scope, whose observations live under
    the single logical `SHARED_OBSERVATION_NODE`.
    """

    node: str
    generation: uuid.UUID


@dataclass(frozen=True)
class StorageView:
    """What a storage *is and can do* — deliberately without its volumes.

    Materializing an observation row per volume is the expensive part, and most
    callers (the datastore listing, every usage preflight) never read one. They
    would pay for a tuple they discard, once per definition on a page. The two
    callers that do want volumes pass this view to `storage_volumes`, which
    carries `volume_scopes` for exactly that purpose.
    """

    definition: ClusterStorage
    nodes: tuple[ClusterStorageNodeState, ...]
    volume_scopes: tuple[VolumeScope, ...]
    # The host mount pve-helper reads this scope's files through, or None. Carried
    # here because deciding it is exactly what `capabilities.can_browse_files`
    # already did; a caller that needs the mount would otherwise redo the binding
    # lookup and could reach a different answer than the capability it trusts.
    mount: StorageMount | None
    capabilities: StorageCapabilities
    metadata_stale: bool
    volumes_stale: bool
    coverage_complete: bool
    coverage_reason: str
    coverage_token: str


@dataclass(frozen=True)
class UsagePreflight:
    state: UsageState
    reason: str
    token: str
    references: tuple[str, ...] = ()

    @property
    def permits_destructive_action(self) -> bool:
        return self.state is UsageState.UNREFERENCED


@dataclass(frozen=True)
class _GuestReferences:
    """Guest labels indexed by the volume they reference, for one storage."""

    by_volid: dict[str, set[str]] = field(default_factory=dict)
    any_volume: set[str] = field(default_factory=set)

    def matching(self, volid: str) -> set[str]:
        # An empty volid asks "is anything on this storage referenced?", which is
        # how the destructive-action gate asks about a whole storage.
        return self.by_volid.get(volid, set()) if volid else self.any_volume


@dataclass(frozen=True)
class _CandidateScope:
    """One other cluster's instance of the same physical backend."""

    cluster_key: str
    storage_id: str = ""
    incomplete: bool = False
    references: _GuestReferences = field(default_factory=_GuestReferences)


@dataclass(frozen=True)
class _UsageScope:
    token: str
    unknown_reason: str = ""
    references: _GuestReferences = field(default_factory=_GuestReferences)
    candidate_reason: str = ""
    candidates: tuple[_CandidateScope, ...] = ()


def _list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _bool(value: Any, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _public_error(exc: Exception) -> str:
    if isinstance(exc, ClusterResolutionError):
        return "Cluster connection is unavailable."
    if isinstance(exc, ProxmoxAPIError):
        return "Proxmox storage inventory is unavailable."
    return "Storage inventory refresh failed."


def _clients(cluster: ProxmoxCluster):
    clients = cluster_clients(cluster)
    if not clients:
        raise StorageCatalogError("Cluster connection is unavailable.")
    return clients


def _get_with_failover(clients: Iterable, path: str):
    last_error: Exception | None = None
    for client in clients:
        try:
            return client.get(path)
        except ProxmoxAPIError as exc:
            last_error = exc
    if last_error is None:
        raise StorageCatalogError("Cluster connection is unavailable.")
    raise last_error


def _try_advisory_xact_lock(cluster: ProxmoxCluster, lane: str) -> bool:
    if connection.vendor != "postgresql":
        return True
    base = _METADATA_LOCK_BASE if lane == "metadata" else _VOLUME_LOCK_BASE
    lock_id = cluster_advisory_lock_id(base, cluster)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [lock_id])
        return bool(cursor.fetchone()[0])


def _node_inventory(
    clients, nodes: list[dict[str, Any]], *, cluster_key: str = ""
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    answers: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    for row in nodes:
        node = str(row.get("node") or "")
        if not node:
            continue
        # An offline member contributes explicit inactive evidence from /nodes;
        # asking it for storage would turn expected maintenance into an error.
        if str(row.get("status") or "").lower() not in {"online", ""}:
            answers[node] = []
            continue
        try:
            value = _get_with_failover(clients, f"nodes/{quote(node, safe='')}/storage")
            if not isinstance(value, list):
                raise StorageCatalogError("Invalid node storage inventory.")
            answers[node] = [item for item in value if isinstance(item, dict)]
        except Exception as exc:  # preserve the last complete generation
            logger.warning(
                "Node storage inventory failed for cluster=%s node=%s",
                cluster_key,
                node,
                exc_info=True,
            )
            errors[node] = _public_error(exc)
    return answers, errors


def _canonical_config(config: Any) -> dict[str, Any]:
    """Config with order-volatile values normalized.

    Proxmox returns `content` as a comma-separated list whose order varies
    between responses for the same unchanged storage. Comparing it verbatim made
    every metadata refresh look like a semantic change, which invalidated volume
    coverage and forced a full republish on every cycle.
    """
    raw = dict(config or {})
    if "content" in raw:
        raw["content"] = ",".join(sorted(set(_list(raw.get("content")))))
    return raw


def _metadata_semantics(cluster: ProxmoxCluster) -> dict[str, tuple[Any, ...]]:
    """Return the storage semantics that can invalidate volume absence proof.

    Capacity and observation timestamps intentionally do not participate: they
    change frequently without changing which storage instances or volumes exist.
    """
    definitions = ClusterStorage.objects.filter(cluster=cluster).prefetch_related("node_states").order_by("storage_id")
    return {
        definition.storage_id: (
            definition.storage_id,
            definition.storage_type,
            tuple(definition.content or ()),
            definition.shared,
            tuple(definition.nodes or ()),
            definition.disabled,
            definition.present,
            json.dumps(_canonical_config(definition.config), sort_keys=True, separators=(",", ":")),
            # Sorted here rather than by `.order_by()` (which would defeat the
            # prefetch above) and rather than by the model's Meta ordering (which
            # exists for other reasons and could be changed for them). This tuple
            # decides whether volume coverage is invalidated: a reordering that
            # arrives from somewhere else would republish the whole catalog every
            # cycle and discard every absence proof with it.
            tuple(
                (state.node, state.present, state.active, state.enabled)
                for state in sorted(definition.node_states.all(), key=lambda state: state.node)
            ),
        )
        for definition in definitions
    }


def refresh_storage_metadata(cluster: ProxmoxCluster) -> StorageCatalogState:
    with transaction.atomic():
        if not _try_advisory_xact_lock(cluster, "metadata"):
            return StorageCatalogState.objects.get_or_create(cluster=cluster)[0]
        return _refresh_storage_metadata_locked(cluster)


def _refresh_storage_metadata_locked(cluster: ProxmoxCluster) -> StorageCatalogState:
    attempted_at = timezone.now()
    try:
        clients = _clients(cluster)
        definitions = _get_with_failover(clients, "storage")
        nodes = _get_with_failover(clients, "nodes")
        if not isinstance(definitions, list) or not isinstance(nodes, list):
            raise StorageCatalogError("Invalid storage metadata response.")
        definitions = [item for item in definitions if isinstance(item, dict) and item.get("storage")]
        nodes = [item for item in nodes if isinstance(item, dict) and item.get("node")]
        node_answers, errors = _node_inventory(clients, nodes, cluster_key=cluster.key)
        if errors:
            raise StorageCatalogError("Incomplete node storage inventory.")
    except Exception as exc:
        logger.exception("Storage metadata refresh failed for cluster=%s", cluster.key)
        state, _ = StorageCatalogState.objects.get_or_create(cluster=cluster)
        state.metadata_last_attempt_at = attempted_at
        state.metadata_complete = False
        state.metadata_errors = {"refresh": _public_error(exc)}
        state.save(update_fields=["metadata_last_attempt_at", "metadata_complete", "metadata_errors", "updated_at"])
        return state

    generation = uuid.uuid4()
    observed_at = timezone.now()
    node_online = {str(row["node"]): str(row.get("status") or "").lower() in {"online", ""} for row in nodes}
    with transaction.atomic():
        state, _ = StorageCatalogState.objects.select_for_update().get_or_create(cluster=cluster)
        previous_semantics = _metadata_semantics(cluster)
        seen_definition_ids: set[int] = set()
        seen_node_ids: set[int] = set()
        by_storage_node = {
            (str(item.get("storage") or ""), node): item for node, items in node_answers.items() for item in items
        }
        for raw in definitions:
            storage_id = str(raw["storage"])
            definition, _ = ClusterStorage.objects.update_or_create(
                cluster=cluster,
                storage_id=storage_id,
                defaults={
                    "storage_type": str(raw.get("type") or "unknown").lower(),
                    # Proxmox returns the content list in arbitrary order; store it
                    # canonically so an unchanged definition compares equal.
                    "content": sorted(set(_list(raw.get("content")))),
                    "shared": _bool(raw.get("shared")),
                    "nodes": _list(raw.get("nodes")),
                    "disabled": _bool(raw.get("disable")),
                    "config": dict(raw),
                    "present": True,
                    "retired_at": None,
                    "observed_metadata_generation": generation,
                    "last_seen_at": observed_at,
                },
            )
            seen_definition_ids.add(definition.pk)
            permitted = set(definition.nodes) if definition.nodes else set(node_online)
            for node in sorted(permitted):
                raw_state = by_storage_node.get((storage_id, node), {})
                online = node_online.get(node, False)
                present = bool(raw_state) and online
                # Not present has two causes that must not be confused: the node
                # answered and the storage is not there, or the node never answered.
                # Both keep `present` False so every gate still refuses — absence
                # requires proof — but only the second is an unknown, and an unknown
                # must stay visible rather than read as "these disks are gone".
                unreachable = not online
                node_state, _ = ClusterStorageNodeState.objects.update_or_create(
                    cluster_storage=definition,
                    node=node,
                    defaults={
                        "active": present and _bool(raw_state.get("active")),
                        "enabled": present and _bool(raw_state.get("enabled"), True) and not definition.disabled,
                        "total_bytes": _int(raw_state.get("total")),
                        "used_bytes": _int(raw_state.get("used")),
                        "available_bytes": _int(raw_state.get("avail")),
                        "present": present,
                        "unreachable": unreachable,
                        "observed_metadata_generation": generation,
                        "last_seen_at": observed_at,
                    },
                )
                seen_node_ids.add(node_state.pk)
        ClusterStorage.objects.filter(cluster=cluster).exclude(pk__in=seen_definition_ids).update(
            present=False, retired_at=observed_at
        )
        ClusterStorageNodeState.objects.filter(cluster_storage__cluster=cluster).exclude(pk__in=seen_node_ids).update(
            present=False, active=False, unreachable=False
        )
        state.metadata_generation = generation
        state.metadata_refreshed_at = observed_at
        state.metadata_last_attempt_at = attempted_at
        state.metadata_complete = True
        state.metadata_errors = {}
        current_semantics = _metadata_semantics(cluster)
        coverage_errors: dict[str, str] = {}
        coverage_rows = ClusterStorageVolumeCoverage.objects.select_for_update().filter(
            cluster_storage__cluster=cluster
        )
        for coverage in coverage_rows.select_related("cluster_storage"):
            storage_id = coverage.cluster_storage.storage_id
            semantics_unchanged = previous_semantics.get(storage_id) == current_semantics.get(storage_id)
            if semantics_unchanged and coverage.complete and coverage.volume_generation is not None:
                # The coverage row is what binds a published volume set to the
                # metadata generation it is still valid under, and it is what
                # every reader checks. Re-stamping each observation as well wrote
                # the whole table on every metadata cycle for a value nothing
                # reads; the per-row field stays as evidence of the generation
                # the observation was made under.
                coverage.based_on_metadata_generation = generation
                coverage.save(update_fields=["based_on_metadata_generation", "updated_at"])
            elif not semantics_unchanged:
                coverage.complete = False
                coverage.error_code = "metadata_changed"
                coverage.error_reason = "Storage metadata changed; volume coverage requires refresh."
                coverage.save(update_fields=["complete", "error_code", "error_reason", "updated_at"])
                coverage_errors[_coverage_error_key(coverage.cluster_storage, coverage.node)] = coverage.error_reason
        state.volume_complete = _coverage_summary_complete(cluster, generation)
        if state.volume_complete:
            state.volume_errors = {}
        elif coverage_errors:
            state.volume_errors = coverage_errors
        state.save()
    return state


def _candidate_nodes(definition: ClusterStorage) -> list[str]:
    # Narrowed in Python so a prefetched definition answers without a query: the
    # volume refresh calls this once per storage, inside the lane that already
    # pays for a Proxmox round trip per storage.
    return sorted(
        state.node for state in definition.node_states.all() if state.present and state.active and state.enabled
    )


def _coverage_error_key(definition: ClusterStorage, node: str | None) -> str:
    return f"{definition.storage_id}@{node}" if node else definition.storage_id


def _coverage_summary_complete(cluster: ProxmoxCluster, metadata_generation: uuid.UUID) -> bool:
    definitions = (
        ClusterStorage.objects.filter(cluster=cluster, present=True, disabled=False)
        .prefetch_related("node_states", "volume_coverages")
        .order_by("storage_id")
    )
    for definition in definitions:
        if backend_profile(definition.storage_type).content_list_mode is not ContentListMode.PVE_API:
            continue
        candidates = _candidate_nodes(definition)
        if not candidates:
            return False
        coverages = {coverage.node: coverage for coverage in definition.volume_coverages.all()}
        required_nodes: tuple[str | None, ...] = (None,) if definition.shared else tuple(candidates)
        for node in required_nodes:
            coverage = coverages.get(node)
            if (
                coverage is None
                or not coverage.complete
                or coverage.volume_generation is None
                or coverage.based_on_metadata_generation != metadata_generation
            ):
                return False
    return True


def _normalize_volume(raw: dict[str, Any]) -> dict[str, Any] | None:
    volid = str(raw.get("volid") or "")
    if not volid:
        return None
    return {
        "volid": volid,
        "vmid": _int(raw.get("vmid")),
        "content": str(raw.get("content") or ""),
        "volume_format": str(raw.get("format") or ""),
        "size_bytes": _int(raw.get("size")),
        "used_bytes": _int(raw.get("used")),
        "metadata": dict(raw),
    }


def refresh_storage_volumes(cluster: ProxmoxCluster) -> StorageCatalogState:
    with transaction.atomic():
        if not _try_advisory_xact_lock(cluster, "volumes"):
            return StorageCatalogState.objects.get_or_create(cluster=cluster)[0]
        return _refresh_storage_volumes_locked(cluster)


def _refresh_storage_volumes_locked(cluster: ProxmoxCluster) -> StorageCatalogState:
    attempted_at = timezone.now()
    state, _ = StorageCatalogState.objects.get_or_create(cluster=cluster)
    metadata_generation = state.metadata_generation
    if not state.metadata_complete or metadata_generation is None:
        ClusterStorageVolumeCoverage.objects.filter(cluster_storage__cluster=cluster).update(
            complete=False,
            last_attempt_at=attempted_at,
            error_code="metadata_incomplete",
            error_reason="Complete storage metadata is required.",
        )
        state.volume_last_attempt_at = attempted_at
        state.volume_complete = False
        state.volume_errors = {"metadata": "Complete storage metadata is required."}
        state.save(update_fields=["volume_last_attempt_at", "volume_complete", "volume_errors", "updated_at"])
        return state
    try:
        clients = _clients(cluster)
        definitions = list(
            ClusterStorage.objects.filter(cluster=cluster, present=True, disabled=False)
            .prefetch_related("node_states")
            .order_by("storage_id")
        )
        successes: list[tuple[ClusterStorage, str, str | None, dict[str, list[dict[str, Any]]]]] = []
        failures: list[tuple[ClusterStorage, str, str | None, str, str]] = []
        errors: dict[str, str] = {}
        for definition in definitions:
            profile = backend_profile(definition.storage_type)
            if profile.content_list_mode is not ContentListMode.PVE_API:
                # Unsupported plugins are explicitly unavailable rather than a
                # failed scope, and therefore do not poison supported storage.
                continue
            candidates = _candidate_nodes(definition)
            if not candidates:
                reason = "No permitted active node."
                failures.append(
                    (
                        definition,
                        ClusterStorageVolumeCoverage.Scope.SHARED
                        if definition.shared
                        else ClusterStorageVolumeCoverage.Scope.NODE,
                        None,
                        "no_active_node",
                        reason,
                    )
                )
                errors[definition.storage_id] = reason
                continue
            if definition.shared:
                answers: dict[str, list[dict[str, Any]]] = {}
                failed_nodes: list[str] = []
                for node in candidates:
                    path = f"nodes/{quote(node, safe='')}/storage/{quote(definition.storage_id, safe='')}/content"
                    try:
                        raw = _get_with_failover(clients, path)
                        if not isinstance(raw, list):
                            raise StorageCatalogError("Invalid storage content response.")
                        answers[node] = [
                            item for item in (_normalize_volume(row) for row in raw if isinstance(row, dict)) if item
                        ]
                    except Exception as exc:
                        logger.warning(
                            "Shared volume listing failed for cluster=%s storage=%s node=%s",
                            cluster.key,
                            definition.storage_id,
                            node,
                            exc_info=True,
                        )
                        failed_nodes.append(node)
                        errors[f"{definition.storage_id}@{node}"] = _public_error(exc)
                if failed_nodes:
                    failures.append(
                        (
                            definition,
                            ClusterStorageVolumeCoverage.Scope.SHARED,
                            None,
                            "required_node_unavailable",
                            "Volume inventory is unavailable on one or more required nodes.",
                        )
                    )
                    continue
                signatures = {
                    tuple(sorted((row["volid"], row["vmid"], row["content"], row["size_bytes"]) for row in rows))
                    for rows in answers.values()
                }
                if len(signatures) != 1:
                    reason = "Shared nodes returned inconsistent volume sets."
                    errors[definition.storage_id] = reason
                    failures.append(
                        (
                            definition,
                            ClusterStorageVolumeCoverage.Scope.SHARED,
                            None,
                            "shared_node_disagreement",
                            reason,
                        )
                    )
                    continue
                successes.append((definition, ClusterStorageVolumeCoverage.Scope.SHARED, None, answers))
                continue
            for node in candidates:
                path = f"nodes/{quote(node, safe='')}/storage/{quote(definition.storage_id, safe='')}/content"
                try:
                    raw = _get_with_failover(clients, path)
                    if not isinstance(raw, list):
                        raise StorageCatalogError("Invalid storage content response.")
                    volumes = [
                        item for item in (_normalize_volume(row) for row in raw if isinstance(row, dict)) if item
                    ]
                    successes.append(
                        (
                            definition,
                            ClusterStorageVolumeCoverage.Scope.NODE,
                            node,
                            {node: volumes},
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        "Node-local volume listing failed for cluster=%s storage=%s node=%s",
                        cluster.key,
                        definition.storage_id,
                        node,
                        exc_info=True,
                    )
                    reason = _public_error(exc)
                    errors[f"{definition.storage_id}@{node}"] = reason
                    failures.append(
                        (
                            definition,
                            ClusterStorageVolumeCoverage.Scope.NODE,
                            node,
                            "node_inventory_unavailable",
                            reason,
                        )
                    )
    except Exception as exc:
        logger.exception("Storage volume refresh failed for cluster=%s", cluster.key)
        reason = _public_error(exc)
        ClusterStorageVolumeCoverage.objects.filter(cluster_storage__cluster=cluster).update(
            complete=False,
            last_attempt_at=attempted_at,
            error_code="refresh_failed",
            error_reason=reason,
        )
        state.refresh_from_db()
        state.volume_last_attempt_at = attempted_at
        state.volume_complete = False
        state.volume_errors = {"refresh": reason}
        state.save(update_fields=["volume_last_attempt_at", "volume_complete", "volume_errors", "updated_at"])
        return state

    generation = uuid.uuid4()
    observed_at = timezone.now()
    with transaction.atomic():
        state = StorageCatalogState.objects.select_for_update().get(cluster=cluster)
        if state.metadata_generation != metadata_generation or not state.metadata_complete:
            state.volume_last_attempt_at = attempted_at
            state.volume_complete = False
            state.volume_errors = {"metadata": "Storage metadata changed during volume refresh."}
            state.save(update_fields=["volume_last_attempt_at", "volume_complete", "volume_errors", "updated_at"])
            return state
        for definition, scope, node, answers in successes:
            coverage, _ = ClusterStorageVolumeCoverage.objects.select_for_update().get_or_create(
                cluster_storage=definition,
                node=node,
                defaults={"scope": scope},
            )
            coverage.scope = scope
            shared = scope == ClusterStorageVolumeCoverage.Scope.SHARED
            # A shared definition publishes one logical set. Every candidate node
            # had to answer identically for it to get here, so the agreement is
            # recorded on the coverage row instead of as one duplicate copy of
            # the whole volume list per node.
            if shared:
                agreeing_nodes = sorted(answers)
                desired = {
                    (SHARED_OBSERVATION_NODE, volume["volid"]): volume for volume in next(iter(answers.values()), [])
                }
            else:
                agreeing_nodes = []
                desired = {
                    (answer_node, volume["volid"]): volume
                    for answer_node, volumes in answers.items()
                    for volume in volumes
                }
            changed = _apply_volume_diff(
                definition,
                scope=scope,
                node=node,
                desired=desired,
                generation=generation,
                metadata_generation=metadata_generation,
                observed_at=observed_at,
                published_generation=coverage.volume_generation if coverage.complete else None,
            )
            coverage.volume_generation = generation if changed else (coverage.volume_generation or generation)
            coverage.based_on_metadata_generation = metadata_generation
            coverage.refreshed_at = observed_at
            coverage.last_attempt_at = attempted_at
            coverage.agreeing_nodes = agreeing_nodes
            coverage.complete = True
            coverage.error_code = ""
            coverage.error_reason = ""
            coverage.save()
        for definition, scope, node, error_code, error_reason in failures:
            if scope == ClusterStorageVolumeCoverage.Scope.NODE and node is None:
                # With no active node there is no independently addressable
                # node-local scope to persist; storage_view reports the metadata
                # condition directly.
                continue
            coverage, _ = ClusterStorageVolumeCoverage.objects.select_for_update().get_or_create(
                cluster_storage=definition,
                node=node,
                defaults={"scope": scope},
            )
            coverage.scope = scope
            coverage.last_attempt_at = attempted_at
            coverage.complete = False
            coverage.error_code = error_code
            coverage.error_reason = error_reason
            coverage.save()
        state.volume_refreshed_at = observed_at
        state.volume_last_attempt_at = attempted_at
        state.volume_complete = not failures and _coverage_summary_complete(cluster, metadata_generation)
        state.volume_errors = errors
        state.save()
    return state


_VOLUME_PAYLOAD_FIELDS = ("vmid", "content", "volume_format", "size_bytes", "used_bytes", "metadata")


def _apply_volume_diff(
    definition: ClusterStorage,
    *,
    scope: str,
    node: str | None,
    desired: dict[tuple[str, str], dict[str, Any]],
    generation: uuid.UUID,
    metadata_generation: uuid.UUID,
    observed_at,
    published_generation: uuid.UUID | None,
) -> bool:
    """Converge one storage scope's observations, writing only what differs.

    The previous implementation deleted and re-created every observation of the
    cluster on every cycle, which is a steady dead-tuple stream for data that
    mostly does not change. A published generation identifies a *set*, not a
    refresh attempt: when the set is byte-identical to the one already published,
    nothing is written at all and the generation stands. Returns whether the set
    changed and therefore needs a new generation.
    """
    existing_rows = ClusterStorageVolumeObservation.objects.filter(cluster_storage=definition)
    if scope == ClusterStorageVolumeCoverage.Scope.NODE:
        existing_rows = existing_rows.filter(node=node)
    else:
        existing_rows = existing_rows.filter(node=SHARED_OBSERVATION_NODE)
    existing = {(row.node, row.volid): row for row in existing_rows}

    def payload(source) -> tuple:
        if isinstance(source, dict):
            return tuple(source.get(field) for field in _VOLUME_PAYLOAD_FIELDS)
        return tuple(getattr(source, field) for field in _VOLUME_PAYLOAD_FIELDS)

    removed = [row.pk for key, row in existing.items() if key not in desired]
    added = [key for key in desired if key not in existing]
    modified = [key for key, row in existing.items() if key in desired and payload(row) != payload(desired[key])]

    # The generation proves *which volumes exist*, which is what an absence proof
    # needs. A volume's used size changes on every thin-provisioned refresh and
    # changes no membership fact, so payload drift is written in place and the
    # published generation stands.
    membership_changed = bool(removed or added)
    if modified:
        for key in modified:
            row = existing[key]
            for field in _VOLUME_PAYLOAD_FIELDS:
                setattr(row, field, desired[key][field])
            row.last_seen_at = observed_at
        ClusterStorageVolumeObservation.objects.bulk_update(
            [existing[key] for key in modified], fields=[*_VOLUME_PAYLOAD_FIELDS, "last_seen_at"]
        )
    if not membership_changed and published_generation is not None:
        # Scope-level freshness lives on the coverage row; re-stamping every
        # unchanged observation would recreate the churn this diff removes.
        return False

    if removed:
        ClusterStorageVolumeObservation.objects.filter(pk__in=removed).delete()
    if added:
        ClusterStorageVolumeObservation.objects.bulk_create(
            [
                ClusterStorageVolumeObservation(
                    cluster_storage=definition,
                    node=key[0],
                    observed_volume_generation=generation,
                    based_on_metadata_generation=metadata_generation,
                    last_seen_at=observed_at,
                    **desired[key],
                )
                for key in added
            ],
            batch_size=500,
        )
    # Every surviving row must carry the newly published generation, or
    # storage_view would hide it as belonging to an older set.
    survivors = [existing[key].pk for key in existing if key in desired]
    if survivors:
        ClusterStorageVolumeObservation.objects.filter(pk__in=survivors).update(
            observed_volume_generation=generation,
            based_on_metadata_generation=metadata_generation,
            last_seen_at=observed_at,
        )
    return True


def refresh_storage_catalog(cluster: ProxmoxCluster) -> StorageCatalogState:
    state = refresh_storage_metadata(cluster)
    if state.metadata_complete:
        state = refresh_storage_volumes(cluster)
    return state


def catalog_state(cluster: ProxmoxCluster) -> StorageCatalogState:
    """The cluster's publication state, read through the reverse one-to-one.

    Going through the relation rather than a fresh queryset lets a caller that
    fans out over many definitions resolve every state in the parent query with
    `select_related("cluster__storage_catalog_state")`. An unsaved instance
    stands in before the first refresh has ever run.
    """
    try:
        return cluster.storage_catalog_state
    except StorageCatalogState.DoesNotExist:
        return StorageCatalogState(cluster=cluster)


def storage_view(definition: ClusterStorage, *, node: str = "") -> StorageView:
    # Every relation below is read with `.all()` and narrowed in Python: that is
    # what lets a prefetched definition answer without a query. `.filter()` or
    # `.order_by()` on a related manager builds a new queryset and silently
    # bypasses the prefetch cache, which is exactly the N+1 this avoids.
    state = catalog_state(definition.cluster)
    profile = backend_profile(definition.storage_type)
    nodes = tuple(sorted((row for row in definition.node_states.all() if row.present), key=lambda row: row.node))
    active_nodes = [row for row in nodes if row.active and row.enabled]
    coverage_rows = tuple(definition.volume_coverages.all())
    coverage_by_node = {coverage.node: coverage for coverage in coverage_rows}
    requested_coverages: list[ClusterStorageVolumeCoverage | None]
    if definition.shared:
        requested_coverages = [coverage_by_node.get(None)]
    elif node:
        requested_coverages = [coverage_by_node.get(node)]
    else:
        requested_coverages = [coverage_by_node.get(row.node) for row in active_nodes]

    def coverage_is_current(coverage: ClusterStorageVolumeCoverage | None) -> bool:
        return bool(
            coverage
            and coverage.complete
            and coverage.volume_generation is not None
            and state.metadata_complete
            and coverage.based_on_metadata_generation == state.metadata_generation
        )

    current_coverages = [coverage for coverage in requested_coverages if coverage_is_current(coverage)]
    display_coverages = [
        coverage
        for coverage in requested_coverages
        if coverage and coverage.volume_generation is not None and profile.content_list_mode is ContentListMode.PVE_API
    ]
    list_reason = ""
    if not profile.known:
        list_reason = f"Unsupported storage type: {definition.storage_type or 'unknown'}."
    elif definition.disabled:
        list_reason = "Storage is disabled in Proxmox."
    elif node and not any(row.node == node and row.active and row.enabled for row in nodes):
        list_reason = "The selected storage instance is not active."
    elif not active_nodes:
        list_reason = "No permitted active node."
    elif not requested_coverages or len(current_coverages) != len(requested_coverages):
        failed = next((coverage for coverage in requested_coverages if not coverage_is_current(coverage)), None)
        if failed and failed.error_reason:
            list_reason = failed.error_reason
        elif not state.metadata_complete:
            list_reason = "Storage metadata inventory is incomplete."
        else:
            list_reason = "Volume coverage has not completed for this storage scope."
    can_list = not list_reason

    bindings = list(definition.mount_bindings.all())
    binding = next((row for row in bindings if definition.shared or row.node == node), None)
    browse_reason = ""
    health = None
    if not profile.filesystem_eligible:
        browse_reason = (
            f"No file browser: {definition.storage_type} is not a browsable file-tree backend."
            if profile.known
            else f"No file browser: unsupported storage type {definition.storage_type or 'unknown'}."
        )
    elif scope_conflict(definition, bindings=bindings):
        browse_reason = "Mount scope conflict; explicitly remap this storage."
    elif binding is None:
        browse_reason = "No host mount is registered for this storage instance."
    else:
        health = mount_health(binding.mount, profile)
        if not health.available:
            browse_reason = health.reason
    can_browse = not browse_reason
    write_reason = browse_reason
    if can_browse and health and not health.writable:
        write_reason = health.reason
    can_write = can_browse and bool(health and health.writable)

    volume_scopes = tuple(
        VolumeScope(
            node=coverage.node if coverage.scope == ClusterStorageVolumeCoverage.Scope.NODE else "",
            generation=coverage.volume_generation,
        )
        for coverage in display_coverages
    )
    coverage_complete = can_list
    coverage_token = ",".join(
        sorted(f"{coverage.node or 'shared'}={coverage.volume_generation}" for coverage in current_coverages)
    )
    return StorageView(
        definition=definition,
        nodes=nodes,
        volume_scopes=volume_scopes,
        mount=binding.mount if binding is not None else None,
        capabilities=StorageCapabilities(can_list, list_reason, can_browse, browse_reason, can_write, write_reason),
        metadata_stale=not state.metadata_complete,
        volumes_stale=not coverage_complete,
        coverage_complete=coverage_complete,
        coverage_reason=list_reason,
        coverage_token=coverage_token,
    )


def storage_volumes(view: StorageView) -> tuple[CatalogVolume, ...]:
    """The published volume observations behind a view.

    Separate from `storage_view` on purpose: this is the part that scales with
    the number of volumes on a datastore, and only the two single-storage detail
    paths read it. The listing page and the usage preflight never call it.
    """
    if not view.volume_scopes:
        return ()
    definition = view.definition
    condition = models.Q(pk__in=[])
    for scope in view.volume_scopes:
        clause = models.Q(observed_volume_generation=scope.generation)
        if scope.node:
            clause &= models.Q(node=scope.node)
        condition |= clause
    observations = definition.volume_observations.filter(condition).order_by("node", "volid")
    if definition.shared:
        observations = observations.filter(node=SHARED_OBSERVATION_NODE)
    return tuple(
        CatalogVolume(
            node=row.node,
            volid=row.volid,
            vmid=row.vmid,
            content=row.content,
            volume_format=row.volume_format,
            size_bytes=row.size_bytes,
            used_bytes=row.used_bytes,
            metadata=dict(row.metadata or {}),
        )
        for row in observations
    )


def node_storage_rows(cluster: ProxmoxCluster, node: str, *, content: str = "") -> list[dict[str, Any]]:
    """Compatibility adapter for operation forms moving off live node fan-out."""
    rows: list[dict[str, Any]] = []
    states = (
        ClusterStorageNodeState.objects.select_related("cluster_storage")
        .filter(
            cluster_storage__cluster=cluster,
            cluster_storage__present=True,
            node=node,
            present=True,
        )
        .order_by("cluster_storage__storage_id")
    )
    for state in states:
        definition = state.cluster_storage
        if content and content not in definition.content:
            continue
        rows.append(
            {
                **dict(definition.config or {}),
                "storage": definition.storage_id,
                "type": definition.storage_type,
                "content": ",".join(definition.content),
                "shared": int(definition.shared),
                "enabled": int(state.enabled and not definition.disabled),
                "active": int(state.active),
                "total": state.total_bytes,
                "used": state.used_bytes,
                "avail": state.available_bytes,
            }
        )
    return rows


def storage_volume_rows(
    cluster: ProxmoxCluster,
    node: str,
    storage_id: str,
    *,
    content: str = "",
    vmid: int | None = None,
) -> tuple[list[dict[str, Any]], bool, str]:
    definition = (
        ClusterStorage.objects.filter(cluster=cluster, storage_id=storage_id, present=True)
        .select_related("cluster__storage_catalog_state")
        .prefetch_related("node_states", "mount_bindings__mount", "volume_coverages")
        .first()
    )
    if definition is None:
        return [], False, "Storage is not present in the latest catalog."
    view = storage_view(definition, node=node)
    rows = [
        {
            **row.metadata,
            "volid": row.volid,
            "vmid": row.vmid,
            "content": row.content,
            "format": row.volume_format,
            "size": row.size_bytes,
            "used": row.used_bytes,
        }
        for row in storage_volumes(view)
        if (not content or row.content == content) and (vmid is None or row.vmid == vmid)
    ]
    return rows, view.coverage_complete, view.coverage_reason


class StorageCatalogChanged(Exception):
    """A published generation moved while one operator action was still running."""


class StorageOperationScope:
    """One catalog refresh shared by every preflight of a single operator action.

    The preflight contract is correct at the *operation* grain: refresh the
    catalog once, then evaluate every affected object against that published
    generation. Callers that fan out over many files must therefore share one
    scope instead of asking each file to refresh the whole cluster again.

    The scope refreshes lazily, once per cluster, and holds the per-storage
    coverage token every preflight evaluated against. If a background refresh
    republishes a generation mid-operation the next preflight raises rather than
    silently mixing snapshots.
    """

    def __init__(self) -> None:
        self._refreshed: set[str] = set()
        self._tokens: dict[tuple[str, str, str], str] = {}

    def ensure_fresh(self, cluster: ProxmoxCluster) -> None:
        if cluster.key in self._refreshed:
            return
        refresh_storage_catalog(cluster)
        self._refreshed.add(cluster.key)

    def preflight(self, definition: ClusterStorage, *, volid: str = "", node: str = "") -> UsagePreflight:
        self.ensure_fresh(definition.cluster)
        definition.refresh_from_db()
        result = usage_preflight(definition, volid=volid, node=node, fresh=False)
        key = (definition.cluster.key, definition.storage_id, node)
        previous = self._tokens.setdefault(key, result.token)
        if previous != result.token:
            raise StorageCatalogChanged(
                f"{definition.cluster.key}:{definition.storage_id} republished its coverage mid-operation"
            )
        return result


def _guest_references(cluster: ProxmoxCluster, storage_id: str, node: str, shared: bool) -> _GuestReferences:
    """Which guests reference which volume of one storage, read in a single pass.

    Answering this per volume meant re-reading every guest row of the cluster per
    volume. The set does not depend on the volume, so it is built once and then
    looked up.
    """
    by_volid: dict[str, set[str]] = {}
    any_volume: set[str] = set()
    prefix = f"{storage_id}:"
    rows = CurrentGuestInventory.objects.filter(cluster=cluster)
    if node and not shared:
        rows = rows.filter(node=node)
    for guest in rows.only("object_type", "vmid", "disk_references"):
        label = f"{guest.object_type}:{guest.vmid}"
        for reference in guest.disk_references or []:
            reference = str(reference)
            if reference.startswith(prefix):
                by_volid.setdefault(reference, set()).add(label)
                any_volume.add(label)
    return _GuestReferences(by_volid=by_volid, any_volume=any_volume)


def _usage_scope(definition: ClusterStorage, node: str) -> _UsageScope:
    """Everything a usage decision needs that does not depend on the volume.

    Building this is the expensive half — a storage view, the catalog state, one
    pass over the cluster's guests, and the same again for every other cluster
    that may be the same physical backend. A caller classifying many volumes of
    one storage builds it once; `usage_preflight` builds it for a single call.
    """
    view = storage_view(definition, node=node)
    state = catalog_state(definition.cluster)
    if state.pk is None:
        return _UsageScope(
            token=f"::{definition.cluster.key}:{definition.storage_id}:{node}",
            unknown_reason="Storage catalog has not completed its first refresh.",
        )
    token = ":".join(
        str(value or "")
        for value in (
            state.metadata_generation,
            view.coverage_token,
            definition.cluster.key,
            definition.storage_id,
            node,
        )
    )
    if not view.coverage_complete:
        return _UsageScope(token=token, unknown_reason=view.coverage_reason)

    references = _guest_references(definition.cluster, definition.storage_id, node, definition.shared)
    candidates, candidate_reason = _cross_cluster_candidates(definition)
    if candidate_reason:
        return _UsageScope(token=token, references=references, candidate_reason=candidate_reason)

    # Candidate order is significant: an incomplete candidate encountered before
    # a matching one makes the whole answer UNKNOWN, so the decision walks this
    # tuple in the order the candidate query produced it.
    scopes: list[_CandidateScope] = []
    for other, other_node in candidates:
        if not storage_view(other, node=other_node).coverage_complete:
            scopes.append(_CandidateScope(cluster_key=other.cluster.key, incomplete=True))
            continue
        scopes.append(
            _CandidateScope(
                cluster_key=other.cluster.key,
                storage_id=other.storage_id,
                references=_guest_references(other.cluster, other.storage_id, other_node, other.shared),
            )
        )
    return _UsageScope(token=token, references=references, candidates=tuple(scopes))


def _usage_decision(scope: _UsageScope, volid: str) -> UsagePreflight:
    if scope.unknown_reason:
        return UsagePreflight(UsageState.UNKNOWN, scope.unknown_reason, scope.token)

    references = scope.references.matching(volid)
    if references:
        return UsagePreflight(
            UsageState.REFERENCED, "Storage content is referenced by guests.", scope.token, tuple(sorted(references))
        )
    if scope.candidate_reason:
        return UsagePreflight(UsageState.UNKNOWN, scope.candidate_reason, scope.token)

    relative = volid.split(":", 1)[1] if ":" in volid else ""
    for candidate in scope.candidates:
        if candidate.incomplete:
            return UsagePreflight(
                UsageState.UNKNOWN,
                "The same backend has incomplete coverage in another cluster.",
                scope.token,
            )
        other_volid = f"{candidate.storage_id}:{relative}" if relative else ""
        matched = candidate.references.matching(other_volid)
        if matched:
            # Named deterministically. The old code reported whichever guest the
            # unordered query happened to yield first.
            return UsagePreflight(
                UsageState.REFERENCED_ELSEWHERE,
                "The same backend is referenced by another cluster.",
                scope.token,
                (f"{candidate.cluster_key}:{min(matched)}",),
            )
    return UsagePreflight(UsageState.UNREFERENCED, "Complete coverage found no references.", scope.token)


def usage_preflight(
    definition: ClusterStorage,
    *,
    volid: str = "",
    node: str = "",
    fresh: bool = True,
) -> UsagePreflight:
    if fresh:
        refresh_storage_catalog(definition.cluster)
        definition.refresh_from_db()
    return _usage_decision(_usage_scope(definition, node), volid)


def _cross_cluster_candidates(definition: ClusterStorage) -> tuple[list[tuple[ClusterStorage, str]], str]:
    """Storage instances in other clusters that may be the same physical backend.

    Two questions used to be conflated here: "is this the same backend as some
    other cluster's storage?" and "does pve-helper have a host mount for it?".
    Only a file-tree backend can answer the first with the second. A block
    backend has no mount to register, ever, so its identity comes from its own
    definition — or, when it is node-local, from the fact that a node belongs to
    exactly one cluster and therefore cannot be reached from another.

    Returns the candidate ``(storage, node)`` pairs, or a reason why the question
    is genuinely unanswerable.
    """
    profile = backend_profile(definition.storage_type)
    bindings = list(definition.mount_bindings.all())
    if bindings:
        if any(not binding.mount.backend_identity for binding in bindings):
            return [], "A registered host mount has no verified backend identity."
        binding_ids = [binding.mount_id for binding in bindings]
        identities = {binding.mount.backend_identity for binding in bindings}
        pairs: list[tuple[ClusterStorage, str]] = []
        others = (
            ClusterStorage.objects.filter(present=True)
            .select_related("cluster__storage_catalog_state")
            .prefetch_related("node_states", "mount_bindings__mount", "volume_coverages")
            .filter(
                models.Q(mount_bindings__mount_id__in=binding_ids)
                | models.Q(mount_bindings__mount__backend_identity__in=identities)
            )
            .exclude(pk=definition.pk)
            .distinct()
        )
        binding_id_set = set(binding_ids)
        for other in others:
            # Narrowed in Python so the prefetched bindings above are reused; the
            # sort makes the candidate order deterministic, which matters because
            # an incomplete candidate seen first makes the whole answer UNKNOWN.
            matching = sorted(
                (
                    row
                    for row in other.mount_bindings.all()
                    if row.mount_id in binding_id_set or row.mount.backend_identity in identities
                ),
                key=lambda row: row.node or "",
            )
            pairs.extend((other, binding.node or "") for binding in {row.node: row for row in matching}.values())
        return pairs, ""

    if profile.filesystem_eligible:
        return [], "No host mount is registered for this storage, so its backend identity is unproven."
    if not definition.shared:
        # A node-local block storage is addressable only through its own node,
        # and a node belongs to exactly one cluster.
        return [], ""

    identity = backend_identity_from_definition(definition)
    if not identity:
        return [], (
            f"The {definition.storage_type or 'unknown'} definition does not publish a cross-cluster backend identity."
        )
    pairs = []
    for other in (
        ClusterStorage.objects.select_related("cluster__storage_catalog_state")
        .prefetch_related("node_states", "mount_bindings__mount", "volume_coverages")
        .filter(present=True, shared=True, storage_type=definition.storage_type)
        .exclude(pk=definition.pk)
    ):
        if backend_identity_from_definition(other) == identity:
            pairs.append((other, ""))
    return pairs, ""


class MountedVolumeClassifier:
    """Classifies many volumes of one host mount against the storage catalog.

    A scan asks this per VM-disk file it finds, and everything except the volume
    id is the same for all of them: the mount's bindings, each binding's storage
    view, the guest references of every cluster involved, and which volumes
    Proxmox actually reported. Resolving that per file made the scan quadratic in
    datastore size — the cost grows with the number of disks, and each answer was
    identical work. It is now resolved once, when the classifier is built.
    """

    def __init__(self, mount: StorageMount) -> None:
        self._bindings = list(mount.cluster_bindings.select_related("cluster_storage", "cluster_storage__cluster"))
        self._scopes = [_usage_scope(binding.cluster_storage, binding.node or "") for binding in self._bindings]
        self._observed: list[set[str]] = []
        for binding in self._bindings:
            observations = binding.cluster_storage.volume_observations
            if binding.node:
                observations = observations.filter(node=binding.node)
            self._observed.append(set(observations.values_list("volid", flat=True)))

    def classify(self, relative_path: str) -> ClassificationResult | None:
        if not self._bindings:
            return None
        suffix = str(relative_path).lstrip("/").removeprefix("images/")
        decisions = [
            _usage_decision(scope, f"{binding.cluster_storage.storage_id}:{suffix}")
            for binding, scope in zip(self._bindings, self._scopes, strict=True)
        ]
        evidence = {
            "catalog_authoritative": True,
            "catalog_decisions": [decision.state.value for decision in decisions],
            "coverage_tokens": [decision.token for decision in decisions],
        }
        if any(decision.state in {UsageState.REFERENCED, UsageState.REFERENCED_ELSEWHERE} for decision in decisions):
            return ClassificationResult(
                FileInventory.Classification.REFERENCED,
                "The API storage catalog found this volume referenced in an associated cluster.",
                {},
                evidence,
            )
        if any(decision.state is UsageState.UNKNOWN for decision in decisions):
            return ClassificationResult(
                FileInventory.Classification.CLASSIFICATION_BLOCKED,
                "The API storage catalog lacks complete coverage for this volume.",
                {},
                evidence,
            )
        observed = any(
            f"{binding.cluster_storage.storage_id}:{suffix}" in volids
            for binding, volids in zip(self._bindings, self._observed, strict=True)
        )
        if not observed:
            return ClassificationResult(
                FileInventory.Classification.UNKNOWN,
                "File resembles a VM disk but is absent from the complete Proxmox volume catalog.",
                {},
                evidence,
            )
        return ClassificationResult(
            FileInventory.Classification.LIKELY_ORPHAN,
            "Proxmox reports this volume, but complete catalog coverage found no guest reference.",
            {},
            evidence,
        )


def classify_mounted_volume(mount: StorageMount, relative_path: str) -> ClassificationResult | None:
    """Single-volume entry point. A caller with many volumes should build one
    `MountedVolumeClassifier` instead of calling this in a loop."""
    return MountedVolumeClassifier(mount).classify(relative_path)

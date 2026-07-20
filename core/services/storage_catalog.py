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
from dataclasses import dataclass
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
class StorageView:
    definition: ClusterStorage
    nodes: tuple[ClusterStorageNodeState, ...]
    volumes: tuple[CatalogVolume, ...]
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
            tuple(
                (state.node, state.present, state.active, state.enabled)
                for state in definition.node_states.all().order_by("node")
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
                present = bool(raw_state) and node_online.get(node, False)
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
                        "observed_metadata_generation": generation,
                        "last_seen_at": observed_at,
                    },
                )
                seen_node_ids.add(node_state.pk)
        ClusterStorage.objects.filter(cluster=cluster).exclude(pk__in=seen_definition_ids).update(
            present=False, retired_at=observed_at
        )
        ClusterStorageNodeState.objects.filter(cluster_storage__cluster=cluster).exclude(pk__in=seen_node_ids).update(
            present=False, active=False
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
    return list(
        definition.node_states.filter(present=True, active=True, enabled=True)
        .order_by("node")
        .values_list("node", flat=True)
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


def storage_view(definition: ClusterStorage, *, node: str = "") -> StorageView:
    state = StorageCatalogState.objects.filter(cluster=definition.cluster).first() or StorageCatalogState(
        cluster=definition.cluster
    )
    profile = backend_profile(definition.storage_type)
    nodes = tuple(definition.node_states.filter(present=True).order_by("node"))
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

    bindings = list(definition.mount_bindings.select_related("mount"))
    binding = next((row for row in bindings if definition.shared or row.node == node), None)
    browse_reason = ""
    health = None
    if not profile.filesystem_eligible:
        browse_reason = (
            f"No file browser: {definition.storage_type} is not a browsable file-tree backend."
            if profile.known
            else f"No file browser: unsupported storage type {definition.storage_type or 'unknown'}."
        )
    elif scope_conflict(definition):
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

    observation_scope = models.Q(pk__in=[])
    for coverage in display_coverages:
        scope = models.Q(observed_volume_generation=coverage.volume_generation)
        if coverage.scope == ClusterStorageVolumeCoverage.Scope.NODE:
            scope &= models.Q(node=coverage.node)
        observation_scope |= scope
    observations = definition.volume_observations.filter(observation_scope).order_by("node", "volid")
    if definition.shared:
        observations = observations.filter(node=SHARED_OBSERVATION_NODE)
    volumes = tuple(
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
    coverage_complete = can_list
    coverage_token = ",".join(
        sorted(f"{coverage.node or 'shared'}={coverage.volume_generation}" for coverage in current_coverages)
    )
    return StorageView(
        definition=definition,
        nodes=nodes,
        volumes=volumes,
        capabilities=StorageCapabilities(can_list, list_reason, can_browse, browse_reason, can_write, write_reason),
        metadata_stale=not state.metadata_complete,
        volumes_stale=not coverage_complete,
        coverage_complete=coverage_complete,
        coverage_reason=list_reason,
        coverage_token=coverage_token,
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
    definition = ClusterStorage.objects.filter(cluster=cluster, storage_id=storage_id, present=True).first()
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
        for row in view.volumes
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
    view = storage_view(definition, node=node)
    state = StorageCatalogState.objects.filter(cluster=definition.cluster).first()
    if state is None:
        return UsagePreflight(
            UsageState.UNKNOWN,
            "Storage catalog has not completed its first refresh.",
            f"::{definition.cluster.key}:{definition.storage_id}:{node}",
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
        return UsagePreflight(UsageState.UNKNOWN, view.coverage_reason, token)

    references: set[str] = set()
    prefix = f"{definition.storage_id}:"
    guest_rows = CurrentGuestInventory.objects.filter(cluster=definition.cluster)
    if node and not definition.shared:
        guest_rows = guest_rows.filter(node=node)
    for guest in guest_rows.only("object_type", "vmid", "disk_references"):
        if any(str(ref).startswith(prefix) and (not volid or str(ref) == volid) for ref in guest.disk_references or []):
            references.add(f"{guest.object_type}:{guest.vmid}")
    if references:
        return UsagePreflight(
            UsageState.REFERENCED, "Storage content is referenced by guests.", token, tuple(sorted(references))
        )

    candidates, unknown_reason = _cross_cluster_candidates(definition)
    if unknown_reason:
        return UsagePreflight(UsageState.UNKNOWN, unknown_reason, token)

    relative = volid.split(":", 1)[1] if ":" in volid else ""
    for other, other_node in candidates:
        other_view = storage_view(other, node=other_node)
        if not other_view.coverage_complete:
            return UsagePreflight(
                UsageState.UNKNOWN,
                "The same backend has incomplete coverage in another cluster.",
                token,
            )
        other_volid = f"{other.storage_id}:{relative}" if relative else ""
        other_prefix = f"{other.storage_id}:"
        guests = CurrentGuestInventory.objects.filter(cluster=other.cluster)
        if other_node and not other.shared:
            guests = guests.filter(node=other_node)
        for guest in guests.only("object_type", "vmid", "disk_references"):
            if any(
                str(ref).startswith(other_prefix) and (not other_volid or str(ref) == other_volid)
                for ref in guest.disk_references or []
            ):
                return UsagePreflight(
                    UsageState.REFERENCED_ELSEWHERE,
                    "The same backend is referenced by another cluster.",
                    token,
                    (f"{other.cluster.key}:{guest.object_type}:{guest.vmid}",),
                )
    return UsagePreflight(UsageState.UNREFERENCED, "Complete coverage found no references.", token)


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
    bindings = list(definition.mount_bindings.select_related("mount"))
    if bindings:
        if any(not binding.mount.backend_identity for binding in bindings):
            return [], "A registered host mount has no verified backend identity."
        binding_ids = [binding.mount_id for binding in bindings]
        identities = {binding.mount.backend_identity for binding in bindings}
        pairs: list[tuple[ClusterStorage, str]] = []
        others = (
            ClusterStorage.objects.filter(present=True)
            .filter(
                models.Q(mount_bindings__mount_id__in=binding_ids)
                | models.Q(mount_bindings__mount__backend_identity__in=identities)
            )
            .exclude(pk=definition.pk)
            .distinct()
        )
        for other in others:
            matching = other.mount_bindings.select_related("mount").filter(
                models.Q(mount_id__in=binding_ids) | models.Q(mount__backend_identity__in=identities)
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
        ClusterStorage.objects.select_related("cluster")
        .filter(present=True, shared=True, storage_type=definition.storage_type)
        .exclude(pk=definition.pk)
    ):
        if backend_identity_from_definition(other) == identity:
            pairs.append((other, ""))
    return pairs, ""


def classify_mounted_volume(mount: StorageMount, relative_path: str) -> ClassificationResult | None:
    bindings = list(mount.cluster_bindings.select_related("cluster_storage", "cluster_storage__cluster").all())
    if not bindings:
        return None
    suffix = str(relative_path).lstrip("/").removeprefix("images/")
    decisions: list[tuple[UsageState, str, str]] = []
    for binding in bindings:
        definition = binding.cluster_storage
        preflight = usage_preflight(
            definition,
            volid=f"{definition.storage_id}:{suffix}",
            node=binding.node or "",
            fresh=False,
        )
        decisions.append((preflight.state, preflight.reason, preflight.token))
    evidence = {
        "catalog_authoritative": True,
        "catalog_decisions": [state.value for state, _reason, _token in decisions],
        "coverage_tokens": [token for _state, _reason, token in decisions],
    }
    if any(state in {UsageState.REFERENCED, UsageState.REFERENCED_ELSEWHERE} for state, _reason, _token in decisions):
        return ClassificationResult(
            FileInventory.Classification.REFERENCED,
            "The API storage catalog found this volume referenced in an associated cluster.",
            {},
            evidence,
        )
    if any(state is UsageState.UNKNOWN for state, _reason, _token in decisions):
        return ClassificationResult(
            FileInventory.Classification.CLASSIFICATION_BLOCKED,
            "The API storage catalog lacks complete coverage for this volume.",
            {},
            evidence,
        )
    observed = any(
        binding.cluster_storage.volume_observations.filter(
            volid=f"{binding.cluster_storage.storage_id}:{suffix}",
            **({"node": binding.node} if binding.node else {}),
        ).exists()
        for binding in bindings
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

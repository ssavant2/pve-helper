"""Storage-owned projection finalization for cluster retirement.

The generic retirement service must not know which storage rows are current
publication state, durable identity, operator-owned safety input, or retained
history. This adapter makes that decision inside the storage module and binds the
decision to a digest. A preflight can therefore be shown to an operator without
holding locks, while the final transaction rejects any catalog, binding, or
consumer change made after that preflight.

No function in this module contacts Proxmox. The finalizer is set-based and must
run inside the retirement transaction, after the shared cluster lifecycle lock
and cluster row lock have been acquired.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import models, transaction
from django.utils import timezone

from core.models import (
    ClusterStorage,
    ClusterStorageMount,
    ClusterStorageNodeState,
    ClusterStorageVolumeCoverage,
    ClusterStorageVolumeObservation,
    ProxmoxCluster,
    ProxmoxStorageConsumer,
    StorageCatalogState,
)
from core.services.refs import ClusterStorageRef, MountRef, NodeRef

_AUDIT_REF_LIMIT = 100


class StorageRetirementError(RuntimeError):
    """Base class for stable storage-retirement refusal conditions."""


class StorageRetirementImpactChanged(StorageRetirementError):
    """The storage impact no longer matches the operator's preflight."""

    def __init__(self, *, expected_digest: str, actual_digest: str):
        self.expected_digest = expected_digest
        self.actual_digest = actual_digest
        super().__init__("Storage retirement impact changed; run preflight again.")


class StorageRetirementConsumersBlock(StorageRetirementError):
    """Verified retirement still has explicitly owned consumer relationships."""

    def __init__(self, consumers: tuple[StorageRetirementConsumer, ...]):
        self.consumers = consumers
        super().__init__("Storage consumers must be explicitly released before verified retirement.")


@dataclass(frozen=True)
class StorageRetirementConsumer:
    """One operator-owned shared-storage relationship affected by retirement."""

    pk: int
    mount_ref: str
    storage_id: str
    storage_name: str
    node_ref: str
    node: str
    last_observed_at: datetime | None
    last_gate_status: str


@dataclass(frozen=True)
class StorageRetirementMount:
    """One active catalog-to-host binding that finalization will remove."""

    pk: int
    definition_ref: str
    mount_ref: str
    storage_id: str
    node: str
    scope: str


@dataclass(frozen=True)
class StorageRetirementPreflight:
    """Generation-bound storage impact returned to the retirement preflight."""

    mode: str
    impact_digest: str
    metadata_generation: str
    definition_count: int
    node_state_count: int
    coverage_count: int
    observation_count: int
    mounts: tuple[StorageRetirementMount, ...]
    consumers: tuple[StorageRetirementConsumer, ...]

    @property
    def consumer_gate_clear(self) -> bool:
        """Forced retirement owns the recorded assertion; verified does not."""
        return self.mode == ProxmoxCluster.RetirementMode.FORCED or not self.consumers


@dataclass(frozen=True)
class StorageRetirementResult:
    """Bounded, Audit-safe facts produced by the committed storage transition."""

    impact_digest: str
    definitions_unmanaged: int
    node_states_deleted: int
    coverages_deleted: int
    observations_deleted: int
    bindings_deleted: int
    consumers_deleted: int
    catalog_states_deleted: int
    mount_refs: tuple[str, ...]
    mount_ref_count: int
    mount_refs_omitted: int
    consumers: tuple[StorageRetirementConsumer, ...]
    consumer_refs: tuple[str, ...]
    consumer_ref_count: int
    consumer_refs_omitted: int


def _normalize_mode(mode: str) -> str:
    value = getattr(mode, "value", mode)
    value = str(value)
    if value not in {
        ProxmoxCluster.RetirementMode.VERIFIED,
        ProxmoxCluster.RetirementMode.FORCED,
    }:
        raise ValueError(f"Unsupported retirement mode: {value!r}")
    return value


def _model_fields(model: type[models.Model]) -> tuple[str, ...]:
    return tuple(field.attname for field in model._meta.concrete_fields)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _hash_rows(digest, label: str, rows) -> int:
    """Hash a deterministic queryset without materialising large observations."""
    digest.update(label.encode())
    count = 0
    for row in rows.iterator(chunk_size=500):
        digest.update(json.dumps(row, default=_json_default, separators=(",", ":")).encode())
        digest.update(b"\n")
        count += 1
    return count


def _for_update(queryset, lock: bool):
    return queryset.select_for_update() if lock else queryset


def _storage_snapshot(
    cluster: ProxmoxCluster,
    *,
    mode: str,
    lock: bool,
) -> StorageRetirementPreflight:
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            {
                "contract": 1,
                "cluster_pk": cluster.pk,
                "cluster_key": cluster.key,
                "mode": mode,
            },
            separators=(",", ":"),
        ).encode()
    )

    state_fields = _model_fields(StorageCatalogState)
    state_rows = list(
        _for_update(
            StorageCatalogState.objects.filter(cluster_id=cluster.pk).order_by("pk"),
            lock,
        ).values_list(*state_fields)
    )
    digest.update(b"catalog_state")
    for row in state_rows:
        digest.update(json.dumps(row, default=_json_default, separators=(",", ":")).encode())
        digest.update(b"\n")
    metadata_generation = str(state_rows[0][state_fields.index("metadata_generation")] or "") if state_rows else ""

    definition_count = _hash_rows(
        digest,
        "definitions",
        _for_update(
            ClusterStorage.objects.filter(cluster_id=cluster.pk).order_by("pk"),
            lock,
        ).values_list(*_model_fields(ClusterStorage)),
    )
    node_state_count = _hash_rows(
        digest,
        "node_states",
        _for_update(
            ClusterStorageNodeState.objects.filter(cluster_storage__cluster_id=cluster.pk).order_by("pk"),
            lock,
        ).values_list(*_model_fields(ClusterStorageNodeState)),
    )
    coverage_count = _hash_rows(
        digest,
        "coverages",
        _for_update(
            ClusterStorageVolumeCoverage.objects.filter(cluster_storage__cluster_id=cluster.pk).order_by("pk"),
            lock,
        ).values_list(*_model_fields(ClusterStorageVolumeCoverage)),
    )
    observation_count = _hash_rows(
        digest,
        "observations",
        _for_update(
            ClusterStorageVolumeObservation.objects.filter(cluster_storage__cluster_id=cluster.pk).order_by("pk"),
            lock,
        ).values_list(*_model_fields(ClusterStorageVolumeObservation)),
    )

    binding_fields = _model_fields(ClusterStorageMount)
    binding_rows = list(
        _for_update(
            ClusterStorageMount.objects.filter(cluster_storage__cluster_id=cluster.pk).order_by("pk"),
            lock,
        ).values_list(
            *binding_fields,
            "cluster_storage__storage_id",
            "mount__mount_key",
        )
    )
    digest.update(b"bindings")
    for row in binding_rows:
        digest.update(json.dumps(row, default=_json_default, separators=(",", ":")).encode())
        digest.update(b"\n")
    binding_index = {name: index for index, name in enumerate(binding_fields)}
    mounts = tuple(
        StorageRetirementMount(
            pk=row[binding_index["id"]],
            definition_ref=ClusterStorageRef(
                cluster.key,
                str(row[len(binding_fields)]),
            ).serialize(),
            mount_ref=MountRef(str(row[len(binding_fields) + 1])).serialize(),
            storage_id=str(row[len(binding_fields)]),
            node=str(row[binding_index["node"]] or ""),
            scope=str(row[binding_index["scope"]]),
        )
        for row in binding_rows
    )

    consumer_fields = _model_fields(ProxmoxStorageConsumer)
    consumer_rows = list(
        _for_update(
            ProxmoxStorageConsumer.objects.filter(cluster_id=cluster.pk).order_by("pk"),
            lock,
        ).values_list(
            *consumer_fields,
            "storage__mount_key",
            "storage__storage_id",
            "storage__display_name",
        )
    )
    digest.update(b"consumers")
    for row in consumer_rows:
        digest.update(json.dumps(row, default=_json_default, separators=(",", ":")).encode())
        digest.update(b"\n")
    consumer_index = {name: index for index, name in enumerate(consumer_fields)}
    consumers = tuple(
        StorageRetirementConsumer(
            pk=row[consumer_index["id"]],
            mount_ref=MountRef(str(row[len(consumer_fields)])).serialize(),
            storage_id=str(row[len(consumer_fields) + 1]),
            storage_name=str(row[len(consumer_fields) + 2]),
            node_ref=NodeRef(
                cluster_key=cluster.key,
                node=str(row[consumer_index["expected_node_name"]]),
            ).serialize(),
            node=str(row[consumer_index["expected_node_name"]]),
            last_observed_at=row[consumer_index["last_successful_inventory_scan"]],
            last_gate_status=str(row[consumer_index["last_gate_status"]]),
        )
        for row in consumer_rows
    )

    return StorageRetirementPreflight(
        mode=mode,
        impact_digest=digest.hexdigest(),
        metadata_generation=metadata_generation,
        definition_count=definition_count,
        node_state_count=node_state_count,
        coverage_count=coverage_count,
        observation_count=observation_count,
        mounts=mounts,
        consumers=consumers,
    )


def cluster_retirement_storage_preflight(
    cluster: ProxmoxCluster,
    *,
    mode: str,
) -> StorageRetirementPreflight:
    """Return the current local storage impact without contacting Proxmox."""
    return _storage_snapshot(cluster, mode=_normalize_mode(mode), lock=False)


def _bounded(values: list[str]) -> tuple[tuple[str, ...], int, int]:
    unique = sorted(set(values))
    shown = tuple(unique[:_AUDIT_REF_LIMIT])
    return shown, len(unique), max(0, len(unique) - len(shown))


def finalize_cluster_retirement_storage(
    cluster: ProxmoxCluster,
    *,
    mode: str,
    expected_digest: str,
    unmanaged_at: datetime | None = None,
) -> StorageRetirementResult:
    """Apply the storage-owned retirement transition after revalidating impact.

    The caller owns the surrounding retirement transaction and lock order. An
    inner savepoint keeps this adapter fault-atomic even when a caller catches its
    exception before leaving that outer transaction.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("finalize_cluster_retirement_storage must run inside transaction.atomic().")
    normalized_mode = _normalize_mode(mode)
    with transaction.atomic():
        current = _storage_snapshot(cluster, mode=normalized_mode, lock=True)
        if current.impact_digest != expected_digest:
            raise StorageRetirementImpactChanged(
                expected_digest=expected_digest,
                actual_digest=current.impact_digest,
            )
        if normalized_mode == ProxmoxCluster.RetirementMode.VERIFIED and current.consumers:
            raise StorageRetirementConsumersBlock(current.consumers)

        transition_at = unmanaged_at or timezone.now()
        definitions_unmanaged = ClusterStorage.objects.filter(
            cluster_id=cluster.pk,
            unmanaged_at__isnull=True,
        ).update(unmanaged_at=transition_at)
        observations_deleted = ClusterStorageVolumeObservation.objects.filter(
            cluster_storage__cluster_id=cluster.pk
        ).delete()[0]
        coverages_deleted = ClusterStorageVolumeCoverage.objects.filter(
            cluster_storage__cluster_id=cluster.pk
        ).delete()[0]
        node_states_deleted = ClusterStorageNodeState.objects.filter(cluster_storage__cluster_id=cluster.pk).delete()[0]
        bindings_deleted = ClusterStorageMount.objects.filter(cluster_storage__cluster_id=cluster.pk).delete()[0]
        catalog_states_deleted = StorageCatalogState.objects.filter(cluster_id=cluster.pk).delete()[0]
        consumers_deleted = 0
        if normalized_mode == ProxmoxCluster.RetirementMode.FORCED:
            consumers_deleted = ProxmoxStorageConsumer.objects.filter(cluster_id=cluster.pk).delete()[0]

        mount_refs, mount_ref_count, mount_refs_omitted = _bounded([mount.mount_ref for mount in current.mounts])
        consumers = current.consumers[:_AUDIT_REF_LIMIT]
        consumer_refs = tuple(f"{consumer.mount_ref}@{consumer.node_ref}" for consumer in consumers)
        consumer_ref_count = len(current.consumers)
        consumer_refs_omitted = max(0, consumer_ref_count - len(consumers))
        return StorageRetirementResult(
            impact_digest=current.impact_digest,
            definitions_unmanaged=definitions_unmanaged,
            node_states_deleted=node_states_deleted,
            coverages_deleted=coverages_deleted,
            observations_deleted=observations_deleted,
            bindings_deleted=bindings_deleted,
            consumers_deleted=consumers_deleted,
            catalog_states_deleted=catalog_states_deleted,
            mount_refs=mount_refs,
            mount_ref_count=mount_ref_count,
            mount_refs_omitted=mount_refs_omitted,
            consumers=consumers,
            consumer_refs=consumer_refs,
            consumer_ref_count=consumer_ref_count,
            consumer_refs_omitted=consumer_refs_omitted,
        )

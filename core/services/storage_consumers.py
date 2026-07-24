"""Operator-owned storage-consumer release.

``ProxmoxStorageConsumer`` rows are safety inputs, not disposable scan output.
Removing them therefore requires a short-lived confirmation bound to the exact
relationships shown to the operator.  The final mutation serializes with scan
admission and cluster retirement so a scan cannot resurrect a row after it was
explicitly released.

No function in this module contacts Proxmox.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from django.core import signing
from django.db import transaction

from core.models import AuditEvent, ProxmoxCluster, ProxmoxStorageConsumer, ScanRun
from core.services.audit_events import record_audit_event
from core.services.cluster_lifecycle_lock import cluster_lifecycle_lock, scan_admission_lock
from core.services.cluster_scopes import historical_clusters
from core.services.public_errors import PublicMessageError
from core.services.refs import MountRef, NodeRef

STORAGE_CONSUMER_RELEASE_SALT = "pve-helper.storage-consumer-release.v1"
STORAGE_CONSUMER_RELEASE_MAX_AGE_SECONDS = 10 * 60
STORAGE_CONSUMER_AUDIT_DETAIL_LIMIT = 100

ERROR_CODE_RELEASE_INVALID = "storage_consumer_release_invalid"
ERROR_CODE_RELEASE_CHANGED = "storage_consumer_release_changed"
ERROR_CODE_RELEASE_NOT_ALLOWED = "storage_consumer_release_not_allowed"
ERROR_CODE_RELEASE_ACTIVE_SCAN = "storage_consumer_release_active_scan"

_TOKEN_FIELDS = frozenset(
    {
        "version",
        "cluster_pk",
        "cluster_key",
        "user_id",
        "consumer_count",
        "consumer_digest",
    }
)


class StorageConsumerReleaseError(PublicMessageError, RuntimeError):
    """Base class for stable, operator-safe release refusals."""


class StorageConsumerReleaseInvalid(StorageConsumerReleaseError):
    error_code = ERROR_CODE_RELEASE_INVALID


class StorageConsumerReleaseChanged(StorageConsumerReleaseError):
    error_code = ERROR_CODE_RELEASE_CHANGED


class StorageConsumerReleaseNotAllowed(StorageConsumerReleaseError):
    error_code = ERROR_CODE_RELEASE_NOT_ALLOWED


class StorageConsumerReleaseActiveScan(StorageConsumerReleaseError):
    error_code = ERROR_CODE_RELEASE_ACTIVE_SCAN


@dataclass(frozen=True)
class StorageConsumerRelationship:
    """One exact relationship displayed before release."""

    pk: int
    mount_ref: str
    storage_id: str
    storage_name: str
    node_ref: str
    node: str
    last_observed_at: datetime | None
    last_gate_status: str


@dataclass(frozen=True)
class StorageConsumerReleasePreflight:
    cluster_pk: int
    cluster_key: str
    consumers: tuple[StorageConsumerRelationship, ...]
    confirmation: str


@dataclass(frozen=True)
class StorageConsumerReleaseResult:
    cluster_pk: int
    cluster_key: str
    consumers_deleted: int
    audit_event_id: int


def _actor_id(actor) -> str:
    if actor is None or not getattr(actor, "is_authenticated", False):
        return ""
    return str(actor.pk)


def _consumer_snapshot(cluster_id: int, cluster_key: str, *, lock: bool) -> tuple[StorageConsumerRelationship, ...]:
    queryset = ProxmoxStorageConsumer.objects.filter(cluster_id=cluster_id).select_related("storage")
    if lock:
        queryset = queryset.select_for_update()
    rows = queryset.order_by(
        "storage__display_name",
        "storage__storage_id",
        "storage__mount_key",
        "expected_node_name",
        "pk",
    )
    return tuple(
        StorageConsumerRelationship(
            pk=row.pk,
            mount_ref=MountRef(str(row.storage.mount_key)).serialize(),
            storage_id=row.storage.storage_id,
            storage_name=row.storage.display_name,
            node_ref=NodeRef(cluster_key=cluster_key, node=row.expected_node_name).serialize(),
            node=row.expected_node_name,
            last_observed_at=row.last_successful_inventory_scan,
            last_gate_status=row.last_gate_status,
        )
        for row in rows
    )


def _consumer_digest(consumers: tuple[StorageConsumerRelationship, ...]) -> str:
    payload = [
        {
            "pk": consumer.pk,
            "mount_ref": consumer.mount_ref,
            "storage_id": consumer.storage_id,
            "storage_name": consumer.storage_name,
            "node_ref": consumer.node_ref,
            "node": consumer.node,
            "last_observed_at": (
                consumer.last_observed_at.isoformat() if consumer.last_observed_at is not None else None
            ),
            "last_gate_status": consumer.last_gate_status,
        }
        for consumer in consumers
    ]
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode()).hexdigest()


def cluster_storage_consumer_release_preflight(
    cluster: ProxmoxCluster,
    *,
    actor,
) -> StorageConsumerReleasePreflight:
    """List and bind every consumer relationship owned by one managed cluster."""
    try:
        current = historical_clusters().only("pk", "key", "retired_at").get(pk=cluster.pk)
    except ProxmoxCluster.DoesNotExist as exc:
        raise StorageConsumerReleaseNotAllowed("The cluster connection no longer exists.") from exc
    if current.retired_at is not None:
        raise StorageConsumerReleaseNotAllowed("A retired cluster has no active storage consumers to release.")

    consumers = _consumer_snapshot(current.pk, current.key, lock=False)
    confirmation = ""
    if consumers:
        confirmation = signing.dumps(
            {
                "version": 1,
                "cluster_pk": current.pk,
                "cluster_key": current.key,
                "user_id": _actor_id(actor),
                "consumer_count": len(consumers),
                "consumer_digest": _consumer_digest(consumers),
            },
            salt=STORAGE_CONSUMER_RELEASE_SALT,
            compress=True,
        )
    return StorageConsumerReleasePreflight(
        cluster_pk=current.pk,
        cluster_key=current.key,
        consumers=consumers,
        confirmation=confirmation,
    )


def _load_confirmation(confirmation: str) -> dict:
    try:
        payload = signing.loads(
            str(confirmation or ""),
            salt=STORAGE_CONSUMER_RELEASE_SALT,
            max_age=STORAGE_CONSUMER_RELEASE_MAX_AGE_SECONDS,
        )
    except (signing.BadSignature, signing.SignatureExpired) as exc:
        raise StorageConsumerReleaseInvalid(
            "This storage-consumer confirmation is invalid or has expired. Reload the datastore and confirm again."
        ) from exc
    if not isinstance(payload, dict) or frozenset(payload) != _TOKEN_FIELDS:
        raise StorageConsumerReleaseInvalid(
            "This storage-consumer confirmation is invalid. Reload the datastore and confirm again."
        )
    if (
        payload.get("version") != 1
        or isinstance(payload.get("cluster_pk"), bool)
        or not isinstance(payload.get("cluster_pk"), int)
        or not isinstance(payload.get("cluster_key"), str)
        or not isinstance(payload.get("user_id"), str)
        or isinstance(payload.get("consumer_count"), bool)
        or not isinstance(payload.get("consumer_count"), int)
        or not isinstance(payload.get("consumer_digest"), str)
    ):
        raise StorageConsumerReleaseInvalid(
            "This storage-consumer confirmation is invalid. Reload the datastore and confirm again."
        )
    return payload


def _audit_consumer_refs(consumers: tuple[StorageConsumerRelationship, ...]) -> tuple[list[str], int]:
    shown = consumers[:STORAGE_CONSUMER_AUDIT_DETAIL_LIMIT]
    return (
        [f"{consumer.mount_ref}@{consumer.node_ref}" for consumer in shown],
        max(0, len(consumers) - len(shown)),
    )


def release_cluster_storage_consumers(
    cluster: ProxmoxCluster,
    *,
    confirmation: str,
    actor,
    source_ip: str | None = None,
) -> StorageConsumerReleaseResult:
    """Delete exactly the relationships confirmed by the current operator."""
    payload = _load_confirmation(confirmation)
    if (
        payload["cluster_pk"] != cluster.pk
        or payload["cluster_key"] != cluster.key
        or payload["user_id"] != _actor_id(actor)
    ):
        raise StorageConsumerReleaseInvalid(
            "This confirmation belongs to another cluster or operator. Reload the datastore and confirm again."
        )

    with transaction.atomic():
        with scan_admission_lock():
            with cluster_lifecycle_lock(cluster):
                try:
                    current = historical_clusters().select_for_update().get(pk=cluster.pk)
                except ProxmoxCluster.DoesNotExist as exc:
                    raise StorageConsumerReleaseNotAllowed("The cluster connection no longer exists.") from exc
                if current.retired_at is not None:
                    raise StorageConsumerReleaseNotAllowed(
                        "A retired cluster has no active storage consumers to release."
                    )
                if ScanRun.objects.filter(status__in=(ScanRun.Status.QUEUED, ScanRun.Status.RUNNING)).exists():
                    raise StorageConsumerReleaseActiveScan(
                        "Storage consumers cannot be released while a storage scan is queued or running."
                    )

                consumers = _consumer_snapshot(current.pk, current.key, lock=True)
                if (
                    not consumers
                    or payload["consumer_count"] != len(consumers)
                    or payload["consumer_digest"] != _consumer_digest(consumers)
                ):
                    raise StorageConsumerReleaseChanged(
                        "Storage consumer relationships changed after they were shown. "
                        "Reload the datastore, review the exact list and confirm again."
                    )

                consumer_ids = [consumer.pk for consumer in consumers]
                deleted = ProxmoxStorageConsumer.objects.filter(
                    cluster_id=current.pk,
                    pk__in=consumer_ids,
                ).delete()[0]
                if deleted != len(consumers):
                    raise StorageConsumerReleaseChanged(
                        "Storage consumer relationships changed while they were being released. "
                        "Reload the datastore and review the remaining relationships."
                    )

                consumer_refs, consumer_refs_omitted = _audit_consumer_refs(consumers)
                event: AuditEvent = record_audit_event(
                    user=actor,
                    source_ip=source_ip,
                    action="storage.consumers.released",
                    object_type="cluster_storage_consumers",
                    object_id=current.key,
                    cluster=current,
                    details={
                        "cluster_key": current.key,
                        "consumer_count": len(consumers),
                        "consumer_refs": consumer_refs,
                        "consumer_refs_omitted": consumer_refs_omitted,
                        "explicit_operator_resolution": True,
                    },
                )
                return StorageConsumerReleaseResult(
                    cluster_pk=current.pk,
                    cluster_key=current.key,
                    consumers_deleted=deleted,
                    audit_event_id=event.pk,
                )

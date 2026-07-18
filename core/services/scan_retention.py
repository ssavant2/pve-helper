from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from core.models import FileInventory, ProxmoxCluster, ProxmoxInventory, ScanRun, StorageMount


SCAN_METADATA_RETENTION_DAYS = 7


@dataclass(frozen=True)
class ScanRetentionResult:
    kept_scan_ids: set[int]
    kept_file_pairs: set[tuple[int, int]]
    deleted_files: int
    deleted_proxmox_objects: int
    deleted_scan_runs: int

    @property
    def deleted_anything(self) -> bool:
        return bool(self.deleted_files or self.deleted_proxmox_objects or self.deleted_scan_runs)


def prune_scan_history(*, now=None, metadata_retention_days: int = SCAN_METADATA_RETENTION_DAYS) -> ScanRetentionResult:
    """Keep only current inventory plus short-lived scan metadata.

    FileInventory is kept per storage for the scan that currently backs that
    storage. ProxmoxInventory is kept for any scan still backing at least one
    storage. Old ScanRun metadata is small, so it is kept briefly for Monitor /
    Recent Tasks and then removed.
    """
    now = now or timezone.now()
    kept_file_pairs = _current_file_inventory_pairs()
    kept_scan_ids = {scan_id for scan_id, _storage_id in kept_file_pairs}
    # ProxmoxInventory (cluster guests + local/shared storages) has a different
    # lifecycle from mounted-storage FileInventory: a full scan that observed a
    # cluster must survive retention even when it backs no mounted storage, or the
    # Datastores nav and storage tabs lose their local storages. Keep the latest
    # completed scan per cluster in addition to the file-backing scans.
    kept_scan_ids |= _current_proxmox_inventory_scan_ids()

    deleted_files, _ = _stale_file_inventory(kept_file_pairs).delete()
    deleted_proxmox, _ = (
        ProxmoxInventory.objects.filter(scan_run__status=ScanRun.Status.COMPLETED)
        .exclude(scan_run_id__in=kept_scan_ids)
        .delete()
    )

    cutoff = now - timedelta(days=metadata_retention_days)
    deleted_scan_runs, _ = (
        ScanRun.objects.filter(
            Q(finished_at__lt=cutoff) | Q(finished_at__isnull=True, created_at__lt=cutoff),
            status__in=[
                ScanRun.Status.COMPLETED,
                ScanRun.Status.FAILED,
                ScanRun.Status.CANCELLED,
            ],
        )
        .exclude(id__in=kept_scan_ids)
        .delete()
    )

    return ScanRetentionResult(
        kept_scan_ids=kept_scan_ids,
        kept_file_pairs=kept_file_pairs,
        deleted_files=deleted_files,
        deleted_proxmox_objects=deleted_proxmox,
        deleted_scan_runs=deleted_scan_runs,
    )


def _current_proxmox_inventory_scan_ids() -> set[int]:
    """Scans whose ProxmoxInventory backs the current per-cluster read model.

    Matches how the cluster/local-datastore reads pick the authoritative scan:
    the most recent completed scan that produced inventory for each cluster,
    ordered by ``-finished_at, -created_at`` so retention never prunes the scan a
    read is about to select. Legacy null-cluster inventory keeps its own latest.
    """
    ids: set[int] = set()
    scoped_clusters = list(ProxmoxCluster.objects.all())
    filters = [{"proxmox_objects__cluster": cluster} for cluster in scoped_clusters]
    filters.append({"proxmox_objects__cluster__isnull": True})
    for scan_filter in filters:
        scan = (
            ScanRun.objects.filter(status=ScanRun.Status.COMPLETED, **scan_filter)
            .order_by("-finished_at", "-created_at")
            .distinct()
            .first()
        )
        if scan is not None:
            ids.add(scan.id)
    return ids


def _current_file_inventory_pairs() -> set[tuple[int, int]]:
    pairs: set[tuple[int, int]] = set()
    for storage in StorageMount.objects.filter(enabled=True).order_by("id"):
        scan = _latest_storage_result_scan(storage)
        if scan:
            pairs.add((scan.id, storage.id))
    return pairs


def _stale_file_inventory(kept_file_pairs: set[tuple[int, int]]):
    queryset = FileInventory.objects.all()
    if not kept_file_pairs:
        return queryset

    keep_filter = Q()
    for scan_id, storage_id in kept_file_pairs:
        keep_filter |= Q(scan_run_id=scan_id, storage_id=storage_id)
    return queryset.exclude(keep_filter)


def _latest_storage_result_scan(storage: StorageMount) -> ScanRun | None:
    return (
        ScanRun.objects.filter(status=ScanRun.Status.COMPLETED)
        .filter(Q(target_storage=storage) | Q(target_storage__isnull=True))
        .order_by("-filesystem_scan_at", "-finished_at", "-created_at")
        .first()
    )

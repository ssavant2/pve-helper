"""Application dashboard.

It aggregates scan, storage and audit state; it is not a storage surface and
does not live in the storage package.
"""

from __future__ import annotations

from core.models import (
    ClusterStorage,
    ProxmoxCluster,
)
from core.services.cluster_scopes import managed_clusters
from core.services.storage_catalog import (
    storage_view,
)
from core.services.storage_mounts import (
    registered_mount_health,
)

from . import common
from .common import (
    AuditEvent,
    FileInventory,
    ScanRun,
    StorageMount,
    _active_scan,
    _latest_result_scan,
    _scan_timestamp,
    app_login_required,
    navigation_context,
    render,
    scan_schedule_state,
    storage_details,
    trash_purge_schedule_state,
)
from .storage._shared import (
    _classification_counts,
    _latest_storage_result_scan,
)


@app_login_required
def dashboard(request):
    latest_scan = ScanRun.objects.order_by("-created_at").first()
    result_scan = _latest_result_scan()
    storages = list(StorageMount.objects.filter(enabled=True).order_by("display_name"))
    catalog_rows = _storage_catalog_rows()
    _decorate_storages_with_scan_state(storages, result_scan)
    classification_counts = _current_classification_counts(storages)
    context = {
        **navigation_context("dashboard"),
        "latest_scan": latest_scan,
        "result_scan": result_scan,
        "storage_definition_count": len(catalog_rows),
        "storage_mount_count": len(storages),
        "scan_count": ScanRun.objects.count(),
        "audit_count": AuditEvent.objects.count(),
        "classification_counts": classification_counts,
        "catalog_rows": catalog_rows,
        "clusters_without_storage": _clusters_without_storage(),
        "storage_gate_rows": _storage_gate_rows(storages, result_scan),
        "scan_schedule": scan_schedule_state(),
        "trash_purge_schedule": _trash_purge_schedule_state(),
        "active_scan": _active_scan(),
    }
    return render(request, "core/dashboard.html", context)


def _clusters_without_storage() -> list[ProxmoxCluster]:
    """Enabled clusters whose catalog has not published a current definition."""
    represented = set(
        ClusterStorage.objects.filter(
            cluster__retired_at__isnull=True,
            unmanaged_at__isnull=True,
            present=True,
        ).values_list("cluster_id", flat=True)
    )
    return list(managed_clusters().filter(enabled=True).exclude(pk__in=represented).order_by("key"))


def _storage_catalog_rows() -> list[dict]:
    catalog_rows = []
    definitions = (
        ClusterStorage.objects.select_related("cluster__storage_catalog_state")
        .filter(
            cluster__enabled=True,
            cluster__retired_at__isnull=True,
            unmanaged_at__isnull=True,
            present=True,
        )
        .prefetch_related("node_states", "mount_bindings__mount", "volume_coverages")
        .order_by("cluster__display_name", "storage_id")
    )
    for definition in definitions:
        nodes = sorted(
            (node_state for node_state in definition.node_states.all() if node_state.present),
            key=lambda node_state: node_state.node,
        )
        selected_node = next((row.node for row in nodes if row.active), nodes[0].node if nodes else "")
        view = storage_view(definition, node=selected_node)
        catalog_rows.append(
            {
                "definition": definition,
                "view": view,
                "node": selected_node,
                "nodes": nodes,
            }
        )

    return catalog_rows


def _decorate_storages_with_scan_state(storages: list[StorageMount], result_scan: ScanRun | None) -> None:
    for storage in storages:
        storage_result_scan = _latest_storage_result_scan(storage)
        storage.latest_counts = _classification_counts(
            FileInventory.objects.filter(scan_run=storage_result_scan, storage=storage)
            if storage_result_scan
            else FileInventory.objects.none()
        )
        storage.latest_file_count = sum(storage.latest_counts.values())
        storage.latest_gate_status = (
            (result_scan.storage_gate_status or {}).get(storage.storage_id, {}) if result_scan else {}
        )
        storage.latest_scan = storage_result_scan
        storage.latest_scan_at = _scan_timestamp(storage_result_scan)
        storage.space_info = common.storage_space_info(storage)
        storage.mount_health = registered_mount_health(storage)
        storage.storage_actions_enabled = storage.mount_health.available and storage.mount_health.writable
        storage.details = storage_details(storage, storage_result_scan, storage.space_info)


def _current_classification_counts(storages: list[StorageMount]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for storage in storages:
        scan = _latest_storage_result_scan(storage)
        if not scan:
            continue
        for classification, count in _classification_counts(
            FileInventory.objects.filter(scan_run=scan, storage=storage)
        ).items():
            totals[classification] = totals.get(classification, 0) + count
    return totals


def _storage_gate_rows(storages: list[StorageMount], result_scan: ScanRun | None) -> list[dict[str, object]]:
    if not result_scan:
        return []

    rows = []
    gate_status = result_scan.storage_gate_status or {}
    for storage in storages:
        rows.append(
            {
                "storage": storage,
                "gate": gate_status.get(storage.storage_id, {}),
                "latest_scan_at": storage.latest_scan_at,
            }
        )
    return rows


def _trash_purge_schedule_state():
    return trash_purge_schedule_state()

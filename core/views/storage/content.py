"""Mount-scoped storage content-type configuration."""

from __future__ import annotations

from core.models import (
    ClusterStorage,
    CurrentGuestInventory,
    ProxmoxCluster,
)
from core.services.storage_catalog import (
    refresh_storage_catalog,
)
from core.services.storage_paths import (
    storage_mount_root,
)

from ...services.storage import StorageScanner
from .. import common
from ..common import (
    FileInventory,
    ProxmoxAPIError,
    ProxmoxInventory,
    ScanRun,
    StorageMount,
    app_login_required,
    datetime,
    ignored_relative_paths_for_storage,
    messages,
    parse_config_value_volid,
    record_audit_event,
    redirect,
    require_POST,
    settings,
    tz,
)
from ._shared import (
    STORAGE_CONTENT_ORDER,
    STORAGE_CONTENT_TYPES,
    _decorate_storage_with_space_info,
    _latest_storage_result_scan,
    _mount_or_404,
    _ordered_storage_content,
    _storage_browser_url,
    _storage_clusters,
    _storage_write_disabled_response,
)


def _cluster_storage_for_mount(storage: StorageMount, cluster: ProxmoxCluster):
    matches = list(
        ClusterStorage.objects.filter(
            cluster=cluster,
            cluster__retired_at__isnull=True,
            unmanaged_at__isnull=True,
            mount_bindings__mount=storage,
            present=True,
        ).distinct()[:2]
    )
    return matches[0] if len(matches) == 1 else None


def _requested_storage_cluster(request, storage: StorageMount):
    clusters = _storage_clusters(storage)
    requested_key = str(request.GET.get("cluster") or request.POST.get("cluster") or "").strip()
    if requested_key:
        return next((cluster for cluster in clusters if cluster.key == requested_key), None), clusters
    return (clusters[0] if len(clusters) == 1 else None), clusters


@require_POST
@app_login_required
def update_storage_content(request, storage_id: str):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    storage = _mount_or_404(storage_id)
    _decorate_storage_with_space_info(storage)
    latest_scan = _latest_storage_result_scan(storage)
    config_scan = latest_scan
    cluster, storage_clusters = _requested_storage_cluster(request, storage)
    if cluster is None:
        messages.error(
            request,
            "Select the Proxmox cluster whose storage configuration should be changed.",
        )
        return redirect("core:storage_content", storage_id=storage.storage_id)
    current_content = _live_storage_content_values(storage, cluster=cluster)
    requested_content = _ordered_storage_content(request.POST.getlist("content"), current_content)
    redirect_to = _storage_browser_url(storage)

    if not requested_content:
        messages.error(request, "Select at least one content type.")
        return redirect(redirect_to)

    try:
        latest_scan = _run_storage_content_preflight_scan(storage)
    except Exception as exc:
        messages.error(request, f"Fresh preflight scan failed; storage content was not changed: {exc}")
        return redirect(redirect_to)

    preflight_errors = _storage_content_preflight_errors(latest_scan, storage)
    if preflight_errors:
        for error in preflight_errors:
            messages.error(request, error)
        return redirect(redirect_to)

    removed = [key for key in current_content if key not in requested_content]
    blockers = _storage_content_blockers(storage, latest_scan, removed)
    if blockers:
        for blocker in blockers:
            examples = ", ".join(blocker["examples"][:3])
            suffix = f" Examples: {examples}." if examples else ""
            messages.error(
                request,
                f"Cannot disable {blocker['label']} because {blocker['count']} existing item"
                f"{'' if blocker['count'] == 1 else 's'} use this storage.{suffix}",
            )
        return redirect(redirect_to)

    updated = False
    err = ""
    for client in common.cluster_scoped_clients(cluster):
        try:
            definition = _cluster_storage_for_mount(storage, cluster)
            client.set_storage_content(
                definition.storage_id if definition else storage.storage_id,
                requested_content,
            )
            updated = True
            err = ""
            break
        except ProxmoxAPIError as exc:
            err = str(exc)
    if not updated:
        if not err:
            err = "No configured Proxmox endpoints."
        messages.error(request, f"Failed to update storage content: {err}")
        return redirect(redirect_to)

    refresh_storage_catalog(cluster)

    _update_latest_storage_config_content(
        storage,
        config_scan,
        requested_content,
        cluster=cluster,
    )
    record_audit_event(
        request,
        action="storage.content.updated",
        object_type="storage",
        object_id=storage.storage_id,
        cluster=cluster,
        details={
            "storage_id": (definition.storage_id if definition else storage.storage_id),
            "storage_name": storage.display_name,
            "old_content": current_content,
            "new_content": requested_content,
        },
    )
    return redirect(redirect_to)


def _run_storage_content_preflight_scan(storage: StorageMount) -> ScanRun:
    now = tz.now()
    scan = ScanRun.objects.create(
        status=ScanRun.Status.RUNNING,
        started_at=now,
        queued_task_id="content-preflight",
        progress_message="Scanning storage content before applying changes.",
        target_storage=storage,
        target_label=storage.display_name,
    )

    scanner = StorageScanner(
        storage.storage_id,
        str(storage_mount_root(storage)),
        ignored_paths=ignored_relative_paths_for_storage(storage),
    )
    rows = [
        FileInventory(
            scan_run=scan,
            storage=storage,
            path=entry.relative_path,
            derived_volid=entry.derived_volid,
            content_category=entry.content_category,
            entry_type=entry.entry_type,
            size_bytes=entry.size_bytes,
            modified_at=_storage_content_preflight_timestamp(entry.modified_at),
        )
        for entry in scanner.iter_entries()
    ]
    FileInventory.objects.bulk_create(rows, batch_size=1000)

    scan.status = ScanRun.Status.COMPLETED
    scan.finished_at = tz.now()
    scan.filesystem_scan_at = scan.finished_at
    scan.summary_counts = {"files": len(rows), "proxmox_objects": 0, "classifications": {}}
    scan.error_details = {"storage": {storage.storage_id: {"errors": scanner.errors}}} if scanner.errors else {}
    scan.progress_message = (
        f"Content preflight scan completed with {len(scanner.errors)} warning(s)."
        if scanner.errors
        else "Content preflight scan completed."
    )
    scan.save(
        update_fields=[
            "status",
            "finished_at",
            "filesystem_scan_at",
            "summary_counts",
            "error_details",
            "progress_message",
            "updated_at",
        ]
    )
    return scan


def _storage_content_preflight_timestamp(value: float | None):
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=tz.get_current_timezone())


def _storage_content_preflight_errors(scan: ScanRun | None, storage: StorageMount) -> list[str]:
    if scan is None:
        return ["Fresh preflight scan did not complete; storage content was not changed."]
    if scan.status != ScanRun.Status.COMPLETED:
        return ["Fresh preflight scan did not complete; storage content was not changed."]

    details = scan.error_details if isinstance(scan.error_details, dict) else {}
    errors: list[str] = []
    if details.get("proxmox"):
        errors.append("Fresh preflight scan could not read all Proxmox inventory; storage content was not changed.")
    storage_errors = (details.get("storage") or {}).get(storage.storage_id)
    if storage_errors:
        errors.append("Fresh preflight scan could not read all files on this storage; storage content was not changed.")
    return errors


def _storage_content_values(storage: StorageMount, *, cluster=None) -> list[str]:
    if cluster is not None:
        definition = ClusterStorage.objects.filter(
            cluster=cluster,
            cluster__retired_at__isnull=True,
            unmanaged_at__isnull=True,
            mount_bindings__mount=storage,
            present=True,
        ).first()
        if definition is not None:
            return list(definition.content)
    content = getattr(getattr(storage, "details", None), "content", "") or ""
    return [part.strip() for part in str(content).split(",") if part.strip()]


def _live_storage_content_values(storage: StorageMount, *, cluster) -> list[str]:
    return _storage_content_values(storage, cluster=cluster)


def _storage_content_blockers(storage: StorageMount, latest_scan: ScanRun | None, removed: list[str]) -> list[dict]:
    if not removed:
        return []
    usage = _storage_content_usage(storage, latest_scan)
    labels = {item["key"]: item["label"] for item in STORAGE_CONTENT_TYPES}
    return [
        {
            "key": key,
            "label": labels.get(key, key),
            "count": usage.get(key, {"count": 0, "examples": []})["count"],
            "examples": usage.get(key, {"count": 0, "examples": []})["examples"],
        }
        for key in removed
        if usage.get(key, {"count": 0})["count"] > 0
    ]


def _storage_content_usage(storage: StorageMount, latest_scan: ScanRun | None) -> dict[str, dict]:
    usage = {key: {"items": set(), "examples": []} for key in STORAGE_CONTENT_ORDER}
    if latest_scan is None:
        return _finalize_storage_content_usage(usage)

    category_map = {
        "images": {"vm_disk", "base_image"},
        "iso": {"iso"},
        "vztmpl": {"ct_template"},
        "backup": {"backup"},
        "rootdir": {"ct_private"},
        "snippets": {"snippet", "snippets"},
    }
    for key, categories in category_map.items():
        entries = (
            FileInventory.objects.filter(
                scan_run=latest_scan,
                storage=storage,
                entry_type=FileInventory.EntryType.FILE,
                content_category__in=categories,
            )
            .order_by("path")
            .values_list("path", flat=True)[:1000]
        )
        for path in entries:
            _add_storage_content_usage(usage, key, path, path)

    inventory = CurrentGuestInventory.objects.all().order_by("node", "object_type", "vmid")
    for obj in inventory:
        if not isinstance(obj.config, dict):
            continue
        for key, value in _iter_config_strings(obj.config):
            volid = parse_config_value_volid(value)
            if not volid.startswith(f"{storage.storage_id}:"):
                continue
            content_key = _content_type_for_config_reference(obj, key, value, volid)
            if content_key not in usage:
                continue
            label = _guest_reference_label(obj, key)
            _add_storage_content_usage(
                usage, content_key, f"{obj.node}:{obj.object_type}:{obj.vmid}:{key}:{volid}", label
            )

    return _finalize_storage_content_usage(usage)


def _iter_config_strings(value, key: str = ""):
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            yield from _iter_config_strings(child_value, str(child_key))
        return
    if isinstance(value, list):
        for child_value in value:
            yield from _iter_config_strings(child_value, key)
        return
    if isinstance(value, str):
        yield key, value


def _content_type_for_config_reference(obj: ProxmoxInventory, key: str, value: str, volid: str) -> str:
    relative = volid.split(":", 1)[1] if ":" in volid else ""
    if relative.startswith("snippets/"):
        return "snippets"
    if relative.startswith("template/iso/") or "media=cdrom" in value:
        return "iso"
    if relative.startswith("template/cache/"):
        return "vztmpl"
    if obj.object_type == ProxmoxInventory.ObjectType.CT:
        return "rootdir"
    if key.startswith(("ide", "sata", "scsi", "virtio", "efidisk", "tpmstate", "unused")):
        return "images"
    return ""


def _guest_reference_label(obj: ProxmoxInventory, key: str) -> str:
    name = obj.name or f"{obj.object_type.upper()} {obj.vmid}"
    node = f" on {obj.node}" if obj.node else ""
    return f"{name}{node} ({key})"


def _add_storage_content_usage(usage: dict[str, dict], key: str, item: str, example: str) -> None:
    bucket = usage.setdefault(key, {"items": set(), "examples": []})
    if item in bucket["items"]:
        return
    bucket["items"].add(item)
    if len(bucket["examples"]) < 10:
        bucket["examples"].append(example)


def _finalize_storage_content_usage(usage: dict[str, dict]) -> dict[str, dict]:
    return {
        key: {
            "count": len(bucket["items"]),
            "examples": list(bucket["examples"]),
        }
        for key, bucket in usage.items()
    }


def _update_latest_storage_config_content(
    storage: StorageMount,
    latest_scan: ScanRun | None,
    content: list[str],
    *,
    cluster,
) -> None:
    if latest_scan is None:
        return
    for obj in ProxmoxInventory.objects.filter(
        scan_run=latest_scan,
        cluster=cluster,
        object_type=ProxmoxInventory.ObjectType.STORAGE,
        name=storage.storage_id,
    ):
        config = dict(obj.config or {})
        config["content"] = ",".join(content)
        obj.config = config
        obj.save(update_fields=["config", "updated_at"])

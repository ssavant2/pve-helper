"""Read model for the datastore file browser."""

from __future__ import annotations

from core.models import (
    CurrentGuestInventory,
)
from core.services.cluster_scopes import managed_clusters

from ..common import (
    FILE_BROWSER_BATCH_SIZE,
    FileInventory,
    Http404,
    JsonResponse,
    ScanRun,
    StorageMount,
    ignored_relative_paths_for_storage,
    is_ignored_storage_path,
    render_to_string,
    reverse,
)
from ._shared import (
    _decorate_browser_entry,
    _decorate_storage_with_space_info,
    _int_request_param,
    _latest_storage_result_scan,
    _lineage_by_cluster,
    _normalize_browser_path,
    _parent_path,
    _storage_browser_url,
    _storage_clusters,
)


def _storage_browser_context(request, storage):
    """The file manager's context for one mount, shared by both pages it appears on.

    Returns a `JsonResponse` instead when the request is the incremental row fetch,
    because paging must answer identically wherever the browser is embedded.
    """
    _decorate_storage_with_space_info(storage)
    latest_scan = _latest_storage_result_scan(storage)
    current_path = _normalize_browser_path(request.GET.get("path", ""))
    parent_path = _parent_path(current_path)
    file_query = request.GET.get("q", "").strip()[:200]
    file_offset = max(0, _int_request_param(request, "file_offset", 0))
    file_partial = request.GET.get("file_partial") == "1"
    entries = []
    current_entry = None
    folder_tree = []

    if latest_scan:
        ignored_paths = ignored_relative_paths_for_storage(storage)
        if current_path:
            if is_ignored_storage_path(current_path, ignored_paths):
                raise Http404("Directory not found in latest scan.")
            current_entry = FileInventory.objects.filter(
                scan_run=latest_scan,
                storage=storage,
                path=current_path,
                entry_type=FileInventory.EntryType.DIRECTORY,
            ).first()
            if current_entry is None:
                raise Http404("Directory not found in latest scan.")

        candidates = FileInventory.objects.filter(scan_run=latest_scan, storage=storage)
        if current_path:
            candidates = candidates.filter(path__startswith=f"{current_path}/")

        prefix = f"{current_path}/" if current_path else ""
        for entry in candidates:
            if is_ignored_storage_path(entry.path, ignored_paths):
                continue
            remainder = entry.path[len(prefix) :] if prefix else entry.path
            if not remainder or "/" in remainder:
                continue
            entry.name = remainder
            _decorate_browser_entry(entry)
            entries.append(entry)
        folder_tree = _browser_folder_tree(latest_scan, storage, current_path, ignored_paths=ignored_paths)

    entries.sort(key=lambda item: (item.entry_type != FileInventory.EntryType.DIRECTORY, item.name.lower()))
    if file_query:
        query = file_query.lower()
        entries = [
            entry
            for entry in entries
            if query
            in " ".join(
                [
                    entry.name.lower(),
                    entry.path.lower(),
                    (entry.content_category or "").lower(),
                    (entry.classification or "").lower(),
                    getattr(entry, "classification_label", "").lower(),
                    getattr(entry, "category_label", "").lower(),
                ]
            )
        ]

    file_total = len(entries)
    entries = entries[file_offset : file_offset + FILE_BROWSER_BATCH_SIZE]

    # Link each referenced disk image to the current VM/CT that owns it.
    from core.services.classification import extract_vmid_from_image_path

    # Resolve guest links inside the clusters that actually consume this mount.
    # A VMID is unique per cluster, never globally, so searching every cluster
    # turns two unrelated `vm:500`s into an ambiguity and drops the link entirely.
    # A mount nobody has bound yet has no cluster to narrow to; there the old wide
    # search is the only thing left, and the ambiguity rule below still applies.
    link_clusters = _storage_clusters(storage) or list(
        managed_clusters().filter(enabled=True).order_by("display_name", "key")
    )
    guests_by_vmid: dict[int, list[CurrentGuestInventory]] = {}
    for obj in CurrentGuestInventory.objects.select_related("cluster").filter(cluster__in=link_clusters):
        guests_by_vmid.setdefault(obj.vmid, []).append(obj)

    def _unique_guest(vmid: int) -> CurrentGuestInventory | None:
        matches = guests_by_vmid.get(vmid, [])
        return matches[0] if len(matches) == 1 else None

    # Linked-clone lineage: which template each clone descends from, and how many
    # clones each template's base volume backs. Cached live fetch; empty if the
    # API is unreachable, so the browser degrades to plain classification.
    import re
    from collections import Counter

    lineage_by_cluster = _lineage_by_cluster(link_clusters)
    clone_counts_by_cluster = {key: Counter(lineage.values()) for key, lineage in lineage_by_cluster.items()}

    def _clone_count(vmid: int) -> int | None:
        """How many linked clones ride this template's base volume, or None where
        that cannot be told apart from another cluster's answer.

        Summing the clusters would report one cluster's clones against another's
        template, which is worse than admitting we do not know: the count is what
        an operator reads before deciding the volume is safe to remove.
        """
        guest = _unique_guest(vmid)
        if guest is not None:
            return clone_counts_by_cluster.get(guest.cluster.key, Counter()).get(vmid, 0)
        if len(link_clusters) == 1:
            # The template itself is gone from inventory, but only one cluster can
            # own this volume, so its lineage is still the whole answer.
            return clone_counts_by_cluster.get(link_clusters[0].key, Counter()).get(vmid, 0)
        return None

    base_volume_re = re.compile(r"base-(\d+)-disk-")

    def _template_link(vmid: int) -> dict:
        guest = _unique_guest(vmid)
        return {
            "vmid": vmid,
            "name": guest.name if guest and guest.name else f"VM {vmid}",
            "url": (
                reverse(
                    "core:guest_summary",
                    args=[guest.cluster.key, guest.object_type, guest.vmid],
                )
                if guest and guest.cluster_id
                else ""
            ),
            "guest_ref": guest.guest_ref().serialize() if guest and guest.guest_ref() else "",
        }

    for entry in entries:
        entry.referenced_guest = None
        entry.template_base = None
        if entry.entry_type != FileInventory.EntryType.FILE:
            continue
        # A template's base volume (base-<vmid>-disk-*), shared read-only by every
        # linked clone. Surface which template owns it and how many clones ride it.
        base_match = base_volume_re.search(entry.name)
        if base_match:
            tmpl_vmid = int(base_match.group(1))
            entry.template_base = {
                **_template_link(tmpl_vmid),
                "clone_count": _clone_count(tmpl_vmid),
            }
        if entry.classification == FileInventory.Classification.REFERENCED:
            owner_vmid = extract_vmid_from_image_path(entry.path)
            guest = _unique_guest(owner_vmid or -1)
            if guest is not None:
                entry.referenced_guest = {
                    "name": guest.name or f"VM {guest.vmid}",
                    "url": reverse(
                        "core:guest_summary",
                        args=[guest.cluster.key, guest.object_type, guest.vmid],
                    ),
                    "guest_ref": guest.guest_ref().serialize() if guest.guest_ref() else "",
                    # If this disk belongs to a linked clone, name its base template.
                    "linked_clone_of": _template_link(lineage_by_cluster.get(guest.cluster.key, {})[owner_vmid])
                    if owner_vmid in lineage_by_cluster.get(guest.cluster.key, {})
                    else None,
                }

    file_next_offset = file_offset + FILE_BROWSER_BATCH_SIZE
    file_has_next = file_next_offset < file_total
    file_next_url = (
        _storage_browser_url(
            storage,
            current_path,
            q=file_query,
            file_offset=file_next_offset,
        )
        if file_has_next
        else ""
    )
    restore_clusters = {
        binding.cluster_storage.cluster.key: {
            "key": binding.cluster_storage.cluster.key,
            "display_name": binding.cluster_storage.cluster.display_name,
            "storage_id": binding.cluster_storage.storage_id,
        }
        for binding in storage.cluster_bindings.select_related("cluster_storage__cluster").filter(
            cluster_storage__cluster__enabled=True,
            cluster_storage__cluster__retired_at__isnull=True,
            cluster_storage__unmanaged_at__isnull=True,
            cluster_storage__present=True,
        )
    }
    storage.backup_restore_clusters = list(restore_clusters.values())

    context = {
        "mount": storage,
        "latest_scan": latest_scan,
        # The file manager's folder tree, breadcrumbs and parent row all link back
        # into itself. They go through this rather than reversing a mount-keyed
        # route, so the browser stays embeddable on the datastore page.
        "files_base_url": _storage_browser_url(storage),
        "current_path": current_path,
        "parent_path": parent_path,
        "breadcrumbs": _browser_breadcrumbs(current_path),
        "folder_tree": folder_tree,
        "dest_storages": StorageMount.objects.filter(enabled=True).order_by("display_name"),
        "entries": entries,
        "current_entry": current_entry,
        "file_query": file_query,
        "file_offset": file_offset,
        "file_batch_size": FILE_BROWSER_BATCH_SIZE,
        "file_total": file_total,
        "file_start": min(file_offset + 1, file_total),
        "file_end": min(file_offset + len(entries), file_total),
        "file_has_next": file_has_next,
        "file_next_url": file_next_url,
        "include_parent_row": current_path and file_offset == 0,
    }
    if file_partial:
        include_parent_in_partial = request.GET.get("include_parent") == "1" and current_path and file_offset == 0
        return JsonResponse(
            {
                "rows_html": render_to_string(
                    "core/partials/storage_file_rows.html",
                    {**context, "include_parent_row": include_parent_in_partial},
                    request=request,
                ),
                "has_next": file_has_next,
                "next_url": file_next_url,
                "total": file_total,
                "end": context["file_end"],
            }
        )
    return context


def _browser_breadcrumbs(path: str) -> list[dict[str, str]]:
    breadcrumbs = [{"label": "Root", "path": ""}]
    if not path:
        return breadcrumbs

    current = []
    for part in path.split("/"):
        current.append(part)
        breadcrumbs.append({"label": part, "path": "/".join(current)})
    return breadcrumbs


def _browser_folder_tree(
    scan: ScanRun,
    storage: StorageMount,
    current_path: str,
    *,
    ignored_paths: set[str] | None = None,
) -> list[dict[str, object]]:
    ignored_paths = ignored_paths or set()
    directory_paths = sorted(
        set(
            path
            for path in (
                FileInventory.objects.filter(
                    scan_run=scan,
                    storage=storage,
                    entry_type=FileInventory.EntryType.DIRECTORY,
                )
                .order_by("path")
                .values_list("path", flat=True)
            )
            if not is_ignored_storage_path(path, ignored_paths)
        ),
        key=lambda item: [part.lower() for part in item.split("/")],
    )
    directory_path_set = set(directory_paths)
    expanded_paths = {""}
    if current_path:
        current_parts = current_path.split("/")
        expanded_paths.update("/".join(current_parts[:index]) for index in range(1, len(current_parts) + 1))

    def has_children(path: str) -> bool:
        if not path:
            return bool(directory_paths)
        return any(candidate.startswith(f"{path}/") for candidate in directory_path_set)

    def is_initially_visible(path: str) -> bool:
        if not path:
            return True
        parts = path.split("/")
        return all("/".join(parts[:index]) in expanded_paths for index in range(0, len(parts)))

    nodes = [
        {
            "name": storage.display_name,
            "path": "",
            "depth": 0,
            "is_current": current_path == "",
            "is_ancestor": bool(current_path),
            "is_expanded": "" in expanded_paths,
            "is_initially_visible": True,
            "has_children": has_children(""),
        }
    ]
    for path in directory_paths:
        parts = path.split("/")
        nodes.append(
            {
                "name": parts[-1],
                "path": path,
                "depth": len(parts),
                "is_current": path == current_path,
                "is_ancestor": bool(current_path) and current_path.startswith(f"{path}/"),
                "is_expanded": path in expanded_paths,
                "is_initially_visible": is_initially_visible(path),
                "has_children": has_children(path),
            }
        )
    return nodes

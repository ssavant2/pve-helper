"""Orphan Finder and classified-file views."""

from __future__ import annotations

from pathlib import PurePosixPath

from django.db.models import QuerySet

from core.services.storage_mounts import (
    resolve_storage_mount,
)

from ..common import (
    FileInventory,
    Q,
    StorageMount,
    _latest_result_scan,
    app_login_required,
    messages,
    navigation_context,
    redirect,
    render,
    urlencode,
)
from ._shared import (
    _content_category_label,
    _decorate_browser_entry,
    _decorate_storage_with_space_info,
    _latest_storage_result_scan,
    _storage_browser_url,
)


@app_login_required
def orphan_finder(request):
    page = _classified_files_page(
        request,
        FileInventory.Classification.LIKELY_ORPHAN,
        StorageMount.objects.filter(enabled=True).order_by("display_name"),
        query={},
    )
    _decorate_orphan_files_with_action_state(page["files"])
    context = {
        **navigation_context("orphans"),
        "latest_scan": _latest_result_scan(),
        **page,
    }
    return render(request, "core/orphan_finder.html", context)


@app_login_required
def classified_files(request):
    """Drill-down list behind the dashboard classification counters: every file
    with the requested classification across the enabled storages."""
    classification = request.GET.get("classification", "")
    if classification not in FileInventory.Classification.values:
        messages.error(request, "Unknown classification.")
        return redirect("core:dashboard")
    # Likely orphans have their own workspace (register / trash actions).
    if classification == FileInventory.Classification.LIKELY_ORPHAN:
        return redirect("core:orphan_finder")

    storage_id = request.GET.get("storage", "").strip()
    storages = StorageMount.objects.filter(enabled=True).order_by("display_name")
    if storage_id:
        try:
            storages = [resolve_storage_mount(storage_id, enabled=True)]
        except StorageMount.DoesNotExist:
            storages = []

    query = {"classification": classification}
    if storage_id:
        query["storage"] = storage_id
    page = _classified_files_page(request, classification, storages, query=query)
    for entry in page["files"]:
        entry.category_label = _content_category_label(entry.content_category, entry.path)
        entry.browser_url = _browser_url_for_file(entry)

    context = {
        # Shares the Orphan Finder's navigation key, so the classification is what
        # distinguishes one of these tabs from another.
        **navigation_context("orphans", page_title=FileInventory.Classification(classification).label),
        "latest_scan": _latest_result_scan(),
        "classification_value": classification,
        "classification_label": FileInventory.Classification(classification).label,
        **page,
    }
    return render(request, "core/classified_files.html", context)


def _browser_url_for_file(entry: FileInventory) -> str:
    """Link to the storage browser opened at the file's containing folder."""
    url = _storage_browser_url(entry.storage)
    parent = PurePosixPath(entry.path).parent
    parent_str = "" if str(parent) in (".", "") else str(parent)
    if parent_str:
        url = f"{url}?{urlencode({'path': parent_str})}"
    return url


_FILE_PAGE_SIZE = 200


def _classified_files_queryset(classification: str, storages) -> QuerySet[FileInventory]:
    """Every file with this classification, across the latest scan of each storage.

    One query, not one per storage. The `(scan, storage)` pairing has to survive
    into the filter: a scan with `target_storage=NULL` is a whole-fleet scan and
    is the latest result for several storages at once, so neither a plain
    `scan_run__in` nor a plain `storage__in` describes the set — only the pairs
    do.

    Returned unsliced and ordered, so the caller can count and page in the
    database. The previous shape took 200 rows per storage and re-sliced the
    concatenation to 200, which silently dropped files that sorted late within
    their own storage: the result was neither the first 200 nor all of them.
    """
    pairs = []
    for storage in storages:
        scan = _latest_storage_result_scan(storage)
        if scan:
            pairs.append(Q(scan_run=scan, storage=storage))
    if not pairs:
        return FileInventory.objects.none()
    latest_scans = pairs[0]
    for pair in pairs[1:]:
        latest_scans |= pair
    return (
        FileInventory.objects.select_related("storage", "scan_run")
        .filter(latest_scans, classification=classification)
        .order_by("storage__display_name", "path")
    )


def _classified_files_page(request, classification: str, storages, query: dict[str, str]) -> dict[str, object]:
    """Context for one page of a classification list.

    Shared by the Orphan Finder and the generic classification drill-down so the
    two cannot disagree about page size, bounds or the "N-M of TOTAL" they show.
    Orphan Finder shipped with no pagination at all while its sibling had all of
    it, three functions away; a review queue that silently ends at 200 cannot
    tell an operator whether the work is done.

    `query` carries the view's own GET parameters into the prev/next links.
    """
    queryset = _classified_files_queryset(classification, storages)
    total = queryset.count()
    try:
        page = max(0, int(request.GET.get("page", "0")))
    except ValueError:
        page = 0
    page = min(page, max(0, (total - 1) // _FILE_PAGE_SIZE) if total else 0)
    start = page * _FILE_PAGE_SIZE
    return {
        "files": list(queryset[start : start + _FILE_PAGE_SIZE]),
        "total": total,
        "page": page,
        "has_prev": page > 0,
        "has_next": start + _FILE_PAGE_SIZE < total,
        "start_index": start + 1 if total else 0,
        "end_index": min(start + _FILE_PAGE_SIZE, total),
        "prev_query": urlencode({**query, "page": page - 1}),
        "next_query": urlencode({**query, "page": page + 1}),
    }


def _decorate_orphan_files_with_action_state(files: list[FileInventory]) -> None:
    storages: dict[int, StorageMount] = {}
    for file in files:
        if file.storage_id not in storages:
            _decorate_storage_with_space_info(file.storage)
            storages[file.storage_id] = file.storage
        file.storage = storages[file.storage_id]
        _decorate_browser_entry(file)

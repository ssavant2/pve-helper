"""Destructive and write file actions on a mounted datastore."""

from __future__ import annotations

from pathlib import PurePosixPath

from core.services.storage_catalog import (
    StorageOperationScope,
)
from core.services.storage_mounts import (
    resolve_storage_mount,
)

from .. import common
from ..common import (
    INFLATE_PREALLOCATION_FULL,
    INFLATE_PREALLOCATION_MODES,
    FileActionRisk,
    FileInventory,
    Http404,
    JsonResponse,
    PermissionDenied,
    Q,
    ScanRun,
    StorageActionError,
    StorageMount,
    StorageOperationAborted,
    _safe_next_url,
    app_login_required,
    create_storage_directory,
    file_action_risk,
    get_object_or_404,
    json_task_response,
    messages,
    move_file_to_trash,
    move_storage_file,
    public_storage_upload_error,
    record_audit_event,
    redirect,
    rename_storage_file,
    require_POST,
    settings,
    transfer_storage_file,
    upload_folder_to_storage,
    upload_to_storage,
    validate_inflate_storage_file,
)
from ._shared import (
    _audit_file_action,
    _audit_file_action_failure,
    _clusters_for_mounts,
    _latest_storage_result_scan,
    _lineage_by_cluster,
    _mount_or_404,
    _normalize_browser_path,
    _parent_path,
    _refresh_latest_storage_directory,
    _storage_browser_url,
    _storage_write_disabled_response,
)


@require_POST
@app_login_required
@json_task_response
def create_storage_folder(request, storage_id: str):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    storage = _mount_or_404(storage_id)
    current_path = _normalize_browser_path(request.POST.get("path", ""))
    redirect_to = _safe_next_url(request) or _storage_browser_url(storage, current_path)
    latest_scan = _latest_storage_result_scan(storage)
    if current_path:
        _storage_directory_or_404(storage, latest_scan, current_path)

    try:
        result = create_storage_directory(
            storage=storage,
            directory_path=current_path,
            folder_name=request.POST.get("folder_name", ""),
        )
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        _audit_file_action_failure(
            request,
            action="file.folder_created",
            storage=storage,
            path=_join_browser_path(current_path, request.POST.get("folder_name", "")),
            exc=exc,
        )
        messages.error(request, str(exc))
        return redirect(redirect_to)

    _audit_file_action(
        request,
        action="file.folder_created",
        storage=storage,
        path=str(result["path"]),
        details={"directory_path": result["directory_path"]},
    )
    _refresh_latest_storage_directory(storage, str(result["directory_path"]))
    return redirect(redirect_to)


@require_POST
@app_login_required
def upload_storage_file(request, storage_id: str):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    storage = _mount_or_404(storage_id)
    current_path = _normalize_browser_path(request.POST.get("path", ""))
    redirect_to = _safe_next_url(request) or _storage_browser_url(storage, current_path)
    uploaded_file = request.FILES.get("file")
    if uploaded_file is None:
        return _upload_error_response(request, redirect_to, "No upload file selected.")

    latest_scan = _latest_storage_result_scan(storage)
    if current_path:
        _storage_directory_or_404(storage, latest_scan, current_path)

    try:
        result = upload_to_storage(
            storage=storage,
            directory_path=current_path,
            uploaded_file=uploaded_file,
        )
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        _audit_file_action_failure(
            request,
            action="file.uploaded",
            storage=storage,
            path=_join_browser_path(current_path, uploaded_file.name),
            exc=exc,
            details={"error": public_storage_upload_error(exc)},
        )
        return _upload_error_response(request, redirect_to, public_storage_upload_error(exc))

    _audit_file_action(
        request,
        action="file.uploaded",
        storage=storage,
        path=str(result["path"]),
        details={"size_bytes": result["size_bytes"]},
    )
    _queue_upload_normalization(storage, [str(result["path"])], request.user)
    _refresh_latest_storage_directory(storage, current_path)
    return _upload_success_response(request, redirect_to)


@require_POST
@app_login_required
def upload_storage_folder(request, storage_id: str):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    storage = _mount_or_404(storage_id)
    current_path = _normalize_browser_path(request.POST.get("path", ""))
    redirect_to = _safe_next_url(request) or _storage_browser_url(storage, current_path)
    uploaded_files = request.FILES.getlist("files")
    relative_paths = request.POST.getlist("relative_path")
    if not uploaded_files:
        return _upload_error_response(request, redirect_to, "No upload files selected.")
    if not relative_paths:
        relative_paths = [uploaded_file.name for uploaded_file in uploaded_files]

    latest_scan = _latest_storage_result_scan(storage)
    if current_path:
        _storage_directory_or_404(storage, latest_scan, current_path)

    try:
        result = upload_folder_to_storage(
            storage=storage,
            directory_path=current_path,
            uploaded_files=uploaded_files,
            relative_paths=relative_paths,
        )
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        _audit_file_action_failure(
            request,
            action="file.folder_uploaded",
            storage=storage,
            path=current_path or "/",
            exc=exc,
            details={"error": public_storage_upload_error(exc)},
        )
        return _upload_error_response(request, redirect_to, public_storage_upload_error(exc))

    _audit_file_action(
        request,
        action="file.folder_uploaded",
        storage=storage,
        path=current_path or "/",
        details={
            "file_count": result["file_count"],
            "size_bytes": result["size_bytes"],
            "directory_path": result["directory_path"],
        },
    )
    _queue_upload_normalization(storage, [str(path) for path in result["paths"]], request.user)
    for directory_path in result["directory_paths"]:
        _refresh_latest_storage_directory(storage, str(directory_path))
    return _upload_success_response(request, redirect_to)


@require_POST
@app_login_required
@json_task_response
def trash_storage_file(request, storage_id: str):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    storage = _mount_or_404(storage_id)
    redirect_to = _safe_next_url(request)
    latest_scan = _latest_storage_result_scan(storage)
    entries = _selected_storage_file_entries(
        request,
        storage=storage,
        latest_scan=latest_scan,
        entry_types=[FileInventory.EntryType.FILE, FileInventory.EntryType.DIRECTORY],
    )

    risks = [file_action_risk(entry) for entry in entries]
    try:
        _require_file_action_confirmations_for_entries(request, entries)
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        # A whole-selection precondition; nothing was attempted.
        for entry in entries:
            _audit_file_action_failure(request, action="file.trashed", storage=storage, path=entry.path, exc=exc)
        messages.error(request, str(exc))
        return redirect(redirect_to)

    acknowledged_risk = _risk_acknowledged_for_risks(request, risks)
    scope = StorageOperationScope()
    outcome = BulkFileOutcome()
    refresh_directories = set()
    pruned_paths = set()
    for index, entry in enumerate(entries):
        try:
            trash_item = move_file_to_trash(
                storage=storage,
                entry=entry,
                user=request.user,
                scope=scope,
                acknowledged_risk=acknowledged_risk,
            )
        except StorageActionError as exc:
            _audit_file_action_failure(request, action="file.trashed", storage=storage, path=entry.path, exc=exc)
            outcome.record_failure(entry, exc, remaining=entries[index + 1 :])
            if outcome.aborted:
                break
            continue
        # Audit each success as it happens: a later failure must never be able to
        # erase the record of what has already been done on disk.
        _audit_file_action(
            request,
            action="file.trashed",
            storage=storage,
            path=entry.path,
            details={"trash_item": trash_item.id, "trash_path": trash_item.trash_path},
        )
        outcome.record_success(entry)
        if entry.entry_type == FileInventory.EntryType.DIRECTORY:
            pruned_paths.add(entry.path)
        refresh_directories.add(_parent_path(entry.path))
    for path in pruned_paths:
        _prune_latest_storage_path(storage, path)
    for directory_path in refresh_directories:
        _refresh_latest_storage_directory(storage, directory_path)
    _report_bulk_file_outcome(
        request,
        outcome,
        storage=storage,
        operation="trash",
        verb="moved to trash",
        destructive=True,
    )
    return redirect(redirect_to)


@require_POST
@app_login_required
@json_task_response
def move_storage_file_view(request, storage_id: str):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    storage = _mount_or_404(storage_id)
    redirect_to = _safe_next_url(request)
    latest_scan = _latest_storage_result_scan(storage)
    entries = _selected_storage_file_entries(request, storage=storage, latest_scan=latest_scan)

    dest_storage_id = request.POST.get("dest_storage", "").strip()
    dest_storage = storage
    if dest_storage_id:
        try:
            dest_storage = resolve_storage_mount(dest_storage_id, enabled=True)
        except StorageMount.DoesNotExist:
            messages.error(request, "Unknown destination storage.")
            return redirect(redirect_to)

    risks = [file_action_risk(entry) for entry in entries]
    try:
        _require_file_action_confirmations_for_entries(request, entries)
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        for entry in entries:
            _audit_file_action_failure(request, action="file.moved", storage=storage, path=entry.path, exc=exc)
        messages.error(request, str(exc))
        return redirect(redirect_to)

    acknowledged_risk = _risk_acknowledged_for_risks(request, risks)
    scope = StorageOperationScope()
    outcome = BulkFileOutcome()
    dest_directory = request.POST.get("dest_directory", "")
    refresh: dict[tuple[str, str], tuple[StorageMount, str]] = {}
    for index, entry in enumerate(entries):
        try:
            if dest_storage_id:
                result = transfer_storage_file(
                    source_storage=storage,
                    entry=entry,
                    dest_storage=dest_storage,
                    dest_directory=dest_directory,
                    keep_source=False,
                    scope=scope,
                    acknowledged_risk=acknowledged_risk,
                )
            else:
                result = move_storage_file(
                    storage=storage,
                    entry=entry,
                    new_path=request.POST.get("new_path", ""),
                    scope=scope,
                    acknowledged_risk=acknowledged_risk,
                )
        except StorageActionError as exc:
            _audit_file_action_failure(request, action="file.moved", storage=storage, path=entry.path, exc=exc)
            outcome.record_failure(entry, exc, remaining=entries[index + 1 :])
            if outcome.aborted:
                break
            continue
        outcome.record_success(entry)
        if dest_storage_id:
            _audit_file_action(
                request,
                action="file.moved",
                storage=dest_storage,
                path=str(result["dest_path"]),
                details={"old_path": result["source_path"], "source_storage": storage.storage_id},
            )
            refresh[(storage.storage_id, str(result["source_directory_path"]))] = (
                storage,
                str(result["source_directory_path"]),
            )
            dest_dir = str(result["dest_directory_path"])
            refresh[(dest_storage.storage_id, dest_dir)] = (dest_storage, dest_dir)
            dest_parent = dest_dir.rsplit("/", 1)[0] if "/" in dest_dir else ""
            refresh[(dest_storage.storage_id, dest_parent)] = (dest_storage, dest_parent)
        else:
            _audit_file_action(
                request,
                action="file.moved",
                storage=storage,
                path=str(result["new_path"]),
                details={"old_path": result["old_path"]},
            )
            refresh[(storage.storage_id, str(result["source_directory_path"]))] = (
                storage,
                str(result["source_directory_path"]),
            )
            refresh[(storage.storage_id, str(result["target_directory_path"]))] = (
                storage,
                str(result["target_directory_path"]),
            )
    for st, directory_path in refresh.values():
        _refresh_latest_storage_directory(st, directory_path)
    _report_bulk_file_outcome(
        request,
        outcome,
        storage=storage,
        operation="move",
        verb="moved",
        destructive=True,
    )
    return redirect(redirect_to)


@require_POST
@app_login_required
@json_task_response
def copy_storage_file_view(request, storage_id: str):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    storage = _mount_or_404(storage_id)
    redirect_to = _safe_next_url(request)
    latest_scan = _latest_storage_result_scan(storage)
    requested_path = _normalize_browser_path(request.POST.get("path", ""))
    entry = get_object_or_404(
        FileInventory,
        scan_run=latest_scan,
        storage=storage,
        path=requested_path,
        entry_type=FileInventory.EntryType.FILE,
    )
    try:
        dest_storage = resolve_storage_mount(request.POST.get("dest_storage", "").strip(), enabled=True)
    except StorageMount.DoesNotExist:
        messages.error(request, "Unknown destination storage.")
        return redirect(redirect_to)

    try:
        result = transfer_storage_file(
            source_storage=storage,
            entry=entry,
            dest_storage=dest_storage,
            dest_directory=request.POST.get("dest_directory", ""),
            dest_name=request.POST.get("dest_name", ""),
            keep_source=True,
        )
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        _audit_file_action_failure(request, action="file.copied", storage=storage, path=entry.path, exc=exc)
        messages.error(request, str(exc))
        return redirect(redirect_to)

    _audit_file_action(
        request,
        action="file.copied",
        storage=dest_storage,
        path=str(result["dest_path"]),
        details={"source_storage": storage.storage_id, "source_path": result["source_path"]},
    )
    dest_directory = str(result["dest_directory_path"])
    _refresh_latest_storage_directory(dest_storage, dest_directory)
    # Also refresh the parent so a newly created destination folder shows up.
    if "/" in dest_directory or dest_directory:
        _refresh_latest_storage_directory(
            dest_storage, dest_directory.rsplit("/", 1)[0] if "/" in dest_directory else ""
        )
    # No success toast — the outcome is recorded as file.copied in the audit log.
    return redirect(redirect_to)


@require_POST
@app_login_required
@json_task_response
def rename_storage_file_view(request, storage_id: str):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    storage = _mount_or_404(storage_id)
    redirect_to = _safe_next_url(request)
    latest_scan = _latest_storage_result_scan(storage)
    requested_path = _normalize_browser_path(request.POST.get("path", ""))
    entry = get_object_or_404(
        FileInventory,
        scan_run=latest_scan,
        storage=storage,
        path=requested_path,
        entry_type=FileInventory.EntryType.FILE,
    )
    risk = file_action_risk(entry)

    try:
        _require_file_action_confirmations(request, risk)
        result = rename_storage_file(
            storage=storage,
            entry=entry,
            new_name=request.POST.get("new_name", ""),
            acknowledged_risk=_risk_acknowledged(request, risk),
        )
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        _audit_file_action_failure(request, action="file.renamed", storage=storage, path=entry.path, exc=exc)
        messages.error(request, str(exc))
        return redirect(redirect_to)

    _audit_file_action(
        request,
        action="file.renamed",
        storage=storage,
        path=str(result["new_path"]),
        details={"old_path": result["old_path"]},
        unverified_nodes=risk.unverified_nodes,
    )
    _refresh_latest_storage_directory(storage, str(result["directory_path"]))
    return redirect(redirect_to)


@require_POST
@app_login_required
@json_task_response
def inflate_storage_file_view(request, storage_id: str):
    if not settings.STORAGE_WRITE_ENABLED:
        return _storage_write_disabled_response()

    storage = _mount_or_404(storage_id)
    redirect_to = _safe_next_url(request)
    target_preallocation = request.POST.get("target_preallocation") or INFLATE_PREALLOCATION_FULL
    if target_preallocation not in INFLATE_PREALLOCATION_MODES:
        messages.error(request, "Unknown inflate target.")
        return redirect(redirect_to)

    latest_scan = _latest_storage_result_scan(storage)
    requested_path = _normalize_browser_path(request.POST.get("path", ""))
    entry = get_object_or_404(
        FileInventory,
        scan_run=latest_scan,
        storage=storage,
        path=requested_path,
        entry_type=FileInventory.EntryType.FILE,
    )
    risk = file_action_risk(entry, block_running_guests=False)

    acknowledged_risk = _risk_acknowledged(request, risk)
    try:
        _require_file_action_confirmations(request, risk)
        validate_inflate_storage_file(
            storage=storage,
            entry=entry,
            target_preallocation=target_preallocation,
            validate_owner_locally=not settings.STORAGE_INFLATE_WORKER_PRESERVES_OWNER,
            acknowledged_risk=acknowledged_risk,
        )
    except PermissionDenied:
        raise
    except StorageActionError as exc:
        _audit_file_action_failure(
            request,
            action="file.inflate_queued",
            storage=storage,
            path=entry.path,
            exc=exc,
            details={"target_preallocation": target_preallocation},
        )
        messages.error(request, str(exc))
        return redirect(redirect_to)

    task_id = common.enqueue_bulk_task(
        "core.tasks.inflate_storage_file_task",
        storage.id,
        entry.id,
        request.user.get_username() if request.user.is_authenticated else "",
        target_preallocation,
        acknowledged_risk,
    )
    _audit_file_action(
        request,
        action="file.inflate_queued",
        storage=storage,
        path=entry.path,
        details={"task_id": task_id, "target_preallocation": target_preallocation},
        unverified_nodes=risk.unverified_nodes,
    )
    return redirect(redirect_to)


def _prune_latest_storage_path(storage: StorageMount, path: str) -> None:
    latest_scan = _latest_storage_result_scan(storage)
    if latest_scan is None:
        return
    prefix = f"{path}/"
    FileInventory.objects.filter(scan_run=latest_scan, storage=storage).filter(
        Q(path=path) | Q(path__startswith=prefix)
    ).delete()


def _storage_directory_or_404(storage: StorageMount, latest_scan: ScanRun | None, path: str) -> None:
    if latest_scan is None:
        raise Http404("No storage inventory has been scanned yet.")
    exists = FileInventory.objects.filter(
        scan_run=latest_scan,
        storage=storage,
        path=path,
        entry_type=FileInventory.EntryType.DIRECTORY,
    ).exists()
    if not exists:
        raise Http404("Directory not found in latest scan.")


class BulkFileOutcome:
    """Per-object outcome of one fan-out over selected files.

    A fan-out is not atomic and must not be reported as if it were. Each entry
    keeps its own verdict so the operator can be told exactly what happened,
    what did not, and what is safe to retry.
    """

    def __init__(self) -> None:
        self.succeeded: list[FileInventory] = []
        self.failed: list[tuple[FileInventory, str]] = []
        self.skipped: list[FileInventory] = []
        self.aborted = False

    def record_success(self, entry: FileInventory) -> None:
        self.succeeded.append(entry)

    def record_failure(self, entry: FileInventory, exc: Exception, *, remaining: list[FileInventory]) -> None:
        self.failed.append((entry, str(exc)))
        if isinstance(exc, StorageOperationAborted):
            # The snapshot every preflight was evaluated against is gone; the
            # remaining entries were deliberately not attempted.
            self.aborted = True
            self.skipped = list(remaining)

    @property
    def attempted(self) -> int:
        return len(self.succeeded) + len(self.failed)

    @property
    def partial(self) -> bool:
        return bool(self.succeeded) and bool(self.failed or self.skipped)


_BULK_FAILURE_DETAIL_LIMIT = 5


def _report_bulk_file_outcome(
    request,
    outcome: BulkFileOutcome,
    *,
    storage: StorageMount,
    operation: str,
    verb: str,
    destructive: bool,
) -> None:
    """Report a fan-out honestly, and leave a durable record when it was not clean.

    A clean run stays silent: the per-file audit rows already tell that story and
    the browser refreshes. Anything else writes one ``file.bulk_operation`` event
    that owns the whole operation, so Recent Tasks and Audit can show a single
    row for "seven of twelve" instead of a scatter of unrelated lines.
    """
    if not outcome.failed and not outcome.skipped:
        return

    total = outcome.attempted + len(outcome.skipped)
    if total == 1:
        # A selection of one is not a fan-out: report the reason plainly and let
        # the single failed action speak for itself.
        messages.error(request, outcome.failed[0][1])
        return
    failures = [
        {"path": entry.path, "error": message} for entry, message in outcome.failed[:_BULK_FAILURE_DETAIL_LIMIT]
    ]
    summary = f"{len(outcome.succeeded)} of {total} {verb}"
    if outcome.succeeded:
        messages.success(request, f"{summary}.")
    detail = "; ".join(f"{item['path']}: {item['error']}" for item in failures)
    remaining = len(outcome.failed) - len(failures)
    if remaining > 0:
        detail += f"; and {remaining} more — see Audit"
    if outcome.skipped:
        detail += f"; {len(outcome.skipped)} not attempted"
    messages.error(request, f"{summary}. {detail}" if outcome.succeeded else detail)

    question = destructive and outcome.partial
    record_audit_event(
        request,
        action="file.bulk_operation",
        object_type="file",
        object_id=f"{storage.mount_ref}:{operation}",
        outcome="warning" if outcome.succeeded else "failed",
        details={
            "operation": operation,
            "verb": verb,
            "storage_id": storage.storage_id,
            "mount_ref": storage.mount_ref,
            "storage_name": storage.display_name,
            "summary": summary,
            "total": total,
            "succeeded": [entry.path for entry in outcome.succeeded],
            "failed": [{"path": entry.path, "error": message} for entry, message in outcome.failed],
            "skipped": [entry.path for entry in outcome.skipped],
            "aborted": outcome.aborted,
            # A destructive fan-out that half-happened is a decision the operator
            # still owes an answer to: retry the rest, or accept this state.
            "question": question,
            "retry": {
                "url": request.path,
                "paths": [entry.path for entry, _message in outcome.failed] + [e.path for e in outcome.skipped],
            },
        },
    )
    request.bulk_file_outcome = {
        "partial": outcome.partial,
        "summary": summary,
        "succeeded": len(outcome.succeeded),
        "failed": len(outcome.failed),
        "skipped": len(outcome.skipped),
    }


def _queue_upload_normalization(storage: StorageMount, paths: list[str], user) -> None:
    image_paths = [path for path in paths if _is_proxmox_image_upload_path(path)]
    if not image_paths:
        return
    common.enqueue_bulk_task(
        "core.tasks.normalize_uploaded_proxmox_image_paths_task",
        storage.id,
        image_paths,
        user.get_username() if getattr(user, "is_authenticated", False) else "",
    )


def _is_proxmox_image_upload_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return len(parts) >= 3 and parts[0] == "images" and parts[1].isdigit()


def _risk_acknowledged(request, risk: FileActionRisk) -> bool:
    """Did the operator answer the question that named what they are overriding?

    `confirm_risk` is that answer, and it only counts where the risk is marked
    acknowledgeable — the cases whose refusal would demand a repair the operator
    may have no way to make. It is deliberately not "the operator confirmed
    something": a mild warning about a guest-shaped filename asks for a second
    click too, and that click must not carry authority over a live guest
    reference it never mentioned.
    """
    return risk.acknowledgeable and request.POST.get("confirm_risk") == "yes"


def _risk_acknowledged_for_risks(request, risks: list[FileActionRisk]) -> bool:
    return any(risk.acknowledgeable for risk in risks) and request.POST.get("confirm_risk") == "yes"


def _require_file_action_confirmations(request, risk: FileActionRisk) -> None:
    if risk.blocked:
        raise StorageActionError(risk.warning_message)
    if request.POST.get("confirm_basic") != "yes":
        raise StorageActionError("File action was not confirmed.")
    if risk.requires_extra_confirmation and request.POST.get("confirm_risk") != "yes":
        raise StorageActionError("Risk confirmation was not confirmed.")


def _require_linked_clone_base_unblocked(entries: list[FileInventory]) -> None:
    """Hard-block trashing/moving a template's base volume while linked clones
    still ride it. Proxmox cannot protect a raw filesystem delete, so removing the
    backing file would corrupt every clone. Now feasible because lineage gives the
    backing-chain the V1 risk gate lacked. (A base volume's images/<vmid> folder is
    already covered by the 'guest image directories must be empty' rule.)"""
    from collections import Counter

    from core.services.classification import extract_vmid_from_image_path

    base_entries = [entry for entry in entries if entry.content_category == "base_image"]
    if not base_entries:
        return
    # Only a cluster that consumes this mount can hold a clone riding a volume on
    # it, so narrowing to those clusters cannot let a real dependency through.
    # Within them we take the largest count rather than the sum: the volume belongs
    # to one cluster, we may not know which, and over-blocking is the safe way to be
    # wrong — but summing would invent clones that do not exist and say so in the
    # refusal message.
    mount_ids = {entry.storage_id for entry in base_entries}
    lineage_by_cluster = _lineage_by_cluster(_clusters_for_mounts(mount_ids) or None)
    counts_by_cluster = [Counter(lineage.values()) for lineage in lineage_by_cluster.values()]
    if not any(counts_by_cluster):
        return
    for entry in base_entries:
        vmid = extract_vmid_from_image_path(entry.path)
        count = max((counts.get(vmid or -1, 0) for counts in counts_by_cluster), default=0)
        if count:
            raise StorageActionError(
                f"This is the base volume of template {vmid}, which {count} linked "
                f"clone{'s' if count != 1 else ''} still depend on. Delete the linked "
                "clones first (or full-clone them to detach) before removing it."
            )


def _require_file_action_confirmations_for_entries(request, entries: list[FileInventory]) -> None:
    _require_linked_clone_base_unblocked(entries)
    risks = [file_action_risk(entry) for entry in entries]
    blocked_risk = next((risk for risk in risks if risk.blocked), None)
    if blocked_risk:
        raise StorageActionError(blocked_risk.warning_message)
    if request.POST.get("confirm_basic") != "yes":
        raise StorageActionError("File action was not confirmed.")
    if any(risk.requires_extra_confirmation for risk in risks) and request.POST.get("confirm_risk") != "yes":
        raise StorageActionError("Risk confirmation was not confirmed.")


def _selected_storage_file_entries(
    request,
    *,
    storage: StorageMount,
    latest_scan: ScanRun | None,
    entry_types: list[str] | None = None,
) -> list[FileInventory]:
    paths: list[str] = []
    seen: set[str] = set()
    for raw_path in request.POST.getlist("path"):
        path = _normalize_browser_path(raw_path)
        if not path or path in seen:
            continue
        paths.append(path)
        seen.add(path)
    if not paths:
        raise Http404("File not found.")

    entry_types = entry_types or [FileInventory.EntryType.FILE]
    entries_by_path = {
        entry.path: entry
        for entry in FileInventory.objects.filter(
            scan_run=latest_scan,
            storage=storage,
            path__in=paths,
            entry_type__in=entry_types,
        )
    }
    if len(entries_by_path) != len(paths):
        raise Http404("File not found.")
    return [entries_by_path[path] for path in paths]


def _is_async_upload_request(request) -> bool:
    return request.headers.get("X-PVE-Helper-Async-Upload") == "1"


def _upload_success_response(request, redirect_to: str):
    if _is_async_upload_request(request):
        return JsonResponse({"ok": True, "redirect": redirect_to})
    return redirect(redirect_to)


def _upload_error_response(request, redirect_to: str, message: str):
    if _is_async_upload_request(request):
        return JsonResponse({"ok": False, "error": message, "redirect": redirect_to}, status=400)
    messages.error(request, message)
    return redirect(redirect_to)


def _join_browser_path(directory_path: str, name: str) -> str:
    """The path an action was aiming at, for the record of a failed attempt.

    Deliberately does not validate: this names what the operator tried, and a
    rejected name is exactly what an audit reader needs to see. It never reaches
    the filesystem — the confined helpers re-derive every path they act on.
    """
    name = (name or "").strip().strip("/")
    if not name:
        return directory_path or "/"
    return f"{directory_path}/{name}" if directory_path else name

"""Mount-scoped file reads: download and folder listing."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from core.services.confined_filesystem import ConfinedFilesystemError, open_regular_file_handle
from core.services.storage_mounts import (
    StorageMountError,
    registered_mount_health,
)
from core.services.storage_paths import (
    normalized_relative_path,
    storage_mount_root,
)

from ..common import (
    FileInventory,
    FileResponse,
    Http404,
    HttpResponse,
    JsonResponse,
    StorageMount,
    StreamingHttpResponse,
    app_login_required,
    content_disposition_header,
    get_object_or_404,
    ignored_relative_paths_for_storage,
    is_ignored_storage_path,
    quote,
    record_audit_event,
    settings,
)
from ._shared import (
    _latest_storage_result_scan,
    _mount_or_404,
    _normalize_browser_path,
)


@app_login_required
def download_storage_file(request, storage_id: str):
    storage = _mount_or_404(storage_id)
    latest_scan = _latest_storage_result_scan(storage)
    if latest_scan is None:
        raise Http404("No storage inventory has been scanned yet.")

    requested_path = _normalize_browser_path(request.GET.get("path", ""))
    if not requested_path:
        raise Http404("No file path requested.")

    entry = get_object_or_404(
        FileInventory,
        scan_run=latest_scan,
        storage=storage,
        path=requested_path,
        entry_type=FileInventory.EntryType.FILE,
    )
    try:
        file_handle = _open_storage_file(storage, entry.path)
    except ConfinedFilesystemError as exc:
        raise Http404("File not found.") from exc

    record_audit_event(
        request,
        action="file.downloaded",
        object_type="file",
        object_id=f"{storage.mount_ref}:{entry.path}",
        details={
            "storage_id": storage.storage_id,
            "mount_ref": storage.mount_ref,
            "storage_name": storage.display_name,
            "path": entry.path,
            "size_bytes": entry.size_bytes,
            "scan_run": latest_scan.id,
        },
    )

    return _download_response(request, storage, entry.path, file_handle)


@app_login_required
def storage_folders_view(request, storage_id: str):
    """JSON list of the folders in a storage, for the move/copy destination picker
    (so a folder can be chosen from a dropdown instead of typed by hand)."""
    storage = _mount_or_404(storage_id)
    scan = _latest_storage_result_scan(storage)
    folders: list[str] = []
    if scan:
        ignored_paths = ignored_relative_paths_for_storage(storage)
        folders = sorted(
            (
                path
                for path in FileInventory.objects.filter(
                    scan_run=scan,
                    storage=storage,
                    entry_type=FileInventory.EntryType.DIRECTORY,
                )
                .order_by("path")
                .values_list("path", flat=True)
                if not is_ignored_storage_path(path, ignored_paths)
            ),
            key=lambda item: [part.lower() for part in item.split("/")],
        )
    return JsonResponse({"folders": folders})


def _open_storage_file(storage: StorageMount, relative_path: str) -> BinaryIO:
    health = registered_mount_health(storage)
    if not health.available:
        raise Http404(health.reason or "Storage mount is unavailable.")
    return open_regular_file_handle(storage_mount_root(storage), relative_path)


def _download_response(request, storage: StorageMount, relative_path: str, file_handle: BinaryIO):
    file_name = PurePosixPath(relative_path).name
    file_size = os.fstat(file_handle.fileno()).st_size
    if _download_accel_available(storage):
        file_handle.close()
        response = HttpResponse(content_type="application/octet-stream")
        response["X-Accel-Redirect"] = _download_accel_uri(storage, relative_path)
        _decorate_download_response(response, file_name)
        return response

    range_header = request.headers.get("Range", "")
    if range_header:
        try:
            byte_range = _parse_http_byte_range(range_header, file_size)
        except ValueError:
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{file_size}"
            response["Accept-Ranges"] = "bytes"
            return response

        if byte_range is not None:
            start, end = byte_range
            length = end - start + 1
            response = StreamingHttpResponse(
                _file_range_iterator(file_handle, start=start, length=length),
                status=206,
                content_type="application/octet-stream",
            )
            response["Content-Range"] = f"bytes {start}-{end}/{file_size}"
            response["Content-Length"] = str(length)
            _decorate_download_response(response, file_name)
            return response

    response = FileResponse(
        file_handle,
        as_attachment=True,
        filename=file_name,
    )
    response.block_size = 1024 * 1024
    response["Accept-Ranges"] = "bytes"
    response["X-Accel-Buffering"] = "no"
    return response


def _accel_manifest_devices() -> dict[str, int]:
    """Top-level datastore name to the device nginx saw when it started.

    A line without a device is not treated as a match on the name alone. That is the
    case during a version skew where `web` is newer than the nginx sidecar, and an
    entry whose identity cannot be established is exactly the entry this function
    exists to distrust — the cost of refusing it is a streamed download.
    """
    devices: dict[str, int] = {}
    for line in settings.STORAGE_DOWNLOAD_ACCEL_MANIFEST_PATH.read_text(encoding="utf-8").splitlines():
        name, separator, device = line.partition("\t")
        if not separator or not name.strip() or not device.strip().isdigit():
            continue
        devices[name.strip()] = int(device.strip())
    return devices


def _download_accel_available(storage: StorageMount) -> bool:
    """Whether nginx can serve this mount's bytes, and serve the *same* filesystem.

    nginx keeps its own copy of every submount for the container's lifetime
    (rprivate + recursive-readonly) while the app follows the host through rslave.
    Matching on the name alone therefore survives a host remount, and an authorized
    download would return bytes from the detached original while every check ran
    against the replacement. Comparing the device catches that: bind mounts share
    the superblock, so the numbers agree while it is one filesystem and diverge the
    moment it is two.

    Every failure here disables acceleration, which is the same path a mount added
    after nginx started already takes — Django streams the file from the filesystem
    the app itself validated.
    """
    if not settings.STORAGE_DOWNLOAD_ACCEL_ENABLED:
        return False
    relative = storage.relative_path
    if not relative and settings.PVE_TEST_NETWORK_DISABLED:
        relative = storage.storage_id
    try:
        relative = normalized_relative_path(relative).split("/", 1)[0]
        expected = _accel_manifest_devices().get(relative)
        if expected is None:
            return False
        current = os.stat(Path(settings.PVE_HELPER_STORAGE_CONTAINER_ROOT) / relative).st_dev
    except OSError, StorageMountError, ValueError:
        return False
    return current == expected


def _decorate_download_response(response, file_name: str) -> None:
    response["Accept-Ranges"] = "bytes"
    response["X-Accel-Buffering"] = "no"
    response["Content-Disposition"] = content_disposition_header(True, file_name)


def _download_accel_uri(storage: StorageMount, relative_path: str) -> str:
    prefix = settings.STORAGE_DOWNLOAD_ACCEL_PREFIX.rstrip("/")
    if not storage.relative_path and settings.PVE_TEST_NETWORK_DISABLED:
        mounted_path = PurePosixPath(storage.storage_id, relative_path).as_posix()
        return f"{prefix}/{quote(mounted_path, safe='/')}"
    mounted_path = PurePosixPath(
        normalized_relative_path(storage.relative_path),
        PurePosixPath(relative_path).as_posix(),
    ).as_posix()
    return f"{prefix}/{quote(mounted_path, safe='/')}"


def _parse_http_byte_range(range_header: str, file_size: int) -> tuple[int, int] | None:
    units, separator, value = range_header.partition("=")
    if units.strip().lower() != "bytes" or separator != "=" or "," in value:
        return None

    start_text, separator, end_text = value.strip().partition("-")
    if separator != "-":
        return None
    if not start_text and not end_text:
        raise ValueError("empty range")

    if start_text:
        start = int(start_text)
        end = int(end_text) if end_text else file_size - 1
    else:
        suffix_length = int(end_text)
        if suffix_length <= 0:
            raise ValueError("invalid suffix range")
        start = max(file_size - suffix_length, 0)
        end = file_size - 1

    if start < 0 or end < start or start >= file_size:
        raise ValueError("unsatisfiable range")
    return start, min(end, file_size - 1)


def _file_range_iterator(file_handle: BinaryIO, *, start: int, length: int):
    remaining = length
    with file_handle:
        file_handle.seek(start)
        while remaining > 0:
            chunk = file_handle.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk

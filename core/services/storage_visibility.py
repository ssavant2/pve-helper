from __future__ import annotations

from pathlib import Path, PurePosixPath

from django.conf import settings

from core.models import StorageMount


def ignored_relative_paths_for_storage(storage: StorageMount) -> set[str]:
    ignored: set[str] = set()
    if settings.FILE_UPLOAD_TEMP_DIR:
        relative = _relative_path_if_inside_storage(settings.FILE_UPLOAD_TEMP_DIR, storage.path)
        if relative:
            ignored.add(relative)
    trash_relative = _relative_path_if_inside_storage(_app_trash_path(storage), storage.path)
    if trash_relative:
        ignored.add(trash_relative)
        ignored.add(PurePosixPath(trash_relative).parts[0])
    return ignored


def is_ignored_storage_path(path: str, ignored_paths: set[str]) -> bool:
    normalized = _normalize_relative_path(path)
    return any(normalized == ignored or normalized.startswith(f"{ignored}/") for ignored in ignored_paths)


def _relative_path_if_inside_storage(path: str, storage_root: str) -> str:
    try:
        root = Path(storage_root).resolve(strict=False)
        candidate = Path(path).resolve(strict=False)
    except OSError:
        return ""

    if candidate == root or not candidate.is_relative_to(root):
        return ""
    return candidate.relative_to(root).as_posix()


def _app_trash_path(storage: StorageMount) -> str:
    if storage.trash_path:
        return storage.trash_path
    return (Path(storage.path) / ".trash" / "pve-helper").as_posix()


def _normalize_relative_path(path: str) -> str:
    stripped = (path or "").strip().strip("/")
    if not stripped:
        return ""
    return PurePosixPath(stripped).as_posix()

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .classification import categorize_proxmox_path, derive_volid
from .public_errors import public_exception_message


@dataclass(frozen=True)
class StorageEntry:
    """One filesystem entry beneath a mount root.

    The two path fields are named for what they are rather than reusing `path`,
    because that word already means the absolute one on `StorageMount` and the
    relative one on `FileInventory`. Callers writing a row therefore spell out
    `path=entry.relative_path`, which is the translation into the model's naming
    and not an accident of matching attribute names.
    """

    full_path: str
    relative_path: str
    entry_type: str
    content_category: str
    derived_volid: str
    size_bytes: int | None
    modified_at: float | None


class StorageScanner:
    """Read-only filesystem scanner shell for configured storage roots."""

    def __init__(self, storage_id: str, root: str, *, ignored_paths: set[str] | None = None):
        self.storage_id = storage_id
        self.root = Path(root)
        self.ignored_paths = ignored_paths or set()
        self.errors: list[dict[str, str]] = []

    def iter_entries(self) -> Iterator[StorageEntry]:
        if not self.root.exists():
            return

        stack = [self.root]
        while stack:
            current = stack.pop()
            try:
                children = sorted(current.iterdir(), key=lambda path: path.name.lower(), reverse=True)
            except OSError as exc:
                self.errors.append(self._error(current, exc))
                continue

            for item in children:
                try:
                    stat = item.lstat()
                except OSError as exc:
                    self.errors.append(self._error(item, exc))
                    continue

                relative = item.relative_to(self.root).as_posix()
                if self._is_ignored(relative):
                    continue
                entry_type = self._entry_type(item)
                derived = derive_volid(self.storage_id, relative)

                if entry_type == "directory":
                    stack.append(item)

                yield StorageEntry(
                    full_path=item.as_posix(),
                    relative_path=relative,
                    entry_type=entry_type,
                    content_category=derived.content_category if derived else categorize_proxmox_path(relative),
                    derived_volid=derived.volid if derived else "",
                    size_bytes=stat.st_size if entry_type == "file" else None,
                    modified_at=stat.st_mtime,
                )

    def iter_directory(self, relative_path: str = "") -> list[StorageEntry]:
        directory = self.root if not relative_path else self.root.joinpath(*PurePosixPath(relative_path).parts)
        if not directory.exists() or not directory.is_dir():
            return []

        entries: list[StorageEntry] = []
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name.lower())
        except OSError as exc:
            self.errors.append(self._error(directory, exc))
            return []

        for item in children:
            try:
                stat = item.lstat()
            except OSError as exc:
                self.errors.append(self._error(item, exc))
                continue

            relative = item.relative_to(self.root).as_posix()
            if self._is_ignored(relative):
                continue
            entry_type = self._entry_type(item)
            derived = derive_volid(self.storage_id, relative)
            entries.append(
                StorageEntry(
                    full_path=item.as_posix(),
                    relative_path=relative,
                    entry_type=entry_type,
                    content_category=derived.content_category if derived else categorize_proxmox_path(relative),
                    derived_volid=derived.volid if derived else "",
                    size_bytes=stat.st_size if entry_type == "file" else None,
                    modified_at=stat.st_mtime,
                )
            )
        return entries

    def _entry_type(self, path: Path) -> str:
        if path.is_symlink():
            return "symlink"
        if path.is_dir():
            return "directory"
        if path.is_file():
            return "file"
        return "other"

    def _is_ignored(self, relative_path: str) -> bool:
        return any(
            relative_path == ignored or relative_path.startswith(f"{ignored}/") for ignored in self.ignored_paths
        )

    def _error(self, path: Path, exc: OSError) -> dict[str, str]:
        try:
            relative = path.relative_to(self.root).as_posix()
        except ValueError:
            relative = path.as_posix()
        return {
            "path": relative or ".",
            "error": exc.__class__.__name__,
            "message": public_exception_message(
                exc, operation="storage_scan.walk", fallback="The path could not be read during the scan."
            ),
        }

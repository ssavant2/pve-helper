from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .classification import categorize_proxmox_path, derive_volid


@dataclass(frozen=True)
class StorageEntry:
    path: str
    relative_path: str
    entry_type: str
    content_category: str
    derived_volid: str
    size_bytes: int | None


class StorageScanner:
    """Read-only filesystem scanner shell for configured storage roots."""

    def __init__(self, storage_id: str, root: str):
        self.storage_id = storage_id
        self.root = Path(root)

    def iter_top_level(self) -> list[StorageEntry]:
        if not self.root.exists():
            return []

        entries: list[StorageEntry] = []
        for item in sorted(self.root.iterdir(), key=lambda path: path.name.lower()):
            relative = item.relative_to(self.root).as_posix()
            derived = derive_volid(self.storage_id, relative)
            entries.append(
                StorageEntry(
                    path=item.as_posix(),
                    relative_path=relative,
                    entry_type=self._entry_type(item),
                    content_category=derived.content_category if derived else categorize_proxmox_path(relative),
                    derived_volid=derived.volid if derived else "",
                    size_bytes=item.stat().st_size if item.is_file() else None,
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

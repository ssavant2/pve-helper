"""Descriptor-relative filesystem access confined beneath a trusted root.

Mounted Proxmox storage can be changed by systems other than this process.  A
``Path.resolve()``/containment check followed by a separate open therefore has
a symlink race.  These helpers keep an open directory descriptor while walking
every untrusted component with ``O_NOFOLLOW``.

The application runtime is Linux-only.  Renames use Linux ``renameat2`` with
``RENAME_NOREPLACE`` so a concurrent target cannot be overwritten.
"""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from core.services.confined_names import ConfinedNameError, confined_path_component


class ConfinedFilesystemError(Exception):
    """A requested path is invalid, unavailable, or unsafe to access."""


class ConfinedPathExistsError(ConfinedFilesystemError):
    """A no-replace operation found an existing target."""


class ConfinedPathMissingError(ConfinedFilesystemError):
    """A required source path does not exist."""


class ConfinedCrossDeviceError(ConfinedFilesystemError):
    """A rename would cross a filesystem boundary.

    Kept distinct from the generic error because callers legitimately react to
    it: a move between two exports falls back to copy-then-delete, while a move
    that was supposed to stay inside one export is a user-facing explanation.
    """


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_EXCLUSIVE_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
_COPY_CHUNK_BYTES = 1024 * 1024
_RENAME_NOREPLACE = 1
# The filesystem declining the flag, rather than the arguments being wrong. NFS
# answers EINVAL; older kernels ENOSYS; some stacked filesystems EOPNOTSUPP.
_RENAMEAT2_UNSUPPORTED = frozenset({errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP})
_AT_FDCWD = -100
_AT_SYMLINK_FOLLOW = 0x400
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)
if _RENAMEAT2 is not None:
    _RENAMEAT2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    _RENAMEAT2.restype = ctypes.c_int
_LINKAT = _LIBC.linkat
_LINKAT.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
_LINKAT.restype = ctypes.c_int
_UNLINKAT = _LIBC.unlinkat
_UNLINKAT.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
_UNLINKAT.restype = ctypes.c_int


def normalized_relative_path(raw: str, *, replace_backslashes: bool = False) -> str:
    """Return a non-empty POSIX path containing only ordinary components."""
    value = str(raw or "")
    if replace_backslashes:
        value = value.replace("\\", "/")
    elif "\\" in value:
        raise ConfinedFilesystemError("Backslashes are not valid path separators.")
    if value.startswith("/"):
        raise ConfinedFilesystemError("Absolute paths are not allowed.")
    value = value.strip("/")
    if not value or "\x00" in value:
        raise ConfinedFilesystemError("Path is empty or invalid.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ConfinedFilesystemError("Path must stay beneath the storage root.")
    return path.as_posix()


def confined_relative_path(*parts: str) -> str:
    """Join already-relative path fragments and validate the result."""
    return normalized_relative_path(PurePosixPath(*parts).as_posix())


def regular_file_exists(root: str | Path, relative_path: str) -> bool:
    try:
        with open_regular_file(root, relative_path):
            return True
    except ConfinedFilesystemError:
        return False


@contextmanager
def open_regular_file(root: str | Path, relative_path: str) -> Iterator[BinaryIO]:
    """Open one regular file without following any untrusted symlink."""
    handle = open_regular_file_handle(root, relative_path)
    try:
        yield handle
    finally:
        handle.close()


def open_regular_file_handle(root: str | Path, relative_path: str) -> BinaryIO:
    """Open one regular file beneath root; the caller owns the returned handle."""
    parts = PurePosixPath(normalized_relative_path(relative_path)).parts
    parent_fd = _open_parent(root, parts[:-1])
    file_fd = -1
    try:
        try:
            file_fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            raise ConfinedFilesystemError("File is unavailable or unsafe.") from exc
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ConfinedFilesystemError("Path does not identify a regular file.")
        handle = os.fdopen(file_fd, "rb", closefd=True)
        file_fd = -1
        return handle
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(parent_fd)


def rename_regular_file_noreplace(
    root: str | Path,
    source_relative_path: str,
    target_name: str,
) -> str:
    """Rename a regular file in place, atomically refusing an existing target."""
    source_parts = PurePosixPath(normalized_relative_path(source_relative_path)).parts
    safe_target = normalized_relative_path(target_name)
    if len(PurePosixPath(safe_target).parts) != 1:
        raise ConfinedFilesystemError("Rename target must be a file name.")

    parent_fd = _open_parent(root, source_parts[:-1])
    try:
        try:
            source_stat = os.stat(source_parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise ConfinedFilesystemError("Source file is unavailable or unsafe.") from exc
        if not stat.S_ISREG(source_stat.st_mode):
            raise ConfinedFilesystemError("Source path does not identify a regular file.")
        _renameat2_noreplace(parent_fd, source_parts[-1], safe_target)
    finally:
        os.close(parent_fd)

    parent = PurePosixPath(*source_parts[:-1]) if source_parts[:-1] else PurePosixPath()
    return (parent / safe_target).as_posix()


def rename_entry_noreplace(
    source_root: str | Path,
    source_relative_path: str,
    target_relative_path: str,
    *,
    target_root: str | Path | None = None,
    expected: str = "any",
    create_target_parents: bool = True,
) -> None:
    """Move a file or directory to another location, refusing an existing target.

    Source and target may sit under different parents and under different roots,
    so both are walked descriptor-relative and the rename crosses the two
    descriptors. An existing target is refused rather than merged or replaced:
    ``RENAME_NOREPLACE`` decides that in the kernel, so no window exists between
    deciding and acting. ``expected`` pins the source type ("file", "directory"
    or "any"), because every caller here knows which one it means and a swapped
    type is a signal rather than something to accommodate.

    A rename across filesystems raises :class:`ConfinedCrossDeviceError`; whether
    that is a fallback or a refusal is the caller's decision, not this helper's.
    """
    source_parts = PurePosixPath(normalized_relative_path(source_relative_path)).parts
    target_parts = PurePosixPath(normalized_relative_path(target_relative_path)).parts

    source_parent_fd = _open_parent(source_root, source_parts[:-1])
    target_parent_fd = -1
    try:
        source_stat = _stat_confined_child(source_parent_fd, source_parts[-1], expected=expected)
        target_parent_base = source_root if target_root is None else target_root
        if create_target_parents:
            target_parent_fd = _open_or_create_parent(target_parent_base, target_parts[:-1])
        else:
            target_parent_fd = _open_parent(target_parent_base, target_parts[:-1])
        _renameat2_noreplace(
            source_parent_fd,
            source_parts[-1],
            target_parts[-1],
            target_parent_fd=target_parent_fd,
            source_is_directory=stat.S_ISDIR(source_stat.st_mode),
        )
    finally:
        if target_parent_fd >= 0:
            os.close(target_parent_fd)
        os.close(source_parent_fd)


def rename_directory_noreplace(root: str | Path, source_relative_path: str, target_relative_path: str) -> None:
    """Move a directory, with its contents, to another location beneath the root."""
    rename_entry_noreplace(root, source_relative_path, target_relative_path, expected="directory")


def copy_regular_file_noreplace(
    source_root: str | Path,
    source_relative_path: str,
    target_relative_path: str,
    *,
    target_root: str | Path | None = None,
    create_target_parents: bool = True,
) -> None:
    """Copy a regular file to a new location, refusing an existing target.

    ``shutil.copy2`` opens the destination with ``"wb"``, which truncates an
    existing file: the same overwrite that ``RENAME_NOREPLACE`` exists to
    prevent, reached by a different call. Here the destination is created with
    ``O_EXCL`` on a directory descriptor, so an existing name loses the race in
    the kernel instead of losing its contents. Mode and timestamps follow the
    source the way ``copy2`` sets them; ownership does not, because this process
    is not necessarily privileged and Proxmox reads as root regardless.

    A partial copy is removed. The destination is only ever the name this call
    created, so cleanup cannot remove a file that belonged to someone else.
    """
    target_parts = PurePosixPath(normalized_relative_path(target_relative_path)).parts
    target_parent_base = source_root if target_root is None else target_root

    source_handle = open_regular_file_handle(source_root, source_relative_path)
    try:
        source_stat = os.fstat(source_handle.fileno())
        if create_target_parents:
            target_parent_fd = _open_or_create_parent(target_parent_base, target_parts[:-1])
        else:
            target_parent_fd = _open_parent(target_parent_base, target_parts[:-1])
        try:
            target_fd = _create_exclusive_child(
                target_parent_fd,
                target_parts[-1],
                mode=stat.S_IMODE(source_stat.st_mode),
            )
            try:
                with os.fdopen(target_fd, "wb", closefd=False) as target_handle:
                    shutil.copyfileobj(source_handle, target_handle, _COPY_CHUNK_BYTES)
                os.utime(
                    target_fd,
                    ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
                )
            except BaseException:
                _discard_created_child(target_parent_fd, target_parts[-1])
                raise
            finally:
                os.close(target_fd)
        finally:
            os.close(target_parent_fd)
    except OSError as exc:
        raise ConfinedFilesystemError("Confined copy failed.") from exc
    finally:
        source_handle.close()


def create_regular_file_exclusive(
    root: str | Path,
    relative_path: str,
    *,
    mode: int = 0o644,
    create_parents: bool = False,
) -> BinaryIO:
    """Create and open a new regular file, refusing an existing name.

    The caller owns the returned handle. Nothing here retries or picks a
    different name: an occupied name is the caller's decision to report.
    """
    parts = PurePosixPath(normalized_relative_path(relative_path)).parts
    parent_fd = _open_or_create_parent(root, parts[:-1]) if create_parents else _open_parent(root, parts[:-1])
    try:
        file_fd = _create_exclusive_child(parent_fd, parts[-1], mode=mode)
        return os.fdopen(file_fd, "wb", closefd=True)
    finally:
        os.close(parent_fd)


def create_directory_noreplace(root: str | Path, relative_path: str, *, mode: int = 0o755) -> None:
    """Create one directory beneath an existing confined parent."""
    parts = PurePosixPath(normalized_relative_path(relative_path)).parts
    parent_fd = _open_parent(root, parts[:-1])
    try:
        try:
            os.mkdir(parts[-1], mode=mode, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise ConfinedPathExistsError("Target folder already exists.") from exc
        except OSError as exc:
            raise ConfinedFilesystemError("Folder creation failed.") from exc
    finally:
        os.close(parent_fd)


def create_confined_directories(root: str | Path, relative_path: str) -> None:
    """Create a directory and any missing parents beneath the root."""
    parts = PurePosixPath(normalized_relative_path(relative_path)).parts
    os.close(_open_or_create_parent(root, parts))


def remove_confined_file(root: str | Path, relative_path: str, *, missing_ok: bool = False) -> None:
    """Unlink one non-directory entry beneath the root."""
    parts = PurePosixPath(normalized_relative_path(relative_path)).parts
    try:
        parent_fd = _open_parent(root, parts[:-1])
    except ConfinedFilesystemError:
        if missing_ok:
            return
        raise
    try:
        try:
            os.unlink(parts[-1], dir_fd=parent_fd)
        except FileNotFoundError:
            if not missing_ok:
                raise ConfinedPathMissingError("File does not exist.") from None
        except IsADirectoryError as exc:
            raise ConfinedFilesystemError("Path is a directory.") from exc
        except OSError as exc:
            raise ConfinedFilesystemError("Delete failed.") from exc
    finally:
        os.close(parent_fd)


def remove_confined_tree(root: str | Path, relative_path: str, *, missing_ok: bool = True) -> None:
    """Delete an entry and, if it is a directory, everything beneath it.

    ``shutil.rmtree`` on a resolved path is the one write in this module that can
    destroy data outside the root: resolution follows symlinks, so a component
    swapped after the check aims the deletion somewhere else entirely. Here every
    level is entered by directory descriptor with ``O_NOFOLLOW``, and a symlink
    found during the walk is unlinked as the link it is — never followed into
    whatever it points at.
    """
    parts = PurePosixPath(normalized_relative_path(relative_path)).parts
    try:
        parent_fd = _open_parent(root, parts[:-1])
    except ConfinedFilesystemError:
        if missing_ok:
            return
        raise
    try:
        try:
            entry_stat = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return
            raise ConfinedPathMissingError("Path does not exist.") from None
        except OSError as exc:
            raise ConfinedFilesystemError("Path is unavailable or unsafe.") from exc

        try:
            if stat.S_ISDIR(entry_stat.st_mode):
                _remove_directory_tree_at(parent_fd, parts[-1])
            else:
                os.unlink(parts[-1], dir_fd=parent_fd)
        except OSError as exc:
            raise ConfinedFilesystemError("Delete failed.") from exc
    finally:
        os.close(parent_fd)


def remove_confined_empty_directory(root: str | Path, relative_path: str, *, missing_ok: bool = True) -> None:
    """Remove one directory beneath the root, only if it is already empty."""
    parts = PurePosixPath(normalized_relative_path(relative_path)).parts
    try:
        parent_fd = _open_parent(root, parts[:-1])
    except ConfinedFilesystemError:
        if missing_ok:
            return
        raise
    try:
        try:
            os.rmdir(parts[-1], dir_fd=parent_fd)
        except FileNotFoundError:
            if not missing_ok:
                raise ConfinedPathMissingError("Directory does not exist.") from None
        except OSError as exc:
            raise ConfinedFilesystemError("Directory could not be removed.") from exc
    finally:
        os.close(parent_fd)


def confined_entry_stat(root: str | Path, relative_path: str) -> os.stat_result | None:
    """Stat one entry without following a final symlink; None when it is absent.

    This is a check, so it carries the usual caveat: by the time the caller acts
    the answer may be stale. Use it for reporting and for choosing between code
    paths, never as the guard that makes a write safe — that is the mutating
    helper's job, in the kernel.
    """
    parts = PurePosixPath(normalized_relative_path(relative_path)).parts
    try:
        parent_fd = _open_parent(root, parts[:-1])
    except ConfinedFilesystemError:
        return None
    try:
        return os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return None
    finally:
        os.close(parent_fd)


def list_confined_directory(root: str | Path, relative_path: str = "") -> list[tuple[str, os.stat_result]]:
    """List one directory's entries with their un-followed stats."""
    parts = PurePosixPath(normalized_relative_path(relative_path)).parts if relative_path else ()
    directory_fd = _open_parent(root, parts)
    try:
        entries: list[tuple[str, os.stat_result]] = []
        for name in os.listdir(directory_fd):
            try:
                entries.append((name, os.stat(name, dir_fd=directory_fd, follow_symlinks=False)))
            except OSError:
                continue
        return entries
    finally:
        os.close(directory_fd)


def set_confined_owner_and_mode(
    root: str | Path,
    relative_path: str,
    *,
    uid: int,
    gid: int,
    mode: int,
    expected: str = "any",
) -> None:
    """Apply ownership and mode to an already-confined entry.

    ``os.chown(path, ...)`` follows symlinks and re-walks the path, so a swapped
    component moves a root-owned chmod onto a file this application does not own.
    The entry is instead opened ``O_NOFOLLOW``, its type verified through the
    descriptor, and both changes applied to that descriptor.
    """
    parts = PurePosixPath(normalized_relative_path(relative_path)).parts
    parent_fd = _open_parent(root, parts[:-1])
    entry_fd = -1
    try:
        entry_stat = _stat_confined_child(parent_fd, parts[-1], expected=expected)
        flags = _DIRECTORY_FLAGS if stat.S_ISDIR(entry_stat.st_mode) else _FILE_FLAGS
        try:
            entry_fd = os.open(parts[-1], flags, dir_fd=parent_fd)
            os.fchown(entry_fd, uid, gid)
            os.fchmod(entry_fd, mode)
        except OSError as exc:
            raise ConfinedFilesystemError("Ownership or mode could not be applied.") from exc
    finally:
        if entry_fd >= 0:
            os.close(entry_fd)
        os.close(parent_fd)


def confined_directory_free_bytes(root: str | Path, relative_path: str = "") -> int:
    """Free bytes on the filesystem holding a confined directory."""
    parts = PurePosixPath(normalized_relative_path(relative_path)).parts if relative_path else ()
    directory_fd = _open_parent(root, parts)
    try:
        usage = os.statvfs(directory_fd)
        return usage.f_bavail * usage.f_frsize
    except OSError as exc:
        raise ConfinedFilesystemError("Free space is unavailable.") from exc
    finally:
        os.close(directory_fd)


@dataclass(frozen=True)
class ConfinedDirectory:
    """An open directory descriptor plus the paths a child process can use.

    ``/proc/self/fd/<n>`` is a magic link: the kernel resolves it straight to the
    directory this descriptor already holds, without re-walking the name. So a
    child process given ``child_path("disk.qcow2")`` reaches exactly the
    directory this process confined, even if the path that led here is swapped
    afterwards. Only the final component is resolved by name, and that name is
    ours rather than the caller's.
    """

    fd: int

    def child_path(self, name: str) -> str:
        if len(PurePosixPath(normalized_relative_path(name)).parts) != 1:
            raise ConfinedFilesystemError("Child name must be a single path component.")
        return f"/proc/self/fd/{self.fd}/{name}"

    @property
    def pass_fds(self) -> tuple[int, ...]:
        return (self.fd,)


@contextmanager
def confined_directory(root: str | Path, relative_path: str = "") -> Iterator[ConfinedDirectory]:
    """Hold one confined directory open for the duration of a block."""
    parts = PurePosixPath(normalized_relative_path(relative_path)).parts if relative_path else ()
    directory_fd = _open_parent(root, parts)
    try:
        yield ConfinedDirectory(fd=directory_fd)
    finally:
        os.close(directory_fd)


def _stat_confined_child(parent_fd: int, name: str, *, expected: str = "any") -> os.stat_result:
    try:
        entry_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ConfinedPathMissingError("Path does not exist.") from exc
    except OSError as exc:
        raise ConfinedFilesystemError("Path is unavailable or unsafe.") from exc
    if expected == "file" and not stat.S_ISREG(entry_stat.st_mode):
        raise ConfinedFilesystemError("Path does not identify a regular file.")
    if expected == "directory" and not stat.S_ISDIR(entry_stat.st_mode):
        raise ConfinedFilesystemError("Path does not identify a directory.")
    if expected == "any" and not (stat.S_ISREG(entry_stat.st_mode) or stat.S_ISDIR(entry_stat.st_mode)):
        raise ConfinedFilesystemError("Only files and directories can be changed.")
    return entry_stat


def _create_exclusive_child(parent_fd: int, name: str, *, mode: int) -> int:
    try:
        return os.open(name, _EXCLUSIVE_FILE_FLAGS, mode=mode, dir_fd=parent_fd)
    except FileExistsError as exc:
        raise ConfinedPathExistsError("Target file already exists.") from exc
    except OSError as exc:
        raise ConfinedFilesystemError("File could not be created.") from exc


def _discard_created_child(parent_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=parent_fd)
    except OSError:
        pass


def _remove_directory_tree_at(parent_fd: int, name: str) -> None:
    directory_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        for entry in os.listdir(directory_fd):
            entry_stat = os.stat(entry, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(entry_stat.st_mode):
                _remove_directory_tree_at(directory_fd, entry)
            else:
                os.unlink(entry, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)


def remove_confined_directory(root: str | Path, relative_path: str) -> None:
    """Remove files from one directory and then the directory, without following links."""
    parts = PurePosixPath(normalized_relative_path(relative_path)).parts
    parent_fd = _open_parent(root, parts[:-1])
    directory_fd = -1
    try:
        try:
            directory_fd = os.open(parts[-1], _DIRECTORY_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ConfinedFilesystemError("Directory is unavailable or unsafe.") from exc
        for name in os.listdir(directory_fd):
            try:
                entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise ConfinedFilesystemError("Directory entry is unavailable.") from exc
            if stat.S_ISDIR(entry_stat.st_mode):
                raise ConfinedFilesystemError("Nested staging directories are not allowed.")
            os.unlink(name, dir_fd=directory_fd)
        os.close(directory_fd)
        directory_fd = -1
        os.rmdir(parts[-1], dir_fd=parent_fd)
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)
        os.close(parent_fd)


def hardlink_open_file_to_new_directory(
    root: str | Path,
    source: BinaryIO,
    *,
    parent_relative_path: str,
    directory_name: str,
    file_name: str,
) -> str:
    """Hardlink an already-confined file into a newly created confined directory."""
    parent_parts = PurePosixPath(normalized_relative_path(parent_relative_path)).parts
    safe_directory = normalized_relative_path(directory_name)
    safe_file = normalized_relative_path(file_name)
    if len(PurePosixPath(safe_directory).parts) != 1 or len(PurePosixPath(safe_file).parts) != 1:
        raise ConfinedFilesystemError("Staging names must be single path components.")

    parent_fd = _open_or_create_parent(root, parent_parts)
    directory_fd = -1
    created = False
    try:
        try:
            os.mkdir(safe_directory, mode=0o750, dir_fd=parent_fd)
            created = True
        except FileExistsError as exc:
            raise ConfinedPathExistsError("Staging directory already exists.") from exc
        directory_fd = os.open(safe_directory, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        _link_open_file_at(source, directory_fd, safe_file)
    except BaseException:
        if directory_fd >= 0:
            try:
                _unlink_file_at(directory_fd, safe_file)
            except OSError:
                pass
        if created:
            try:
                os.rmdir(safe_directory, dir_fd=parent_fd)
            except OSError:
                pass
        raise
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)
        os.close(parent_fd)
    return confined_relative_path(parent_relative_path, safe_directory, safe_file)


def hardlink_open_file_noreplace(
    root: str | Path,
    source: BinaryIO,
    target_relative_path: str,
    *,
    create_parents: bool = True,
) -> None:
    """Hardlink an already-confined open file to a new name beneath the root.

    ``os.link(source_path, target_path)`` resolves both names again; this links
    the descriptor already held, into a directory descriptor already walked.
    ``linkat`` has no no-replace flag because it never replaces: an existing
    target is ``EEXIST``, which is exactly the semantics wanted here.
    """
    target_parts = PurePosixPath(normalized_relative_path(target_relative_path)).parts
    parent_fd = (
        _open_or_create_parent(root, target_parts[:-1]) if create_parents else _open_parent(root, target_parts[:-1])
    )
    try:
        _link_open_file_at(source, parent_fd, target_parts[-1])
    finally:
        os.close(parent_fd)


def _link_open_file_at(source: BinaryIO, directory_fd: int, file_name: str) -> None:
    result = _LINKAT(
        _AT_FDCWD,
        os.fsencode(f"/proc/self/fd/{source.fileno()}"),
        directory_fd,
        os.fsencode(file_name),
        _AT_SYMLINK_FOLLOW,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise ConfinedPathExistsError("Target file already exists.")
        raise ConfinedFilesystemError("Could not hardlink the confined source.") from OSError(
            error_number,
            os.strerror(error_number),
        )


def _unlink_file_at(directory_fd: int, file_name: str) -> None:
    result = _UNLINKAT(directory_fd, os.fsencode(file_name), 0)
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _open_parent(root: str | Path, parts: tuple[str, ...]) -> int:
    try:
        trusted_root = Path(root).resolve(strict=True)
        current_fd = os.open(trusted_root, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise ConfinedFilesystemError("Storage root is unavailable.") from exc

    try:
        for part in parts:
            try:
                next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
            except OSError as exc:
                raise ConfinedFilesystemError("Parent directory is unavailable or unsafe.") from exc
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _open_or_create_parent(root: str | Path, parts: tuple[str, ...]) -> int:
    try:
        trusted_root = Path(root).resolve(strict=True)
        current_fd = os.open(trusted_root, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise ConfinedFilesystemError("Storage root is unavailable.") from exc

    try:
        for part in parts:
            try:
                next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
            except FileNotFoundError:
                os.mkdir(part, mode=0o750, dir_fd=current_fd)
                next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
            except OSError as exc:
                raise ConfinedFilesystemError("Parent directory is unavailable or unsafe.") from exc
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _renameat2_noreplace(
    parent_fd: int,
    source_name: str,
    target_name: str,
    *,
    target_parent_fd: int | None = None,
    source_is_directory: bool = False,
) -> None:
    """Rename without replacing, on whatever the export happens to support.

    ``RENAME_NOREPLACE`` is the right primitive and the only one that decides the
    whole question in a single syscall, but it is a Linux-VFS feature that the
    underlying filesystem must implement. **NFS does not**: the exports this
    application is actually pointed at return ``EINVAL`` for it, which is the
    filesystem declining the flag rather than the arguments being wrong. Treating
    that as a hard failure would mean no-replace semantics existed only on the
    local disks nobody stores disk images on, so the guarantee falls back to
    primitives NFS does implement.
    """
    target_fd = parent_fd if target_parent_fd is None else target_parent_fd
    if _RENAMEAT2 is not None:
        result = _RENAMEAT2(
            parent_fd,
            os.fsencode(source_name),
            target_fd,
            os.fsencode(target_name),
            _RENAME_NOREPLACE,
        )
        if result == 0:
            return
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise ConfinedPathExistsError("Target file already exists.")
        if error_number == errno.EXDEV:
            raise ConfinedCrossDeviceError("Rename would cross a filesystem boundary.")
        if error_number not in _RENAMEAT2_UNSUPPORTED:
            raise ConfinedFilesystemError("Secure rename failed.") from OSError(error_number, os.strerror(error_number))

    _reserve_then_rename(parent_fd, source_name, target_fd, target_name, source_is_directory)


def _reserve_then_rename(
    source_parent_fd: int,
    source_name: str,
    target_parent_fd: int,
    target_name: str,
    source_is_directory: bool,
) -> None:
    """No-replace rename built from an exclusive create and a plain ``renameat``.

    Two calls, both of which NFS implements. First the target name is *claimed*:
    ``O_CREAT|O_EXCL`` for a file, ``mkdir`` for a directory. Both refuse an
    existing name atomically, so a target that is already taken loses here, in
    the kernel, with no window in which this process decides anything. The move
    then lands on the empty placeholder this call just created — plain ``rename``
    replaces a file, and replaces a directory only when it is empty.

    Why not ``linkat``, which never replaces at all: it needs permission to link
    the *source*, and ``fs.protected_hardlinks`` refuses that for a file the
    process neither owns nor can write. Disk images on these exports are owned by
    root while the application runs unprivileged, so that path fails ``EPERM`` on
    exactly the files this exists for. ``rename`` needs write permission on the
    two directories, which is what the export actually grants.

    The residual window: another writer would have to delete the placeholder this
    call created and put its own file there, in between the two calls, for the
    rename to land on something real. That is a far narrower opening than the
    ``exists()``-then-``rename`` this replaces, and it cannot happen by accident —
    it requires deleting a file the deleter did not create.
    """
    # Each syscall below passes a single validated component together with a
    # `dir_fd`, so the kernel resolves it against a directory descriptor this
    # process already walked with O_NOFOLLOW. The name cannot select a directory,
    # and `confined_path_component` refuses anything that is not one ordinary
    # component, so it cannot traverse either.
    #
    # That validator is imported rather than defined here, and the import is
    # load-bearing: it is what lets the CodeQL model recognise the check. Inlining
    # it turns these five lines back into py/path-injection findings. See
    # core/services/confined_names.py.
    try:
        source_name = confined_path_component(source_name)
        target_name = confined_path_component(target_name)
    except ConfinedNameError as exc:
        raise ConfinedFilesystemError(str(exc)) from exc
    if source_is_directory:
        try:
            os.mkdir(target_name, mode=0o750, dir_fd=target_parent_fd)
        except FileExistsError as exc:
            raise ConfinedPathExistsError("Target already exists.") from exc
        except OSError as exc:
            raise ConfinedFilesystemError("Secure rename failed.") from exc
    else:
        try:
            reserved_fd = os.open(target_name, _EXCLUSIVE_FILE_FLAGS, mode=0o600, dir_fd=target_parent_fd)
        except FileExistsError as exc:
            raise ConfinedPathExistsError("Target already exists.") from exc
        except OSError as exc:
            raise ConfinedFilesystemError("Secure rename failed.") from exc
        os.close(reserved_fd)

    try:
        os.rename(source_name, target_name, src_dir_fd=source_parent_fd, dst_dir_fd=target_parent_fd)
    except OSError as exc:
        try:
            if source_is_directory:
                os.rmdir(target_name, dir_fd=target_parent_fd)
            else:
                os.unlink(target_name, dir_fd=target_parent_fd)
        except OSError:
            pass
        if exc.errno == errno.EXDEV:
            raise ConfinedCrossDeviceError("Rename would cross a filesystem boundary.") from exc
        if exc.errno == errno.ENOTEMPTY:
            raise ConfinedPathExistsError("Target already exists.") from exc
        raise ConfinedFilesystemError("Secure rename failed.") from exc

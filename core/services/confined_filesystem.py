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
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator


class ConfinedFilesystemError(Exception):
    """A requested path is invalid, unavailable, or unsafe to access."""


class ConfinedPathExistsError(ConfinedFilesystemError):
    """A no-replace operation found an existing target."""


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_RENAME_NOREPLACE = 1
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


def _renameat2_noreplace(parent_fd: int, source_name: str, target_name: str) -> None:
    if _RENAMEAT2 is None:
        raise ConfinedFilesystemError("Secure no-replace rename is unavailable on this Linux runtime.")
    result = _RENAMEAT2(
        parent_fd,
        os.fsencode(source_name),
        parent_fd,
        os.fsencode(target_name),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise ConfinedPathExistsError("Target file already exists.")
    raise ConfinedFilesystemError("Secure rename failed.") from OSError(error_number, os.strerror(error_number))

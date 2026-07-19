from __future__ import annotations

import grp
import os
import pwd
import stat
import subprocess
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AclEntry:
    entry_type: str
    principal: str
    permissions: str
    default: bool = False


@dataclass(frozen=True)
class StoragePermissions:
    ok: bool
    path: str = ""
    owner: str = ""
    owner_uid: int = -1
    group: str = ""
    group_gid: int = -1
    mode_octal: str = ""
    mode_symbolic: str = ""
    acl_entries: list[AclEntry] = field(default_factory=list)
    acl_available: bool = False
    error: str = ""


def storage_permissions(path: str) -> StoragePermissions:
    try:
        st = os.stat(path)
    except OSError as exc:
        return StoragePermissions(ok=False, path=path, error=str(exc))

    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)

    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)

    mode = stat.S_IMODE(st.st_mode)
    mode_symbolic = stat.filemode(st.st_mode)

    acl_entries, acl_available = _read_acl(path)

    return StoragePermissions(
        ok=True,
        path=path,
        owner=owner,
        owner_uid=st.st_uid,
        group=group,
        group_gid=st.st_gid,
        mode_octal=f"{mode:04o}",
        mode_symbolic=mode_symbolic,
        acl_entries=acl_entries,
        acl_available=acl_available,
    )


def _read_acl(path: str) -> tuple[list[AclEntry], bool]:
    try:
        result = subprocess.run(
            ["getfacl", "-p", "--no-effective", path],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return [], False

    if result.returncode != 0:
        return [], False

    entries: list[AclEntry] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        is_default = line.startswith("default:")
        if is_default:
            line = line[len("default:") :]

        parts = line.split(":", 2)
        if len(parts) != 3:
            continue

        entry_type, principal, permissions = parts
        if entry_type not in ("user", "group", "mask", "other"):
            continue

        entries.append(
            AclEntry(
                entry_type=entry_type,
                principal=principal,
                permissions=permissions,
                default=is_default,
            )
        )

    return entries, True

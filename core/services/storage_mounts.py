"""Confined mount resolution, liveness checks and binding ownership."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction

from core.models import ClusterStorage, ClusterStorageMount, StorageMount
from core.services.refs import MountRef
from core.services.storage_backends import StorageBackendProfile
from core.services.storage_paths import (
    StorageMountError,
    storage_mount_root,
)


def resolve_storage_mount(reference: str, *, enabled: bool | None = None) -> StorageMount:
    """Resolve immutable mount identity, with an unambiguous legacy-ID fallback."""
    value = str(reference or "").strip()
    query = StorageMount.objects.all()
    if enabled is not None:
        query = query.filter(enabled=enabled)
    try:
        mount_key = MountRef.parse(value).mount_key
    except ValueError:
        try:
            mount_key = str(uuid.UUID(value))
        except (TypeError, ValueError, AttributeError):
            mount_key = ""
    if mount_key:
        return query.get(mount_key=mount_key)
    matches = list(query.filter(storage_id=value)[:2])
    if len(matches) != 1:
        raise StorageMount.DoesNotExist
    return matches[0]


@dataclass(frozen=True)
class MountHealth:
    available: bool
    writable: bool
    reason: str = ""
    filesystem_type: str = ""


def normalized_backend_identity(value: str) -> str:
    """Normalize the host part without changing case-sensitive export paths."""
    raw = str(value or "").strip()
    if "://" in raw or "@" in raw:
        raise StorageMountError("Backend identity must use a credential-free server:/export or //server/share value.")
    if raw.startswith("//"):
        server, separator, suffix = raw[2:].partition("/")
        return f"//{server.lower()}{separator}{suffix}" if server else ""
    server, separator, suffix = raw.partition(":")
    if separator and server:
        return f"{server.lower()}:{suffix}"
    return raw


def _unescape_mountinfo(value: str) -> str:
    for encoded, decoded in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        value = value.replace(encoded, decoded)
    return value


def mountinfo_entries(path: str = "/proc/self/mountinfo") -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                before, separator, after = line.rstrip("\n").partition(" - ")
                if not separator:
                    continue
                left = before.split()
                right = after.split()
                if len(left) < 5 or not right:
                    continue
                rows.append((_unescape_mountinfo(left[4]), right[0]))
    except OSError:
        return ()
    return tuple(rows)


def mount_health(mount: StorageMount, profile: StorageBackendProfile) -> MountHealth:
    if not mount.enabled:
        return MountHealth(False, False, "Mount registration is disabled.")
    try:
        root = storage_mount_root(mount)
    except StorageMountError as exc:
        return MountHealth(False, False, str(exc))
    # storage_mount_root() accepts only ordinary relative components beneath the
    # fixed /storages root; this check intentionally probes that trusted path.
    if not root.is_dir():
        return MountHealth(False, False, "Mount path is unavailable.")
    fs_type = ""
    if profile.requires_mountpoint:
        exact = {path: filesystem for path, filesystem in mountinfo_entries()}.get(str(root))
        if exact is None:
            return MountHealth(False, False, "Mount unavailable; refusing the backing directory.")
        fs_type = exact
        expected = profile.expected_filesystems
        if expected and exact.lower() not in expected:
            return MountHealth(False, False, f"Unexpected filesystem type: {exact}.", exact)
    writable = bool(settings.STORAGE_WRITE_ENABLED and os.access(root, os.W_OK))
    reason = (
        ""
        if writable
        else ("Storage writes are disabled." if not settings.STORAGE_WRITE_ENABLED else "Mount is read-only.")
    )
    return MountHealth(True, writable, reason, fs_type)


def registered_mount_health(mount: StorageMount) -> MountHealth:
    from core.services.storage_backends import backend_profile

    definitions = [
        binding.cluster_storage for binding in mount.cluster_bindings.select_related("cluster_storage").all()
    ]
    if not definitions:
        # Transitional legacy mounts are still treated as explicit directory
        # registrations, never as guessed network mounts.
        return mount_health(mount, backend_profile("dir"))
    if any(scope_conflict(definition) for definition in definitions):
        return MountHealth(False, False, "Mount scope conflict; explicitly remap this storage.")
    health = [mount_health(mount, backend_profile(definition.storage_type)) for definition in definitions]
    failed = next((item for item in health if not item.available), None)
    if failed:
        return failed
    nonwritable = next((item for item in health if not item.writable), None)
    return nonwritable or health[0]


@transaction.atomic
def bind_storage_mount(*, cluster_storage: ClusterStorage, mount: StorageMount, node: str = "") -> ClusterStorageMount:
    definition = ClusterStorage.objects.select_for_update().get(pk=cluster_storage.pk)
    bindings = list(ClusterStorageMount.objects.select_for_update().filter(cluster_storage=definition))
    if definition.shared:
        if node:
            raise StorageMountError("Shared storage requires a shared mount association.")
        if any(binding.scope != ClusterStorageMount.Scope.SHARED for binding in bindings):
            raise StorageMountError("Mount scope conflict; remap the existing node associations first.")
        binding, _ = ClusterStorageMount.objects.update_or_create(
            cluster_storage=definition,
            node=None,
            defaults={"mount": mount, "scope": ClusterStorageMount.Scope.SHARED},
        )
        return binding
    if not node:
        raise StorageMountError("Node-local storage requires an explicit node.")
    if any(binding.scope != ClusterStorageMount.Scope.NODE for binding in bindings):
        raise StorageMountError("Mount scope conflict; remap the existing shared association first.")
    binding, _ = ClusterStorageMount.objects.update_or_create(
        cluster_storage=definition,
        node=node,
        defaults={"mount": mount, "scope": ClusterStorageMount.Scope.NODE},
    )
    return binding


@transaction.atomic
def unbind_storage_mount(binding: ClusterStorageMount) -> None:
    definition = ClusterStorage.objects.select_for_update().get(pk=binding.cluster_storage_id)
    locked = (
        ClusterStorageMount.objects.select_for_update()
        .filter(
            pk=binding.pk,
            cluster_storage=definition,
        )
        .first()
    )
    if locked is None:
        raise StorageMountError("Mount association no longer exists.")
    locked.delete()


def scope_conflict(definition: ClusterStorage) -> bool:
    scopes = set(definition.mount_bindings.values_list("scope", "node"))
    if definition.shared:
        return (
            any(scope != ClusterStorageMount.Scope.SHARED or node is not None for scope, node in scopes)
            or len(scopes) > 1
        )
    return any(scope != ClusterStorageMount.Scope.NODE or not node for scope, node in scopes)

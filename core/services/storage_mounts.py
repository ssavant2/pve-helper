"""Confined mount resolution, liveness checks and binding ownership."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction

from core.models import ClusterStorage, ClusterStorageMount, StorageMount
from core.services.filesystem import mountinfo_mounts
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


def _identity_parts(identity: str) -> tuple[str, str]:
    """Split a normalized identity into its host and its export path."""
    raw = str(identity or "").strip()
    if raw.startswith("//"):
        server, _separator, share = raw[2:].partition("/")
        return server, share.strip("/")
    server, separator, path = raw.partition(":")
    if not separator:
        return "", raw.strip("/")
    return server, path.strip("/")


def _hosts(value: str) -> str:
    """Canonicalize a possibly multi-valued Proxmox host list."""
    hosts = sorted({part.strip().lower() for part in str(value or "").replace(";", ",").split(",") if part.strip()})
    return ",".join(hosts)


def backend_identity_from_definition(definition: ClusterStorage) -> str:
    """Compose the cross-cluster backend identity from the Proxmox definition.

    Returns "" when the configuration genuinely does not carry one — a `dir`
    path says nothing about which physical backend is behind it, and an
    internally hyperconverged Ceph pool is only addressable inside its own
    cluster. "Not stated" must stay distinguishable from "not the same".
    """
    storage_type = str(definition.storage_type or "").strip().lower()
    config = definition.config or {}

    def value(*names: str) -> str:
        for name in names:
            found = str(config.get(name) or "").strip()
            if found:
                return found
        return ""

    if storage_type in {"nfs", "cifs", "glusterfs"}:
        server = value("server")
        export = value("export", "share", "volume")
        if not server or not export:
            return ""
        if storage_type == "cifs":
            raw = f"//{server}/{export.lstrip('/')}"
        elif storage_type == "glusterfs":
            raw = f"{server}:{export.lstrip('/')}"
        else:
            raw = f"{server}:/{export.lstrip('/')}"
        try:
            return normalized_backend_identity(raw)
        except StorageMountError:
            return ""
    if storage_type in {"cephfs", "rbd"}:
        monitors = _hosts(value("monhost"))
        target = value("pool", "subdir") or "/"
        if not monitors:
            # Hyperconverged Ceph: reachable only from its own cluster, so the
            # cluster key is the identity rather than an unknown.
            return f"{storage_type}://cluster:{definition.cluster.key}/{target.strip('/')}"
        namespace = value("namespace")
        suffix = f"/{namespace}" if namespace else ""
        return f"{storage_type}://{monitors}/{target.strip('/')}{suffix}"
    if storage_type in {"iscsi", "iscsidirect", "zfs"}:
        portal = _hosts(value("portal"))
        target = value("target")
        if not portal or not target:
            return ""
        pool = value("pool")
        return f"iscsi://{portal}/{target.lower()}" + (f"/{pool}" if pool else "")
    if storage_type == "pbs":
        server = value("server").lower()
        datastore = value("datastore")
        if not server or not datastore:
            return ""
        return f"pbs://{server}/{datastore}"
    if storage_type == "lvm":
        vgname = value("vgname")
        return f"lvm://{vgname}" if vgname else ""
    return ""


def derived_backend_identity(definition: ClusterStorage) -> str:
    """The identity the mount-registration form prefills.

    This is the value the operator should almost never have to type: the same
    NAS export registered in two clusters derives byte-identically in both,
    which is exactly what the cross-cluster reference check requires. Limited to
    the backends whose identity is also a valid ``server:/export`` mount value;
    the block backends carry their identity in a scheme this field cannot hold.
    """
    if str(definition.storage_type or "").strip().lower() not in {"nfs", "cifs", "glusterfs"}:
        return ""
    return backend_identity_from_definition(definition)


def near_match_mounts(identity: str) -> list[StorageMount]:
    """Registered mounts exporting the same path under a differently spelled host.

    A short hostname in one cluster and its FQDN in another are the common human
    variation, and they defeat the byte-equality the cross-cluster check relies
    on — silently, and in the direction that offers an in-use disk for deletion.
    """
    server, path = _identity_parts(identity)
    if not path:
        return []
    matches = []
    for mount in StorageMount.objects.exclude(backend_identity="").exclude(backend_identity=identity):
        other_server, other_path = _identity_parts(mount.backend_identity)
        if other_path == path and other_server != server:
            matches.append(mount)
    return matches


@dataclass(frozen=True)
class BackendAgreement:
    """Whether a host mount really comes from the export its datastore names."""

    agrees: bool
    reason: str = ""


def _is_descendant(child: str, parent: str) -> bool:
    return bool(parent) and child.startswith(f"{parent}/")


def compare_mount_source(identity: str, source: str) -> BackendAgreement:
    """Check a proposed backend identity against the kernel's mount source.

    A binding is the sole authority for which directory a datastore's files live
    in — nothing downstream re-derives it — so pairing a datastore with the wrong
    host mount silently redirects every scan, classification and file action to
    another export. The Orphan Finder then compares one export's volume list
    against another's files, finds no matches, and offers live disks for
    deletion. Both halves of that pairing are knowable here, so they are compared
    here.

    Disagreement is reported, never decided: a mount of a subdirectory of the
    export is legitimate, and so is a NAS reachable under two spellings. The
    caller escalates to an explicit confirmation rather than refusing outright.
    """
    expected = str(identity or "").strip()
    observed = str(source or "").strip()
    # Nothing to compare: a `dir` datastore publishes no export, and a plain
    # directory that is not a mount point of its own has no source.
    if not expected or not observed:
        return BackendAgreement(True)
    try:
        expected = normalized_backend_identity(expected)
        observed = normalized_backend_identity(observed)
    except StorageMountError:
        # An exotic source this field cannot express is not evidence of a
        # mismatch; the mount-health check still governs whether it is usable.
        return BackendAgreement(True)
    if expected == observed:
        return BackendAgreement(True)
    expected_host, expected_path = _identity_parts(expected)
    observed_host, observed_path = _identity_parts(observed)
    if expected_path == observed_path:
        return BackendAgreement(
            False,
            f"The datastore names host '{expected_host}' and the mount was made from "
            f"'{observed_host}'. If that is the same server under two spellings, register the "
            "identity the other clusters derive, or their shared-backend check cannot fire.",
        )
    if expected_host == observed_host and _is_descendant(observed_path, expected_path):
        return BackendAgreement(
            False,
            f"'{observed}' is a subdirectory of the datastore's export '{expected}'. "
            "PVE-helper will then see only part of the datastore, and anything outside the "
            "mounted subtree counts as absent.",
        )
    if expected_host == observed_host and _is_descendant(expected_path, observed_path):
        return BackendAgreement(
            False,
            f"'{observed}' is mounted above the datastore's export '{expected}' and so contains "
            "more than this datastore. Files belonging to other datastores would be scanned as "
            "if they belonged to this one.",
        )
    return BackendAgreement(
        False,
        f"The datastore is on '{expected}' but this directory is mounted from '{observed}'. "
        "These are different exports: every scan, classification and file action for this "
        "datastore would be applied to the wrong one.",
    )


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
        exact = {mount.mount_point: mount.filesystem_type for mount in mountinfo_mounts()}.get(str(root))
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


def scope_conflict(definition: ClusterStorage, *, bindings: Iterable[ClusterStorageMount] | None = None) -> bool:
    """Whether the storage's mount bindings disagree with its own scope.

    A caller that already holds the bindings passes them in: `values_list` always
    reaches the database, so resolving them here would defeat a prefetch and cost
    one query per definition on a page that lists many.
    """
    rows = definition.mount_bindings.all() if bindings is None else bindings
    scopes = {(row.scope, row.node) for row in rows}
    if definition.shared:
        return (
            any(scope != ClusterStorageMount.Scope.SHARED or node is not None for scope, node in scopes)
            or len(scopes) > 1
        )
    return any(scope != ClusterStorageMount.Scope.NODE or not node for scope, node in scopes)


def mount_datastore_scope(mount: StorageMount):
    """The datastore page a host mount's files are browsed on.

    A mount is bound to one or more storage scopes; its file tree is the same
    tree whichever one you arrive through, so the binding is resolved in a fixed
    order rather than asking the caller to carry a scope through every file
    operation. Returns None for a mount with no binding left, which can only
    happen while an operator is unbinding it.
    """
    binding = (
        mount.cluster_bindings.select_related("cluster_storage__cluster")
        .order_by("cluster_storage__cluster__key", "node")
        .first()
    )
    if binding is None:
        return None
    definition = binding.cluster_storage
    node = "" if definition.shared else (binding.node or "")
    return definition.cluster.key, definition.storage_id, node

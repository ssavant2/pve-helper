"""Versioned, cluster-qualified object references.

Node names are unique only *within* a Proxmox cluster: two independent clusters may
both have a `pve1`. A bare node name is therefore not a safe reference outside a
known cluster context, and treating one as identity is how evidence from the wrong
cluster gets accepted.

`NodeRef` landed here first because scan coverage and the storage gate needed it
before client resolution moved. `GuestRef` — (cluster_key, object_type, vmid) —
shares this serializer: there is exactly one durable reference format, not several
ad-hoc strings with subtly different field order.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from urllib.parse import quote, unquote

# Serialized references are durable: they reach queue payloads and audit rows and
# must stay parseable across deploys, so the format carries its own version.
NODE_REF_VERSION = "nr1"
GUEST_REF_VERSION = "gr1"

_CLUSTER_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class RefParseError(ValueError):
    """A serialized reference was absent, malformed or of an unknown version."""


@dataclass(frozen=True)
class ClusterStorageRef:
    cluster_key: str
    storage_id: str

    def __post_init__(self) -> None:
        if not self.cluster_key or not self.storage_id or ":" in self.cluster_key:
            raise RefParseError("Invalid cluster storage reference.")

    def serialize(self) -> str:
        return f"sr1:{quote(self.cluster_key, safe='')}:{quote(self.storage_id, safe='')}"

    @classmethod
    def parse(cls, raw: str) -> ClusterStorageRef:
        parts = str(raw or "").split(":")
        if len(parts) != 3 or parts[0] != "sr1":
            raise RefParseError("Invalid cluster storage reference.")
        return cls(unquote(parts[1]), unquote(parts[2]))


@dataclass(frozen=True)
class StorageInstanceRef:
    cluster_key: str
    storage_id: str
    node: str = ""

    def serialize(self) -> str:
        return ":".join(
            ("si1", quote(self.cluster_key, safe=""), quote(self.storage_id, safe=""), quote(self.node, safe=""))
        )

    @classmethod
    def parse(cls, raw: str) -> StorageInstanceRef:
        parts = str(raw or "").split(":")
        if len(parts) != 4 or parts[0] != "si1":
            raise RefParseError("Invalid storage instance reference.")
        values = [unquote(value) for value in parts[1:]]
        if not values[0] or not values[1]:
            raise RefParseError("Invalid storage instance reference.")
        return cls(*values)


@dataclass(frozen=True)
class VolumeRef:
    instance: StorageInstanceRef
    volid: str

    def serialize(self) -> str:
        return f"vr1:{quote(self.instance.serialize(), safe='')}:{quote(self.volid, safe='')}"

    @classmethod
    def parse(cls, raw: str) -> VolumeRef:
        parts = str(raw or "").split(":")
        if len(parts) != 3 or parts[0] != "vr1":
            raise RefParseError("Invalid volume reference.")
        return cls(StorageInstanceRef.parse(unquote(parts[1])), unquote(parts[2]))


@dataclass(frozen=True)
class MountRef:
    mount_key: str

    def __post_init__(self) -> None:
        try:
            uuid.UUID(str(self.mount_key))
        except (TypeError, ValueError, AttributeError) as exc:
            raise RefParseError("Invalid mount key.") from exc

    def serialize(self) -> str:
        return f"mr1:{self.mount_key}"

    @classmethod
    def parse(cls, raw: str) -> MountRef:
        parts = str(raw or "").split(":")
        if len(parts) != 2 or parts[0] != "mr1" or not parts[1]:
            raise RefParseError("Invalid mount reference.")
        return cls(parts[1])


@dataclass(frozen=True)
class NodeRef:
    """A node, qualified by the immutable key of the cluster it belongs to."""

    cluster_key: str
    node: str

    def __post_init__(self) -> None:
        if not _CLUSTER_KEY_RE.match(self.cluster_key or ""):
            raise RefParseError(f"Invalid cluster key in node reference: {self.cluster_key!r}")
        if not self.node or ":" in self.node:
            raise RefParseError(f"Invalid node name in node reference: {self.node!r}")

    def serialize(self) -> str:
        return f"{NODE_REF_VERSION}:{self.cluster_key}:{self.node}"

    @classmethod
    def parse(cls, raw: str) -> NodeRef:
        if not raw:
            raise RefParseError("Empty node reference.")
        parts = raw.split(":", 2)
        if len(parts) != 3:
            raise RefParseError(f"Malformed node reference: {raw!r}")
        version, cluster_key, node = parts
        if version != NODE_REF_VERSION:
            raise RefParseError(f"Unsupported node reference version {version!r} in {raw!r}")
        return cls(cluster_key=cluster_key, node=node)

    def __str__(self) -> str:
        return self.serialize()


@dataclass(frozen=True)
class GuestRef:
    """A VM/CT qualified by the immutable key of its Proxmox cluster.

    ``node`` is current location metadata, not part of stable guest identity. It
    may be carried when a live operation needs to prove which node was selected,
    but callers compare guests through ``identity_tuple``.
    """

    cluster_key: str
    object_type: str
    vmid: int
    node: str = ""

    def __post_init__(self) -> None:
        if not _CLUSTER_KEY_RE.match(self.cluster_key or ""):
            raise RefParseError(f"Invalid cluster key in guest reference: {self.cluster_key!r}")
        if self.object_type not in {"vm", "ct"}:
            raise RefParseError(f"Invalid object type in guest reference: {self.object_type!r}")
        if isinstance(self.vmid, bool) or not isinstance(self.vmid, int) or self.vmid <= 0:
            raise RefParseError(f"Invalid VMID in guest reference: {self.vmid!r}")
        if self.node and (":" in self.node or "@" in self.node):
            raise RefParseError(f"Invalid node name in guest reference: {self.node!r}")

    @property
    def identity_tuple(self) -> tuple[str, str, int]:
        return (self.cluster_key, self.object_type, self.vmid)

    @property
    def node_ref(self) -> NodeRef | None:
        if not self.node:
            return None
        return NodeRef(cluster_key=self.cluster_key, node=self.node)

    def without_node(self) -> GuestRef:
        if not self.node:
            return self
        return GuestRef(
            cluster_key=self.cluster_key,
            object_type=self.object_type,
            vmid=self.vmid,
        )

    def serialize(self, *, include_node: bool = True) -> str:
        stable = f"{GUEST_REF_VERSION}:{self.cluster_key}:{self.object_type}:{self.vmid}"
        return f"{stable}@{self.node}" if include_node and self.node else stable

    @classmethod
    def parse(cls, raw: str) -> GuestRef:
        if not raw:
            raise RefParseError("Empty guest reference.")
        stable, separator, node = raw.partition("@")
        if separator and not node:
            raise RefParseError(f"Malformed guest reference: {raw!r}")
        parts = stable.split(":", 3)
        if len(parts) != 4:
            raise RefParseError(f"Malformed guest reference: {raw!r}")
        version, cluster_key, object_type, vmid_text = parts
        if version != GUEST_REF_VERSION:
            raise RefParseError(f"Unsupported guest reference version {version!r} in {raw!r}")
        try:
            vmid = int(vmid_text)
        except (TypeError, ValueError) as exc:
            raise RefParseError(f"Invalid VMID in guest reference: {vmid_text!r}") from exc
        return cls(
            cluster_key=cluster_key,
            object_type=object_type,
            vmid=vmid,
            node=node,
        )

    def __str__(self) -> str:
        return self.serialize()

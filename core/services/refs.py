"""Versioned, cluster-qualified object references.

Node names are unique only *within* a Proxmox cluster: two independent clusters may
both have a `pve1`. A bare node name is therefore not a safe reference outside a
known cluster context, and treating one as identity is how evidence from the wrong
cluster gets accepted.

`NodeRef` lands here first because scan coverage and the storage gate need it before
client resolution moves. `GuestRef` — (cluster_key, object_type, vmid) — joins it in
Phase 3 and shares this serializer: there must be exactly one durable reference
format, not several ad-hoc strings with subtly different field order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# Serialized references are durable: they reach queue payloads and audit rows and
# must stay parseable across deploys, so the format carries its own version.
NODE_REF_VERSION = "nr1"

_CLUSTER_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class RefParseError(ValueError):
    """A serialized reference was absent, malformed or of an unknown version."""


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

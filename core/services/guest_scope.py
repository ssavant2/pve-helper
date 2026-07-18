"""Temporary Phase-3 adapters from legacy guest identity to ``GuestRef``.

Only entry points whose URL/form contract is migrated in Phase 4 may call this
module. It is intentionally tiny so activation can delete it as one unit.
"""

from __future__ import annotations

from core.services.cluster_resolver import require_sole_enabled_cluster_for_legacy_caller
from core.services.refs import GuestRef


def guest_ref_from_legacy_identity(
    object_type: str,
    vmid: int,
    *,
    node: str = "",
) -> GuestRef:
    cluster = require_sole_enabled_cluster_for_legacy_caller()
    return GuestRef(cluster.key, object_type, int(vmid), node)

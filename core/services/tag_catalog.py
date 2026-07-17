from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.db.models import Q

from core.models import CurrentGuestInventory
from core.services.current_guest_inventory import current_inventory_state
from core.services.tag_registry import registered_tags, resolve_tag_registry_cluster
from core.services.tags import RegisteredTag, TagChip, TagSummary, inventory_rows, parse_tags, tag_chip


def _inventory_errors(raw) -> tuple[str, ...]:
    if not isinstance(raw, dict):
        return ()
    errors: list[str] = []
    for value in raw.values():
        if isinstance(value, (list, tuple)):
            errors.extend(str(item) for item in value if item)
        elif value:
            errors.append(str(value))
    return tuple(errors)


@dataclass(frozen=True)
class TagCatalog:
    cluster_key: str
    registered: dict[str, RegisteredTag]
    assigned: tuple[str, ...]
    available: tuple[str, ...]
    summaries: tuple[TagSummary, ...]
    guests: tuple[CurrentGuestInventory, ...]
    registry_error: str
    inventory_refreshed_at: datetime | None
    inventory_complete: bool
    inventory_errors: tuple[str, ...]
    endpoints_attempted: tuple[str, ...]
    endpoints_succeeded: tuple[str, ...]

    @property
    def degraded(self) -> bool:
        return bool(self.registry_error or self.inventory_errors or not self.inventory_complete)

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(item for item in (self.registry_error, *self.inventory_errors) if item)

    def chip(self, name: str) -> TagChip:
        return tag_chip(name, self.registered)

    def chips(self, names) -> list[TagChip]:
        return [self.chip(name) for name in parse_tags(names)]

    def metadata(self) -> dict[str, object]:
        return {
            "degraded": self.degraded,
            "errors": list(self.errors),
            "inventory_refreshed_at": self.inventory_refreshed_at.isoformat()
            if self.inventory_refreshed_at
            else None,
            "inventory_complete": self.inventory_complete,
            "endpoints_attempted": list(self.endpoints_attempted),
            "endpoints_succeeded": list(self.endpoints_succeeded),
        }


def load_tag_catalog(*, cluster=None) -> TagCatalog:
    cluster, cluster_error = resolve_tag_registry_cluster(cluster)
    registered, registry_error = registered_tags(cluster=cluster) if cluster else ({}, cluster_error)
    guests = tuple(
        CurrentGuestInventory.objects.filter(Q(cluster=cluster) | Q(cluster__isnull=True)).order_by(
            "node", "vmid"
        )
        if cluster
        else ()
    )
    assigned = tuple(
        sorted(
            {
                name
                for guest in guests
                for name in parse_tags(guest.config)
            },
            key=str.casefold,
        )
    )
    available = tuple(sorted(set(registered) | set(assigned), key=str.casefold))
    state = current_inventory_state(cluster) if cluster else None
    return TagCatalog(
        cluster_key=cluster.key if cluster else "",
        registered=registered,
        assigned=assigned,
        available=available,
        summaries=tuple(inventory_rows(guests, registered)),
        guests=guests,
        registry_error=registry_error,
        inventory_refreshed_at=state.refreshed_at if state else None,
        inventory_complete=bool(state and state.complete),
        inventory_errors=_inventory_errors(state.errors if state else {}),
        endpoints_attempted=tuple(state.endpoints_attempted if state else ()),
        endpoints_succeeded=tuple(state.endpoints_succeeded if state else ()),
    )

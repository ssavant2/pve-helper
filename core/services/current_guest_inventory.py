from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import (
    CurrentGuestInventory,
    CurrentGuestInventoryState,
    ProxmoxEndpoint,
    ProxmoxInventory,
    ScanRun,
)
from core.services.classification import extract_disk_references
from core.services.proxmox import ProxmoxGuestSummary, VerifiedGuestInventory
from core.services.tags import join_tags


GUEST_TYPES = (ProxmoxInventory.ObjectType.VM, ProxmoxInventory.ObjectType.CT)


@dataclass(frozen=True)
class ScanGuestObservation:
    endpoint: ProxmoxEndpoint
    guest: Any


def current_inventory_state() -> CurrentGuestInventoryState | None:
    return CurrentGuestInventoryState.objects.filter(pk=1).first()


def current_guest_queryset():
    return CurrentGuestInventory.objects.filter(object_type__in=GUEST_TYPES)


def _identity_filter(keys: set[tuple[str, int]]) -> Q:
    query = Q()
    for object_type, vmid in keys:
        query |= Q(object_type=object_type, vmid=vmid)
    return query


def _delete_missing(queryset, observed: set[tuple[str, int]]) -> None:
    if observed:
        queryset.exclude(_identity_filter(observed)).delete()
    else:
        queryset.delete()


def _update_state(
    *,
    refreshed_at,
    complete: bool,
    attempted: list[str],
    succeeded: list[str],
    errors: dict,
    source_scan: ScanRun | None,
) -> CurrentGuestInventoryState:
    state, _created = CurrentGuestInventoryState.objects.select_for_update().get_or_create(pk=1)
    state.refreshed_at = refreshed_at
    if complete:
        state.last_complete_at = refreshed_at
    state.source_scan = source_scan
    state.complete = complete
    state.endpoints_attempted = attempted
    state.endpoints_succeeded = succeeded
    state.errors = errors
    state.save()
    return state


@transaction.atomic
def reconcile_scan_guest_inventory(
    *,
    scan: ScanRun,
    observations: Iterable[ScanGuestObservation],
    attempted_endpoints: Iterable[ProxmoxEndpoint],
    successful_endpoints: Iterable[ProxmoxEndpoint],
    errors: dict,
    observed_at=None,
) -> CurrentGuestInventoryState:
    observed_at = observed_at or timezone.now()
    observations = [item for item in observations if item.guest.object_type in GUEST_TYPES and item.guest.vmid is not None]
    attempted = list(attempted_endpoints)
    succeeded = list(successful_endpoints)
    complete = bool(attempted) and {item.pk for item in attempted} == {item.pk for item in succeeded}

    observed_by_endpoint: dict[int, set[tuple[str, int]]] = {endpoint.pk: set() for endpoint in succeeded}
    all_observed: set[tuple[str, int]] = set()
    for item in observations:
        key = (item.guest.object_type, int(item.guest.vmid))
        all_observed.add(key)
        if item.endpoint.pk in observed_by_endpoint:
            observed_by_endpoint[item.endpoint.pk].add(key)

    if complete:
        _delete_missing(CurrentGuestInventory.objects.all(), all_observed)
    else:
        for endpoint in succeeded:
            _delete_missing(
                CurrentGuestInventory.objects.filter(source_endpoint=endpoint),
                observed_by_endpoint.get(endpoint.pk, set()),
            )

    for item in observations:
        guest = item.guest
        CurrentGuestInventory.objects.update_or_create(
            object_type=guest.object_type,
            vmid=int(guest.vmid),
            defaults={
                "source_endpoint": item.endpoint,
                "source_scan": scan,
                "node": guest.node,
                "name": guest.name,
                "status": guest.status,
                "config": dict(guest.config or {}),
                "config_complete": True,
                "disk_references": list(guest.disk_references or []),
                "observed_at": observed_at,
            },
        )

    return _update_state(
        refreshed_at=observed_at,
        complete=complete,
        attempted=[item.name for item in attempted],
        succeeded=[item.name for item in succeeded],
        errors=errors,
        source_scan=scan,
    )


def _endpoint_for_live_guest(
    guest: ProxmoxGuestSummary,
    endpoints: list[ProxmoxEndpoint],
) -> ProxmoxEndpoint | None:
    matches = [
        endpoint
        for endpoint in endpoints
        if endpoint.name == guest.node or str((endpoint.details or {}).get("node") or "") == guest.node
    ]
    return matches[0] if len(matches) == 1 else None


def _live_config(existing: CurrentGuestInventory | None, guest: ProxmoxGuestSummary) -> dict:
    config = dict(existing.config or {}) if existing else {}
    if guest.tags:
        config["tags"] = join_tags(guest.tags)
    else:
        config.pop("tags", None)
    if guest.object_type == ProxmoxInventory.ObjectType.VM:
        if guest.is_template:
            config["template"] = "1"
        else:
            config.pop("template", None)
    return config


@transaction.atomic
def reconcile_live_guest_inventory(
    inventory: VerifiedGuestInventory,
    *,
    observed_at=None,
) -> CurrentGuestInventoryState:
    observed_at = observed_at or timezone.now()
    observed = {(guest.object_type, guest.vmid) for guest in inventory.guests}
    endpoints = list(ProxmoxEndpoint.objects.filter(enabled=True))
    if inventory.complete:
        _delete_missing(CurrentGuestInventory.objects.all(), observed)

    for guest in inventory.guests:
        existing = CurrentGuestInventory.objects.filter(
            object_type=guest.object_type,
            vmid=guest.vmid,
        ).first()
        endpoint = _endpoint_for_live_guest(guest, endpoints)
        config = _live_config(existing, guest)
        CurrentGuestInventory.objects.update_or_create(
            object_type=guest.object_type,
            vmid=guest.vmid,
            defaults={
                "source_endpoint": endpoint or (existing.source_endpoint if existing else None),
                "source_scan": existing.source_scan if existing else None,
                "node": guest.node,
                "name": guest.name,
                "status": guest.status,
                "config": config,
                "config_complete": existing.config_complete if existing else False,
                "disk_references": list(existing.disk_references or []) if existing else [],
                "observed_at": observed_at,
            },
        )

    return _update_state(
        refreshed_at=observed_at,
        complete=inventory.complete,
        attempted=list(inventory.attempted_endpoints),
        succeeded=list(inventory.successful_endpoints),
        errors={"live_inventory": list(inventory.errors)} if inventory.errors else {},
        source_scan=(current_inventory_state() or CurrentGuestInventoryState()).source_scan,
    )


@transaction.atomic
def update_current_guest_config(*, object_type: str, vmid: int, node: str = "", updates=None, delete=None) -> None:
    observed_at = timezone.now()
    guest, _created = CurrentGuestInventory.objects.select_for_update().get_or_create(
        object_type=object_type,
        vmid=vmid,
        defaults={
            "node": node,
            "config": {},
            "config_complete": False,
            "observed_at": observed_at,
        },
    )
    config = dict(guest.config or {})
    config.update(updates or {})
    for key in delete or []:
        config.pop(key, None)
    guest.config = config
    if node:
        guest.node = node
    guest.observed_at = observed_at
    guest.save(update_fields=["config", "node", "observed_at", "updated_at"])


def upsert_current_guest(
    *,
    object_type: str,
    vmid: int,
    node: str,
    name: str,
    status: str,
    config: dict,
) -> CurrentGuestInventory:
    return CurrentGuestInventory.objects.update_or_create(
        object_type=object_type,
        vmid=vmid,
        defaults={
            "node": node,
            "name": name,
            "status": status,
            "config": config,
            "config_complete": True,
            "disk_references": extract_disk_references(config),
            "observed_at": timezone.now(),
        },
    )[0]


def delete_current_guest(*, object_type: str, vmid: int) -> None:
    CurrentGuestInventory.objects.filter(object_type=object_type, vmid=vmid).delete()

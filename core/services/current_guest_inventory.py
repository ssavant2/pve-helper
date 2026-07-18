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
from core.services.proxmox import ProxmoxAPIError, ProxmoxGuestSummary, VerifiedGuestInventory
from core.services.tags import join_tags


GUEST_TYPES = (ProxmoxInventory.ObjectType.VM, ProxmoxInventory.ObjectType.CT)


@dataclass(frozen=True)
class ScanGuestObservation:
    endpoint: ProxmoxEndpoint
    guest: Any


@dataclass(frozen=True)
class TargetedGuestRefresh:
    found: bool
    absent: bool = False
    node: str = ""
    error: str = ""


def current_inventory_state(cluster) -> CurrentGuestInventoryState | None:
    """Return the freshness/coverage record for one explicit cluster."""
    if cluster is None:
        return None
    return CurrentGuestInventoryState.objects.filter(cluster=cluster).first()


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
    cluster,
    refreshed_at,
    complete: bool,
    attempted: list[str],
    succeeded: list[str],
    errors: dict,
    source_scan: ScanRun | None,
    unreachable: bool = False,
) -> CurrentGuestInventoryState:
    state, _created = CurrentGuestInventoryState.objects.select_for_update().get_or_create(cluster=cluster)
    # A cluster whose every endpoint failed keeps its last-known freshness: guests
    # are unknown, not absent, so refreshed_at does not advance and nothing retires.
    if not unreachable:
        state.refreshed_at = refreshed_at
        if complete:
            state.last_complete_at = refreshed_at
        state.source_scan = source_scan
    state.complete = complete
    state.unreachable = unreachable
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

    # Completeness and absence are evaluated per cluster: cluster A succeeding says
    # nothing about cluster B, and a partial answer in one cluster must never retire
    # another cluster's rows. Group everything the scan saw by the cluster it came
    # from, then reconcile each cluster independently against only its own data.
    cluster_ids = {ep.cluster_id for ep in attempted if ep.cluster_id is not None}
    states: list[CurrentGuestInventoryState] = []
    for cluster_id in cluster_ids:
        cluster_attempted = [ep for ep in attempted if ep.cluster_id == cluster_id]
        cluster_succeeded = [ep for ep in succeeded if ep.cluster_id == cluster_id]
        cluster_obs = [item for item in observations if item.endpoint.cluster_id == cluster_id]
        cluster = cluster_attempted[0].cluster
        complete = bool(cluster_attempted) and (
            {ep.pk for ep in cluster_attempted} == {ep.pk for ep in cluster_succeeded}
        )

        observed_by_endpoint: dict[int, set[tuple[str, int]]] = {ep.pk: set() for ep in cluster_succeeded}
        all_observed: set[tuple[str, int]] = set()
        for item in cluster_obs:
            key = (item.guest.object_type, int(item.guest.vmid))
            all_observed.add(key)
            if item.endpoint.pk in observed_by_endpoint:
                observed_by_endpoint[item.endpoint.pk].add(key)

        cluster_rows = CurrentGuestInventory.objects.filter(cluster=cluster)
        if complete:
            _delete_missing(cluster_rows, all_observed)
        else:
            for endpoint in cluster_succeeded:
                _delete_missing(
                    cluster_rows.filter(source_endpoint=endpoint),
                    observed_by_endpoint.get(endpoint.pk, set()),
                )

        for item in cluster_obs:
            guest = item.guest
            CurrentGuestInventory.objects.update_or_create(
                cluster=cluster,
                object_type=guest.object_type,
                vmid=int(guest.vmid),
                defaults={
                    "source_endpoint": item.endpoint,
                    "source_scan": scan,
                    "node": guest.node,
                    "name": guest.name,
                    "status": guest.status,
                    "runtime_observed_at": observed_at,
                    "config": dict(guest.config or {}),
                    "config_complete": True,
                    "config_observed_at": observed_at,
                    "disk_references": list(guest.disk_references or []),
                    "observed_at": observed_at,
                },
            )

        states.append(
            _update_state(
                cluster=cluster,
                refreshed_at=observed_at,
                complete=complete,
                unreachable=bool(cluster_attempted) and not cluster_succeeded,
                attempted=[ep.name for ep in cluster_attempted],
                succeeded=[ep.name for ep in cluster_succeeded],
                errors=errors,
                source_scan=scan,
            )
        )

    # The scan orchestrator only uses this as a completion marker. Per-cluster
    # callers read their own state through current_inventory_state().
    return states[0] if states else None


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


def _int_or_zero(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float_or_zero(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


@transaction.atomic
def reconcile_live_guest_inventory(
    inventory: VerifiedGuestInventory,
    *,
    observed_at=None,
) -> CurrentGuestInventoryState:
    observed_at = observed_at or timezone.now()
    from core.models import ProxmoxCluster

    cluster = ProxmoxCluster.objects.filter(key=inventory.cluster_key).first() if inventory.cluster_key else None
    if cluster is None:
        raise ValueError("Verified guest inventory must carry a valid cluster key.")

    observed = {(guest.object_type, guest.vmid) for guest in inventory.guests}
    endpoints = list(ProxmoxEndpoint.objects.filter(cluster=cluster, enabled=True))
    cluster_rows = CurrentGuestInventory.objects.filter(cluster=cluster)
    # Only a complete answer may retire rows, and only within this cluster.
    if inventory.complete:
        _delete_missing(cluster_rows, observed)

    for guest in inventory.guests:
        existing = cluster_rows.filter(object_type=guest.object_type, vmid=guest.vmid).first()
        endpoint = _endpoint_for_live_guest(guest, endpoints)
        config = _live_config(existing, guest)
        CurrentGuestInventory.objects.update_or_create(
            cluster=cluster,
            object_type=guest.object_type,
            vmid=guest.vmid,
            defaults={
                "source_endpoint": endpoint or (existing.source_endpoint if existing else None),
                "source_scan": existing.source_scan if existing else None,
                "node": guest.node,
                "name": guest.name,
                "status": guest.status,
                "cpu_usage": guest.cpu,
                "memory_used_bytes": guest.mem,
                "memory_max_bytes": guest.maxmem,
                "disk_used_bytes": guest.disk,
                "disk_max_bytes": guest.maxdisk,
                "uptime_seconds": guest.uptime,
                "runtime_lock": guest.lock,
                "runtime_observed_at": observed_at,
                "config": config,
                "config_complete": existing.config_complete if existing else False,
                "config_observed_at": existing.config_observed_at if existing else None,
                "disk_references": list(existing.disk_references or []) if existing else [],
                "observed_at": observed_at,
            },
        )

    prior = current_inventory_state(cluster)
    return _update_state(
        cluster=cluster,
        refreshed_at=observed_at,
        complete=inventory.complete,
        unreachable=bool(inventory.attempted_endpoints) and not inventory.successful_endpoints,
        attempted=list(inventory.attempted_endpoints),
        succeeded=list(inventory.successful_endpoints),
        errors={"live_inventory": list(inventory.errors)} if inventory.errors else {},
        source_scan=prior.source_scan if prior else None,
    )


def _target_cluster(cluster=None, *, endpoint=None):
    """The cluster a targeted single-guest write belongs to.

    Prefer an explicit cluster, then the answering endpoint's cluster, then the sole
    enabled cluster — the same bounded adapter used elsewhere until Phase 3 threads a
    GuestRef through these call sites.
    """
    if cluster is not None:
        return cluster
    if endpoint is not None and endpoint.cluster_id is not None:
        return endpoint.cluster
    raise ValueError("Current guest writes require an explicit cluster or cluster-bound endpoint.")


@transaction.atomic
def update_current_guest_config(*, object_type: str, vmid: int, cluster, node: str = "", updates=None, delete=None) -> None:
    observed_at = timezone.now()
    guest, _created = CurrentGuestInventory.objects.select_for_update().get_or_create(
        cluster=_target_cluster(cluster),
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
    guest.config_observed_at = observed_at
    guest.save(update_fields=["config", "node", "observed_at", "config_observed_at", "updated_at"])


def upsert_current_guest(
    *,
    object_type: str,
    vmid: int,
    node: str,
    name: str,
    status: str,
    config: dict,
    cpu_usage: float = 0,
    memory_used_bytes: int = 0,
    memory_max_bytes: int = 0,
    disk_used_bytes: int = 0,
    disk_max_bytes: int = 0,
    uptime_seconds: int = 0,
    runtime_lock: str = "",
    cluster,
) -> CurrentGuestInventory:
    observed_at = timezone.now()
    return CurrentGuestInventory.objects.update_or_create(
        cluster=_target_cluster(cluster),
        object_type=object_type,
        vmid=vmid,
        defaults={
            "node": node,
            "name": name,
            "status": status,
            "cpu_usage": cpu_usage,
            "memory_used_bytes": memory_used_bytes,
            "memory_max_bytes": memory_max_bytes,
            "disk_used_bytes": disk_used_bytes,
            "disk_max_bytes": disk_max_bytes,
            "uptime_seconds": uptime_seconds,
            "runtime_lock": runtime_lock,
            "config": config,
            "config_complete": True,
            "config_observed_at": observed_at,
            "disk_references": extract_disk_references(config),
            "observed_at": observed_at,
            "runtime_observed_at": observed_at,
        },
    )[0]


def delete_current_guest(*, object_type: str, vmid: int, cluster) -> None:
    CurrentGuestInventory.objects.filter(
        cluster=_target_cluster(cluster), object_type=object_type, vmid=vmid
    ).delete()


def refresh_current_guest_from_client(
    client,
    *,
    node: str,
    object_type: str,
    vmid: int,
    cluster,
    allow_relocation: bool = False,
    delete_if_authoritatively_absent: bool = False,
) -> TargetedGuestRefresh:
    """Immediately reconcile one guest after a provider-side operation.

    A direct read is the fast path for power/config changes. Migration/clone
    callers may allow one cluster-resource lookup to discover the new node.
    Absence is acted on only after a valid authoritative resource listing.
    """
    resolved_node = node
    current: dict[str, Any] | None = None
    direct_error = ""
    if resolved_node:
        try:
            current = client.guest_current(node=resolved_node, object_type=object_type, vmid=vmid)
            if not isinstance(current, dict):
                raise ProxmoxAPIError("Proxmox returned an invalid guest status.")
        except ProxmoxAPIError as exc:
            direct_error = str(exc)

    if current is None and allow_relocation:
        try:
            resources = client.get("cluster/resources?type=vm")
        except ProxmoxAPIError as exc:
            return TargetedGuestRefresh(found=False, error=str(exc))
        if not isinstance(resources, list):
            return TargetedGuestRefresh(found=False, error="Proxmox returned an invalid guest inventory.")
        expected_type = "qemu" if object_type == ProxmoxInventory.ObjectType.VM else "lxc"
        matches = [
            item
            for item in resources
            if isinstance(item, dict)
            and str(item.get("type") or "") == expected_type
            and str(item.get("vmid") or "") == str(vmid)
        ]
        if not matches:
            if delete_if_authoritatively_absent:
                endpoint = ProxmoxEndpoint.objects.filter(url=getattr(client, "endpoint", "")).first()
                delete_current_guest(
                    object_type=object_type,
                    vmid=vmid,
                    cluster=_target_cluster(cluster, endpoint=endpoint),
                )
            return TargetedGuestRefresh(found=False, absent=True)
        if len(matches) != 1:
            return TargetedGuestRefresh(found=False, error="Guest identity was ambiguous after the operation.")
        resolved_node = str(matches[0].get("node") or "")
        if not resolved_node:
            return TargetedGuestRefresh(found=False, error="Proxmox did not return the guest node.")
        try:
            current = client.guest_current(node=resolved_node, object_type=object_type, vmid=vmid)
            if not isinstance(current, dict):
                raise ProxmoxAPIError("Proxmox returned an invalid guest status.")
        except ProxmoxAPIError as exc:
            return TargetedGuestRefresh(found=False, node=resolved_node, error=str(exc))

    if current is None:
        return TargetedGuestRefresh(found=False, node=resolved_node, error=direct_error or "Guest status unavailable.")

    endpoint = ProxmoxEndpoint.objects.filter(url=getattr(client, "endpoint", "")).first()
    target_cluster = _target_cluster(cluster, endpoint=endpoint)
    existing = CurrentGuestInventory.objects.filter(
        cluster=target_cluster, object_type=object_type, vmid=vmid
    ).first()
    config = dict(existing.config or {}) if existing else {}
    config_complete = existing.config_complete if existing else False
    config_observed_at = existing.config_observed_at if existing else None
    try:
        refreshed_config = client.guest_config(node=resolved_node, object_type=object_type, vmid=vmid)
        if not isinstance(refreshed_config, dict):
            raise ProxmoxAPIError("Proxmox returned an invalid guest configuration.")
        config = refreshed_config
        config_complete = True
        config_observed_at = timezone.now()
    except ProxmoxAPIError:
        pass

    observed_at = timezone.now()
    name_key = "name" if object_type == ProxmoxInventory.ObjectType.VM else "hostname"
    CurrentGuestInventory.objects.update_or_create(
        cluster=target_cluster,
        object_type=object_type,
        vmid=vmid,
        defaults={
            "source_endpoint": endpoint or (existing.source_endpoint if existing else None),
            "source_scan": existing.source_scan if existing else None,
            "node": resolved_node,
            "name": str(config.get(name_key) or current.get("name") or current.get("hostname") or ""),
            "status": str(current.get("status") or ""),
            "cpu_usage": _float_or_zero(current.get("cpu")),
            "memory_used_bytes": _int_or_zero(current.get("mem")),
            "memory_max_bytes": _int_or_zero(current.get("maxmem")),
            "disk_used_bytes": _int_or_zero(current.get("disk")),
            "disk_max_bytes": _int_or_zero(current.get("maxdisk")),
            "uptime_seconds": _int_or_zero(current.get("uptime")),
            "runtime_lock": str(current.get("lock") or config.get("lock") or ""),
            "runtime_observed_at": observed_at,
            "config": config,
            "config_complete": config_complete,
            "config_observed_at": config_observed_at,
            "disk_references": extract_disk_references(config),
            "observed_at": observed_at,
        },
    )
    return TargetedGuestRefresh(found=True, node=resolved_node)

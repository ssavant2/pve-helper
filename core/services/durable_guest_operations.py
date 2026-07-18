"""Resolve durable guest-operation state without endpoint or VMID guessing."""

from __future__ import annotations

from core.models import AuditEvent, ProxmoxCluster, ProxmoxEndpoint
from core.services.cluster_resolver import (
    client_for_endpoint,
    cluster_clients,
)
from core.services.refs import GuestRef, RefParseError


class DurableGuestOperationError(RuntimeError):
    pass


def guest_ref_from_audit_event(event: AuditEvent) -> GuestRef:
    details = event.details if isinstance(event.details, dict) else {}
    try:
        ref = GuestRef.parse(str(details.get("guest_ref") or ""))
    except RefParseError as exc:
        raise DurableGuestOperationError(
            "Audit event has no cluster-qualified guest target."
        ) from exc

    if event.cluster_id is not None and event.cluster.key != ref.cluster_key:
        raise DurableGuestOperationError("Audit relation and GuestRef identify different clusters.")
    if event.cluster_key_snapshot and event.cluster_key_snapshot != ref.cluster_key:
        raise DurableGuestOperationError("Audit snapshot and GuestRef identify different clusters.")
    return ref


def cluster_for_guest_ref(ref: GuestRef) -> ProxmoxCluster:
    cluster = ProxmoxCluster.objects.filter(key=ref.cluster_key).first()
    if cluster is None:
        raise DurableGuestOperationError(
            f"The Proxmox cluster '{ref.cluster_key}' no longer exists."
        )
    return cluster


def client_for_audit_event(event: AuditEvent, *, preferred_endpoint_url: str = ""):
    ref = guest_ref_from_audit_event(event)
    cluster = cluster_for_guest_ref(ref)
    preferred_endpoint_url = str(
        preferred_endpoint_url
        or (event.details or {}).get("proxmox_endpoint")
        or ""
    )
    if preferred_endpoint_url:
        endpoint = ProxmoxEndpoint.objects.filter(
            cluster=cluster,
            enabled=True,
            url=preferred_endpoint_url,
        ).first()
        if endpoint is not None:
            return client_for_endpoint(endpoint), ref, cluster
    clients = cluster_clients(cluster)
    if not clients:
        raise DurableGuestOperationError(
            f"Cluster '{cluster.key}' has no enabled Proxmox endpoint."
        )
    return clients[0], ref, cluster

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import quote

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from core.models import ConsoleSession, ProxmoxCluster
from core.services.cluster_footprint import FOOTPRINT_CONSOLE_SESSION, stamp_operational_footprint
from core.services.cluster_lifecycle_registry import (
    CODE_FORCE_RETIRED_UNRESOLVABLE,
    CODE_RETIRED_BEFORE_START,
)
from core.services.proxmox import ProxmoxAPIError, ProxmoxClient, clear_live_guest_caches
from core.services.request_metadata import client_ip

_RETIREMENT_DETAIL_LIMIT = 100
_RETIREMENT_PENDING_STATUSES = {ConsoleSession.Status.PENDING}
_RETIREMENT_ACTIVE_STATUSES = {
    ConsoleSession.Status.CONNECTING,
    ConsoleSession.Status.CONNECTED,
}
_RETIREMENT_TERMINAL_STATUSES = {
    ConsoleSession.Status.CLOSED,
    ConsoleSession.Status.FAILED,
    ConsoleSession.Status.EXPIRED,
}


@dataclass(frozen=True)
class ConsoleSessionResult:
    session: ConsoleSession
    token: str
    password: str
    console_type: str


@dataclass(frozen=True)
class ConsoleRetirementParticipant:
    pk: int
    status: str
    target_type: str
    target_vmid: int
    target_node: str


@dataclass(frozen=True)
class ConsoleRetirementPreflight:
    mode: str
    pending_count: int
    active_count: int
    unknown_count: int
    participants: tuple[ConsoleRetirementParticipant, ...]
    participant_count: int
    participants_omitted: int

    @property
    def gate_clear(self) -> bool:
        return not self.unknown_count and (self.mode == ProxmoxCluster.RetirementMode.FORCED or not self.active_count)


@dataclass(frozen=True)
class ConsoleRetirementResult:
    pending_closed: int
    active_closed: int
    sessions_sanitized: int
    participants: tuple[ConsoleRetirementParticipant, ...]
    participant_count: int
    participants_omitted: int


class ConsoleRetirementBlocked(RuntimeError):
    def __init__(self, preflight: ConsoleRetirementPreflight):
        self.preflight = preflight
        super().__init__("Console retirement is blocked by active or unclassified sessions.")


def _retirement_mode(mode: str) -> str:
    value = str(getattr(mode, "value", mode))
    if value not in {
        ProxmoxCluster.RetirementMode.VERIFIED,
        ProxmoxCluster.RetirementMode.FORCED,
    }:
        raise ValueError(f"Unsupported retirement mode: {value!r}")
    return value


def _console_retirement_preflight(
    cluster,
    *,
    mode: str,
    lock: bool,
) -> ConsoleRetirementPreflight:
    sessions = ConsoleSession.objects.filter(cluster_id=cluster.pk).exclude(status__in=_RETIREMENT_TERMINAL_STATUSES)
    if lock:
        sessions = sessions.select_for_update()
        rows = list(
            sessions.order_by("pk").values_list(
                "pk",
                "status",
                "target_type",
                "target_vmid",
                "target_node",
            )
        )
        participant_count = len(rows)
        pending_count = sum(status in _RETIREMENT_PENDING_STATUSES for _pk, status, *_target in rows)
        active_count = sum(status in _RETIREMENT_ACTIVE_STATUSES for _pk, status, *_target in rows)
    else:
        counts = sessions.aggregate(
            total=Count("pk"),
            pending=Count("pk", filter=Q(status__in=_RETIREMENT_PENDING_STATUSES)),
            active=Count("pk", filter=Q(status__in=_RETIREMENT_ACTIVE_STATUSES)),
        )
        participant_count = counts["total"]
        pending_count = counts["pending"]
        active_count = counts["active"]
        rows = list(
            sessions.order_by("pk").values_list(
                "pk",
                "status",
                "target_type",
                "target_vmid",
                "target_node",
            )[:_RETIREMENT_DETAIL_LIMIT]
        )
    participants = tuple(ConsoleRetirementParticipant(*row) for row in rows)
    unknown_count = participant_count - pending_count - active_count
    bounded = participants[:_RETIREMENT_DETAIL_LIMIT]
    return ConsoleRetirementPreflight(
        mode=mode,
        pending_count=pending_count,
        active_count=active_count,
        unknown_count=unknown_count,
        participants=bounded,
        participant_count=participant_count,
        participants_omitted=participant_count - len(bounded),
    )


def cluster_retirement_console_preflight(
    cluster,
    *,
    mode: str,
) -> ConsoleRetirementPreflight:
    """Classify console sessions without contacting Proxmox."""
    return _console_retirement_preflight(cluster, mode=_retirement_mode(mode), lock=False)


@transaction.atomic
def finalize_cluster_retirement_consoles(
    cluster,
    *,
    mode: str,
    retired_at=None,
) -> ConsoleRetirementResult:
    """Close eligible sessions and remove every retained provider secret."""
    mode = _retirement_mode(mode)
    retired_at = retired_at or timezone.now()
    sessions = ConsoleSession.objects.filter(cluster_id=cluster.pk)
    list(sessions.select_for_update().order_by("pk").values_list("pk", flat=True))
    preflight = _console_retirement_preflight(cluster, mode=mode, lock=True)
    if not preflight.gate_clear:
        raise ConsoleRetirementBlocked(preflight)

    sensitive = ~Q(proxmox_ticket="") | ~Q(proxmox_password="") | ~Q(proxmox_endpoint="")
    sessions_sanitized = sessions.filter(sensitive).count()
    pending_closed = sessions.filter(status__in=_RETIREMENT_PENDING_STATUSES).update(
        status=ConsoleSession.Status.CLOSED,
        closed_at=retired_at,
        close_reason=CODE_RETIRED_BEFORE_START,
        proxmox_ticket="",
        proxmox_password="",
        proxmox_endpoint="",
        updated_at=retired_at,
    )
    active_closed = 0
    if mode == ProxmoxCluster.RetirementMode.FORCED:
        active_closed = sessions.filter(status__in=_RETIREMENT_ACTIVE_STATUSES).update(
            status=ConsoleSession.Status.CLOSED,
            closed_at=retired_at,
            close_reason=CODE_FORCE_RETIRED_UNRESOLVABLE,
            proxmox_ticket="",
            proxmox_password="",
            proxmox_endpoint="",
            updated_at=retired_at,
        )
    sessions.filter(sensitive).update(
        proxmox_ticket="",
        proxmox_password="",
        proxmox_endpoint="",
        updated_at=retired_at,
    )
    return ConsoleRetirementResult(
        pending_closed=pending_closed,
        active_closed=active_closed,
        sessions_sanitized=sessions_sanitized,
        participants=preflight.participants,
        participant_count=preflight.participant_count,
        participants_omitted=preflight.participants_omitted,
    )


def console_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_guest_console_session(*, request, detail) -> ConsoleSessionResult:
    if not settings.CONSOLE_ENABLED:
        raise ProxmoxAPIError("Console access is disabled.")
    if detail.object_type not in {ConsoleSession.TargetType.VM, ConsoleSession.TargetType.CT}:
        raise ProxmoxAPIError("Integrated console is available for VMs and containers only.")
    if not detail.node:
        raise ProxmoxAPIError("The guest's node could not be resolved.")
    if detail.status != "running":
        raise ProxmoxAPIError("The guest must be running before a console can be opened.")

    proxmox_kind = "qemu" if detail.object_type == ConsoleSession.TargetType.VM else "lxc"
    console_type = "novnc" if detail.object_type == ConsoleSession.TargetType.VM else "xterm"
    proxy_endpoint = "vncproxy" if console_type == "novnc" else "termproxy"
    response: dict | None = None
    selected_client: ProxmoxClient | None = None
    last_error = "No Proxmox endpoint could create a console session."
    # A console must attach to the guest in its own cluster: a same-VMID guest on a
    # same-named node elsewhere would hand the operator a shell on the wrong
    # machine. The cluster is pinned onto the session here; the gateway resolves
    # that cluster's credential and WSS trust at connect time.
    from core.services.cluster_resolver import (
        ClusterResolutionError,
        cluster_clients,
    )

    cluster = getattr(detail, "cluster", None)
    guest_ref = getattr(detail, "guest_ref", None)
    try:
        if cluster is None or guest_ref is None or cluster.key != guest_ref.cluster_key:
            raise ClusterResolutionError("Console target is missing cluster-qualified identity.")
        candidates = cluster_clients(cluster)
    except ClusterResolutionError as exc:
        candidates = []
        last_error = str(exc)

    for client in candidates:
        try:
            data = client.post(
                f"nodes/{quote(detail.node, safe='')}/{proxmox_kind}/{detail.vmid}/{proxy_endpoint}",
                data={"websocket": 1} if console_type == "novnc" else None,
            )
            if not isinstance(data, dict):
                raise ProxmoxAPIError("Unexpected vncproxy response.")
            response = data
            selected_client = client
            break
        except ProxmoxAPIError as exc:
            last_error = str(exc)

    if response is None or selected_client is None:
        raise ProxmoxAPIError(last_error)

    port = str(response.get("port") or "")
    ticket = str(response.get("ticket") or response.get("vncticket") or "")
    password = str(response.get("password") or "")
    # The xterm handshake user falls back to the cluster credential's own token id,
    # not a global setting, so a multi-cluster deployment names the right identity.
    fallback_user = ""
    credential = getattr(selected_client, "_credential", None)
    if credential is not None and credential.token_id:
        fallback_user = credential.token_id.split("!", 1)[0]
    proxmox_user = str(response.get("user") or fallback_user)
    if not port or not ticket:
        raise ProxmoxAPIError("Proxmox did not return a usable console ticket.")

    token = secrets.token_urlsafe(32)
    user = getattr(request, "user", None)
    authenticated = user is not None and getattr(user, "is_authenticated", False)
    source_ip = client_ip(request)
    expires_at = timezone.now() + timezone.timedelta(seconds=max(settings.CONSOLE_SESSION_TTL_SECONDS, 5))
    session = ConsoleSession.objects.create(
        token_hash=console_token_hash(token),
        cluster=cluster,
        target_type=detail.object_type,
        target_vmid=detail.vmid,
        target_node=detail.node,
        target_name_snapshot=detail.name,
        created_by=user if authenticated else None,
        username=user.get_username() if authenticated else "",
        source_ip=source_ip,
        expires_at=expires_at,
        proxmox_endpoint=selected_client.endpoint,
        proxmox_node=detail.node,
        proxmox_upid=str(response.get("upid") or ""),
        proxmox_port=port,
        proxmox_ticket=ticket,
        proxmox_password=password,
        details={
            "cert_present": bool(response.get("cert")),
            "console_type": console_type,
            "proxmox_user": proxmox_user,
        },
    )
    # Opening a console is operational footprint even after the 24h session-row
    # retention deletes this row; the marker is the durable memory of it.
    stamp_operational_footprint(cluster, reason=FOOTPRINT_CONSOLE_SESSION)
    clear_live_guest_caches(cluster=cluster)
    return ConsoleSessionResult(session=session, token=token, password=password, console_type=console_type)

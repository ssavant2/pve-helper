from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import quote

from django.conf import settings
from django.utils import timezone

from core.models import ConsoleSession
from core.services.proxmox import ProxmoxAPIError, ProxmoxClient, clear_live_guest_caches
from core.services.request_metadata import client_ip


@dataclass(frozen=True)
class ConsoleSessionResult:
    session: ConsoleSession
    token: str
    password: str
    console_type: str


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
        require_sole_enabled_cluster_for_legacy_caller,
    )

    cluster = None
    try:
        cluster = require_sole_enabled_cluster_for_legacy_caller()
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
    elif settings.PVE_API_TOKEN_ID:
        fallback_user = settings.PVE_API_TOKEN_ID.split("!", 1)[0]
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
        details={"cert_present": bool(response.get("cert")), "console_type": console_type, "proxmox_user": proxmox_user},
    )
    clear_live_guest_caches()
    return ConsoleSessionResult(session=session, token=token, password=password, console_type=console_type)

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import quote

from django.conf import settings
from django.utils import timezone

from core.models import ConsoleSession
from core.services.proxmox import ProxmoxAPIError, ProxmoxClient, clear_live_guest_caches, configured_clients


@dataclass(frozen=True)
class ConsoleSessionResult:
    session: ConsoleSession
    token: str
    password: str


def console_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_vm_console_session(*, request, detail) -> ConsoleSessionResult:
    if not settings.CONSOLE_ENABLED:
        raise ProxmoxAPIError("Console access is disabled.")
    if detail.object_type != ConsoleSession.TargetType.VM:
        raise ProxmoxAPIError("Integrated console is currently available for VMs only.")
    if not detail.node:
        raise ProxmoxAPIError("The VM's node could not be resolved.")
    if detail.status != "running":
        raise ProxmoxAPIError("The VM must be running before a console can be opened.")

    response: dict | None = None
    selected_client: ProxmoxClient | None = None
    last_error = "No Proxmox endpoint could create a console session."
    for client in configured_clients():
        try:
            data = client.post(
                f"nodes/{quote(detail.node, safe='')}/qemu/{detail.vmid}/vncproxy",
                data={"websocket": 1},
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
    if not port or not ticket:
        raise ProxmoxAPIError("Proxmox did not return a usable console ticket.")

    token = secrets.token_urlsafe(32)
    user = getattr(request, "user", None)
    authenticated = user is not None and getattr(user, "is_authenticated", False)
    source_ip = _client_ip(request)
    expires_at = timezone.now() + timezone.timedelta(seconds=max(settings.CONSOLE_SESSION_TTL_SECONDS, 5))
    session = ConsoleSession.objects.create(
        token_hash=console_token_hash(token),
        target_type=ConsoleSession.TargetType.VM,
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
        details={"cert_present": bool(response.get("cert"))},
    )
    clear_live_guest_caches()
    return ConsoleSessionResult(session=session, token=token, password=password)


def _client_ip(request) -> str | None:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None

from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import quote, urlparse

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pve_helper.settings")

import django  # noqa: E402

django.setup()

from asgiref.sync import sync_to_async  # noqa: E402
from django.conf import settings  # noqa: E402
from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route, WebSocketRoute  # noqa: E402
from starlette.websockets import WebSocket  # noqa: E402
from websockets.asyncio.client import connect as ws_connect  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402

from core.models import ConsoleSession  # noqa: E402
from core.services.audit_events import record_audit_event  # noqa: E402
from core.services.console_sessions import console_token_hash  # noqa: E402


async def health_live(_request):
    return JSONResponse({"status": "ok", "service": "pve-helper-console"})


async def console_ws(websocket: WebSocket):
    token = websocket.path_params.get("token", "")
    session = await _consume_session(token)
    if session is None:
        await websocket.close(code=1008)
        return

    marked_closed = False
    try:
        upstream_url = _upstream_url(session)
        headers = await _resolve_cluster_credential_header(session)
        ssl_context = await _resolve_cluster_ssl_context(session, upstream_url)
        console_type = str((session.details or {}).get("console_type") or "novnc")
        connect_kwargs = {}
        if console_type == "xterm":
            connect_kwargs["subprotocols"] = ["binary"]
        async with ws_connect(
            upstream_url,
            additional_headers=headers,
            ssl=ssl_context,
            open_timeout=max(settings.CONSOLE_CONNECT_TIMEOUT_SECONDS, 1),
            ping_interval=30,
            ping_timeout=30,
            max_size=None,
            **connect_kwargs,
        ) as upstream:
            if console_type == "xterm":
                initial_output = await _authenticate_xterm_session(session, upstream)
                await websocket.accept()
                if initial_output:
                    await websocket.send_bytes(initial_output)
                await _mark_session(session.id, ConsoleSession.Status.CONNECTED, connected_at=timezone.now())
                await _relay_xterm(websocket, upstream)
            else:
                await websocket.accept()
                await _mark_session(session.id, ConsoleSession.Status.CONNECTED, connected_at=timezone.now())
                await _relay(websocket, upstream)
            await _mark_session(
                session.id, ConsoleSession.Status.CLOSED, closed_at=timezone.now(), close_reason="closed"
            )
            await _audit_session(session.id, "guest.console.closed", "success")
            marked_closed = True
    except Exception as exc:
        if marked_closed:
            # The relay already ended cleanly; this is teardown noise from the
            # upstream closing without a close frame. Keep the CLOSED outcome.
            return
        await _mark_session(
            session.id,
            ConsoleSession.Status.FAILED,
            closed_at=timezone.now(),
            close_reason=exc.__class__.__name__,
            error=str(exc),
        )
        await _audit_session(session.id, "guest.console.failed", "failed", error=str(exc))
        try:
            if websocket.client_state.name != "DISCONNECTED":
                await websocket.close(code=1011)
        except RuntimeError:
            pass


async def _relay(websocket: WebSocket, upstream):
    async def browser_to_upstream():
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                await upstream.close()
                break
            if message["type"] != "websocket.receive":
                continue
            if message.get("bytes") is not None:
                await upstream.send(message["bytes"])
            elif message.get("text") is not None:
                await upstream.send(message["text"])

    async def upstream_to_browser():
        async for message in upstream:
            if isinstance(message, bytes):
                await websocket.send_bytes(message)
            else:
                await websocket.send_text(message)

    tasks = {asyncio.create_task(browser_to_upstream()), asyncio.create_task(upstream_to_browser())}
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        try:
            task.result()
        except ConnectionClosed:
            # Either side closing the console (incl. no close frame from the
            # upstream) is a normal end of session, not a failure.
            pass


async def _authenticate_xterm_session(session: ConsoleSession, upstream) -> bytes:
    user = str((session.details or {}).get("proxmox_user") or "")
    ticket = session.proxmox_ticket
    if not user or not ticket:
        raise RuntimeError("Missing xterm console credentials.")
    await upstream.send(f"{user}:{ticket}\n")
    answer = await asyncio.wait_for(upstream.recv(), timeout=max(settings.CONSOLE_CONNECT_TIMEOUT_SECONDS, 1))
    if isinstance(answer, str):
        answer_bytes = answer.encode("utf-8", errors="replace")
    else:
        answer_bytes = bytes(answer)
    if not answer_bytes.startswith(b"OK"):
        raise RuntimeError("xterm console authentication failed.")
    return answer_bytes[2:]


async def _relay_xterm(websocket: WebSocket, upstream):
    async def browser_to_upstream():
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                await upstream.close()
                break
            if message["type"] != "websocket.receive":
                continue
            payload = message.get("text")
            if payload is None and message.get("bytes") is not None:
                payload = message["bytes"].decode("utf-8", errors="replace")
            if not payload:
                continue
            try:
                decoded = json.loads(payload)
            except json.JSONDecodeError:
                data = payload
                await upstream.send(f"0:{len(data.encode('utf-8'))}:{data}")
                continue
            if decoded.get("type") == "resize":
                cols = max(1, int(decoded.get("cols") or 80))
                rows = max(1, int(decoded.get("rows") or 24))
                await upstream.send(f"1:{cols}:{rows}:")
            elif decoded.get("type") == "data":
                data = str(decoded.get("data") or "")
                await upstream.send(f"0:{len(data.encode('utf-8'))}:{data}")
            elif decoded.get("type") == "ping":
                await upstream.send("2")

    async def upstream_to_browser():
        async for message in upstream:
            if isinstance(message, bytes):
                await websocket.send_bytes(message)
            else:
                await websocket.send_text(message)

    async def ping_upstream():
        while True:
            await asyncio.sleep(30)
            await upstream.send("2")

    tasks = {
        asyncio.create_task(browser_to_upstream()),
        asyncio.create_task(upstream_to_browser()),
        asyncio.create_task(ping_upstream()),
    }
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        try:
            task.result()
        except ConnectionClosed:
            # Either side closing the console (incl. no close frame from the
            # upstream) is a normal end of session, not a failure.
            pass


@sync_to_async(thread_sensitive=True)
def _consume_session(token: str) -> ConsoleSession | None:
    token_hash = console_token_hash(token)
    now = timezone.now()
    with transaction.atomic():
        session = ConsoleSession.objects.select_for_update().filter(token_hash=token_hash).first()
        if session is None:
            return None
        if session.consumed_at or session.status != ConsoleSession.Status.PENDING:
            return None
        if session.expires_at < now:
            ConsoleSession.objects.filter(pk=session.pk).update(
                status=ConsoleSession.Status.EXPIRED,
                closed_at=now,
                close_reason="expired",
                proxmox_ticket="",
                proxmox_password="",
                updated_at=now,
            )
            return None
        # Keep credentials only in this in-memory object for the upstream
        # handshake. Once the one-time token is consumed, no database row needs
        # to retain them.
        session.consumed_at = now
        session.status = ConsoleSession.Status.CONNECTING
        ConsoleSession.objects.filter(pk=session.pk).update(
            consumed_at=now,
            status=ConsoleSession.Status.CONNECTING,
            proxmox_ticket="",
            proxmox_password="",
            updated_at=now,
        )
        return session


@sync_to_async(thread_sensitive=True)
def _mark_session(session_id: int, status: str, **updates) -> None:
    fields = {"status": status, **updates}
    ConsoleSession.objects.filter(pk=session_id).update(**fields)


@sync_to_async(thread_sensitive=True)
def _audit_session(session_id: int, action: str, outcome: str, *, error: str = "") -> None:
    session = ConsoleSession.objects.filter(pk=session_id).first()
    if session is None:
        return
    details = {
        "node": session.target_node,
        "vmid": session.target_vmid,
        "target_type": session.target_type,
        "name": session.target_name_snapshot,
        "console_session_id": session.id,
    }
    if error:
        details["error"] = error
    guest_ref = session.guest_ref()
    record_audit_event(
        user=session.created_by,
        username=session.username,
        source_ip=session.source_ip,
        action=action,
        object_type="guest",
        object_id=f"{session.target_type}:{session.target_vmid}",
        outcome=outcome,
        details=details,
        cluster=session.cluster,
        guest_ref=guest_ref,
    )


def _upstream_url(session: ConsoleSession) -> str:
    endpoint = session.proxmox_endpoint.rstrip("/")
    parsed = urlparse(endpoint)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    proxmox_kind = "qemu" if session.target_type == ConsoleSession.TargetType.VM else "lxc"
    path = (
        f"/api2/json/nodes/{quote(session.proxmox_node, safe='')}/{proxmox_kind}/{session.target_vmid}/vncwebsocket"
        f"?port={quote(session.proxmox_port, safe='')}&vncticket={quote(session.proxmox_ticket, safe='')}"
    )
    return f"{scheme}://{parsed.netloc}{path}"


class _ConsoleAuthError(RuntimeError):
    """The session's cluster could not supply a usable credential or trust."""


@sync_to_async
def _resolve_cluster_credential_header(session: ConsoleSession) -> dict[str, str]:
    """The Authorization header for this session's cluster, at connect time.

    Resolved now rather than stored, so a credential rotation during the short
    session window takes effect and no API token is ever copied into a
    browser-visible session payload. Fails closed if the cluster has none usable.

    Contract version 1 forbids ambient credentials. A session without cluster
    identity is invalid and must fail before any upstream connection is opened.
    """
    if session.cluster_id is None:
        raise _ConsoleAuthError("Console session has no cluster identity.")

    from core.services.cluster_credentials import ClusterCredentialError, resolve_credential

    try:
        credential = resolve_credential(session.cluster)
    except ClusterCredentialError as exc:
        raise _ConsoleAuthError(str(exc)) from exc
    return {"Authorization": credential.authorization_header()}


@sync_to_async
def _resolve_cluster_ssl_context(session: ConsoleSession, url: str):
    """The WSS TLS context for this session's cluster.

    Built from the cluster's own transport-trust profile so one cluster's WSS trust
    is never another's or an ambient global fallback.
    """
    if not url.startswith("wss://"):
        return None
    if session.cluster_id is None:
        raise _ConsoleAuthError("Console session has no cluster identity.")

    from core.services.cluster_trust import (
        TransportTrustError,
        resolve_trust_profile,
        ssl_context_for,
    )

    try:
        return ssl_context_for(resolve_trust_profile(session.cluster))
    except TransportTrustError as exc:
        raise _ConsoleAuthError(str(exc)) from exc


app = Starlette(
    routes=[
        Route("/healthz/live", health_live),
        WebSocketRoute("/console/ws/{token}/", console_ws),
    ]
)

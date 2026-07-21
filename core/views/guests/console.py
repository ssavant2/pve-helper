"""Guest console (extracted from _core)."""

from django.templatetags.static import static

from core.services.console_sessions import create_guest_console_session
from core.services.public_errors import public_exception_message

from ..common import (
    GUEST_OBJECT_TYPES,
    Http404,
    JsonResponse,
    ProxmoxAPIError,
    ProxmoxInventory,
    app_login_required,
    render,
    require_POST,
    reverse,
    settings,
)
from .operation_lifecycle import _audit_guest
from .read_model_support import _guest_tab_context, _resolve_guest_detail


@app_login_required
def guest_console(request, cluster_key: str, object_type: str, vmid: int):
    if object_type not in GUEST_OBJECT_TYPES:
        raise Http404("Unknown guest type")
    detail = _resolve_guest_detail(object_type, vmid, cluster_key=cluster_key)
    if not detail.found:
        raise Http404("Guest not found")

    context = _guest_tab_context(detail, "console")
    context.update(
        {
            "console_enabled": settings.CONSOLE_ENABLED,
            "console_supported": detail.object_type in {ProxmoxInventory.ObjectType.VM, ProxmoxInventory.ObjectType.CT},
            "console_session_url": reverse("core:guest_console_session", args=[cluster_key, object_type, vmid]),
            # Locally vendored (no CDN). Pinned versions + update steps:
            # static/vendor/README.md.
            "console_novnc_url": static("vendor/novnc/rfb.esm.js"),
            "console_xterm_js_url": static("vendor/xterm/xterm.min.js"),
            "console_xterm_fit_url": static("vendor/xterm/addon-fit.min.js"),
            "console_xterm_css_url": static("vendor/xterm/xterm.min.css"),
            "console_require_running": detail.status != "running",
        }
    )
    return render(request, "core/guest_console.html", context)


@require_POST
@app_login_required
def guest_console_session(request, cluster_key: str, object_type: str, vmid: int):
    if object_type not in GUEST_OBJECT_TYPES:
        return JsonResponse({"error": "Unknown guest type."}, status=404)
    detail = _resolve_guest_detail(object_type, vmid, cluster_key=cluster_key)
    if not detail.found:
        return JsonResponse({"error": "Guest not found."}, status=404)

    try:
        result = create_guest_console_session(request=request, detail=detail)
    except ProxmoxAPIError as exc:
        error = public_exception_message(
            exc,
            operation="guest_console_session",
            fallback="The console session could not be opened through Proxmox.",
        )
        _audit_guest(request, detail, "guest.console.failed", {"error": error}, outcome="failed")
        return JsonResponse({"error": error}, status=400)

    _audit_guest(
        request,
        detail,
        "guest.console.opened",
        {"console_session_id": result.session.id, "proxmox_task_upid": result.session.proxmox_upid},
    )
    return JsonResponse(
        {
            "token": result.token,
            "password": result.password,
            "console_type": result.console_type,
            "websocket_url": f"/console/ws/{result.token}/",
            "expires_at": result.session.expires_at.isoformat(),
        }
    )

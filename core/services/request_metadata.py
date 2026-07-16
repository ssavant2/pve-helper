from __future__ import annotations

import ipaddress


def client_ip(request) -> str | None:
    """Return the valid address supplied by the trusted app nginx.

    The application must never consume the client-controlled leftmost value of
    ``X-Forwarded-For``. The nginx sidecar overwrites ``X-Real-IP`` after its
    optional real-IP processing; direct requests fall back to ``REMOTE_ADDR``.
    """
    if request is None:
        return None
    return _valid_ip(request.META.get("HTTP_X_REAL_IP")) or _valid_ip(request.META.get("REMOTE_ADDR"))


def _valid_ip(value) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except ValueError:
        return None

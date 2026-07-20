from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from django.conf import settings


@dataclass(frozen=True)
class EndpointDefinition:
    name: str
    url: str


def endpoint_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or urlparse(f"https://{url}").hostname or url
    return host.split(".", 1)[0]


# Proxmox serves its API on 8006; an omitted port means the same endpoint.
_DEFAULT_PORTS = {"https": 8006, "http": 80}


def normalize_endpoint_url(url: str) -> str:
    """A canonical form for comparing endpoint URLs across clusters.

    An endpoint is a transport, and the same transport must not be claimed by two
    clusters — otherwise one cluster's inventory arrives under another's identity.
    Case, a trailing slash and an explicitly written default port are all the same
    host, so they must not defeat the check.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "//" in raw else f"https://{raw}")
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    port = parsed.port or _DEFAULT_PORTS.get(scheme)
    path = parsed.path.rstrip("/")
    return f"{scheme}://{host}:{port}{path}" if port else f"{scheme}://{host}{path}"


def configured_endpoint_definitions() -> list[EndpointDefinition]:
    definitions: list[EndpointDefinition] = []
    for endpoint in settings.PVE_ENDPOINTS:
        endpoint = endpoint.rstrip("/")
        if not endpoint:
            continue
        definitions.append(EndpointDefinition(name=endpoint_name_from_url(endpoint), url=endpoint))
    return definitions

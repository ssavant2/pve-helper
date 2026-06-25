from __future__ import annotations

from dataclasses import dataclass

import httpx
from django.conf import settings


@dataclass(frozen=True)
class EndpointHealth:
    endpoint: str
    ok: bool
    status: str
    details: dict


class ProxmoxClient:
    """Small Proxmox API client shell.

    Phase 0 only exposes a health probe. Inventory discovery will be added in
    the Proxmox read-model phase, keeping API parsing outside Django models.
    """

    def __init__(self, endpoint: str):
        self.endpoint = endpoint.rstrip("/")

    def health(self) -> EndpointHealth:
        verify: bool | str = settings.PVE_VERIFY_TLS
        if settings.PVE_CA_BUNDLE:
            verify = settings.PVE_CA_BUNDLE

        try:
            response = httpx.get(
                f"{self.endpoint}/api2/json/version",
                timeout=5,
                verify=verify,
            )
            response.raise_for_status()
        except Exception as exc:
            return EndpointHealth(
                endpoint=self.endpoint,
                ok=False,
                status="error",
                details={"error": exc.__class__.__name__},
            )

        return EndpointHealth(
            endpoint=self.endpoint,
            ok=True,
            status="ok",
            details=response.json(),
        )


def configured_clients() -> list[ProxmoxClient]:
    return [ProxmoxClient(endpoint) for endpoint in settings.PVE_ENDPOINTS]

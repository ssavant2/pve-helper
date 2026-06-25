from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from django.conf import settings

from .classification import extract_disk_references


@dataclass(frozen=True)
class EndpointHealth:
    endpoint: str
    ok: bool
    status: str
    details: dict


@dataclass(frozen=True)
class ProxmoxObject:
    node: str
    object_type: str
    vmid: int | None
    name: str
    status: str
    config: dict[str, Any]
    disk_references: list[str]


@dataclass(frozen=True)
class InventoryResult:
    node: str
    ok: bool
    objects: list[ProxmoxObject]
    errors: list[dict[str, Any]]


class ProxmoxAPIError(Exception):
    pass


class ProxmoxClient:
    """Small read-only Proxmox API client."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint.rstrip("/")

    def health(self) -> EndpointHealth:
        try:
            details = self.get("version")
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
            details=details,
        )

    def discover_node_name(self, fallback: str) -> str:
        try:
            nodes = self.get("nodes")
        except ProxmoxAPIError:
            return fallback

        if not isinstance(nodes, list) or not nodes:
            return fallback

        node_names = [str(node.get("node", "")) for node in nodes if node.get("node")]
        if fallback in node_names:
            return fallback
        if len(node_names) == 1:
            return node_names[0]
        return fallback

    def inventory(self, node: str) -> InventoryResult:
        objects: list[ProxmoxObject] = []
        errors: list[dict[str, Any]] = []

        qemu_vms = self._get_list(f"nodes/{quote(node)}/qemu", errors, "qemu.list")
        for vm in qemu_vms:
            vmid = self._int_or_none(vm.get("vmid"))
            if vmid is None:
                continue
            config = self._get_config(f"nodes/{quote(node)}/qemu/{vmid}/config", errors, "qemu.config", vmid)
            if config is None:
                continue
            objects.append(
                ProxmoxObject(
                    node=node,
                    object_type="vm",
                    vmid=vmid,
                    name=str(vm.get("name") or config.get("name") or ""),
                    status=str(vm.get("status") or ""),
                    config=config,
                    disk_references=extract_disk_references(config),
                )
            )

        containers = self._get_list(f"nodes/{quote(node)}/lxc", errors, "lxc.list")
        for container in containers:
            vmid = self._int_or_none(container.get("vmid"))
            if vmid is None:
                continue
            config = self._get_config(f"nodes/{quote(node)}/lxc/{vmid}/config", errors, "lxc.config", vmid)
            if config is None:
                continue
            objects.append(
                ProxmoxObject(
                    node=node,
                    object_type="ct",
                    vmid=vmid,
                    name=str(container.get("name") or config.get("hostname") or ""),
                    status=str(container.get("status") or ""),
                    config=config,
                    disk_references=extract_disk_references(config),
                )
            )

        storages = self._get_list(f"nodes/{quote(node)}/storage", errors, "storage.list")
        for storage in storages:
            storage_id = str(storage.get("storage") or "")
            if not storage_id:
                continue
            objects.append(
                ProxmoxObject(
                    node=node,
                    object_type="storage",
                    vmid=None,
                    name=storage_id,
                    status=str(storage.get("active") or ""),
                    config=storage,
                    disk_references=[],
                )
            )

        return InventoryResult(node=node, ok=not errors, objects=objects, errors=errors)

    def get(self, path: str) -> Any:
        verify: bool | str = settings.PVE_VERIFY_TLS
        if settings.PVE_CA_BUNDLE:
            verify = settings.PVE_CA_BUNDLE

        headers = {}
        token_id = settings.PVE_API_TOKEN_ID
        token_secret = settings.PVE_API_TOKEN_SECRET
        if token_id and token_secret:
            headers["Authorization"] = f"PVEAPIToken={token_id}={token_secret}"

        try:
            response = httpx.get(
                f"{self.endpoint}/api2/json/{path.lstrip('/')}",
                headers=headers,
                timeout=15,
                verify=verify,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProxmoxAPIError(f"{exc.response.status_code} from {path}") from exc
        except httpx.HTTPError as exc:
            raise ProxmoxAPIError(f"{exc.__class__.__name__} from {path}") from exc

        payload = response.json()
        return payload.get("data")

    def _get_list(self, path: str, errors: list[dict[str, Any]], action: str) -> list[dict[str, Any]]:
        try:
            data = self.get(path)
        except ProxmoxAPIError as exc:
            errors.append({"action": action, "path": path, "error": str(exc)})
            return []
        return data if isinstance(data, list) else []

    def _get_config(
        self,
        path: str,
        errors: list[dict[str, Any]],
        action: str,
        vmid: int,
    ) -> dict[str, Any] | None:
        try:
            data = self.get(path)
        except ProxmoxAPIError as exc:
            errors.append({"action": action, "path": path, "vmid": vmid, "error": str(exc)})
            return None
        return data if isinstance(data, dict) else {}

    def _int_or_none(self, value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


def configured_clients() -> list[ProxmoxClient]:
    return [ProxmoxClient(endpoint) for endpoint in settings.PVE_ENDPOINTS]

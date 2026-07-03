from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from django.conf import settings
from django.core.cache import cache

from .classification import extract_disk_references


LIVE_GUEST_STATUS_CACHE_KEY = "pve-helper:live-guest-status:v1"
LIVE_GUEST_INVENTORY_CACHE_KEY = "pve-helper:live-guest-inventory:v1"
LIVE_GUEST_STATUS_CACHE_SECONDS = 1
LIVE_GUEST_INVENTORY_CACHE_SECONDS = 30


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
class ProxmoxGuestSummary:
    node: str
    object_type: str
    vmid: int
    name: str
    status: str
    cpu: float = 0.0
    mem: int = 0
    maxmem: int = 0
    disk: int = 0
    maxdisk: int = 0
    uptime: int = 0


@dataclass(frozen=True)
class InventoryResult:
    node: str
    ok: bool
    objects: list[ProxmoxObject]
    errors: list[dict[str, Any]]


@dataclass(frozen=True)
class ProxmoxTaskResult:
    node: str
    upid: str
    status: str
    exitstatus: str
    raw: dict[str, Any]

    @property
    def success(self) -> bool:
        return self.status == "stopped" and self.exitstatus == "OK"

    @property
    def ok(self) -> bool:
        return self.success


class ProxmoxAPIError(Exception):
    pass


class ProxmoxTaskTimeout(ProxmoxAPIError):
    pass


class ProxmoxClient:
    """Small Proxmox API client."""

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
        node_names = self.node_names(fallback=fallback)
        if not node_names:
            return fallback

        if fallback in node_names:
            return fallback
        if len(node_names) == 1:
            return node_names[0]
        return fallback

    def node_names(self, *, fallback: str = "") -> list[str]:
        try:
            nodes = self.get("nodes")
        except ProxmoxAPIError:
            return [fallback] if fallback else []

        if not isinstance(nodes, list):
            return [fallback] if fallback else []

        names = [str(node.get("node", "")) for node in nodes if node.get("node")]
        if names:
            return names
        return [fallback] if fallback else []

    def inventory(self, node: str) -> InventoryResult:
        objects: list[ProxmoxObject] = []
        errors: list[dict[str, Any]] = []
        storage_configs = self._storage_config_map()

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
            config = {
                **storage_configs.get(storage_id, {}),
                "node_status": storage,
            }
            for key, value in storage.items():
                config.setdefault(key, value)
            objects.append(
                ProxmoxObject(
                    node=node,
                    object_type="storage",
                    vmid=None,
                    name=storage_id,
                    status=str(storage.get("active") or ""),
                    config=config,
                    disk_references=[],
                )
            )

        return InventoryResult(node=node, ok=not errors, objects=objects, errors=errors)

    def guest_status(self, *, node: str, object_type: str, vmid: int) -> str:
        data = self.guest_current(node=node, object_type=object_type, vmid=vmid)
        return str(data.get("status") or "")

    def guest_current(self, *, node: str, object_type: str, vmid: int) -> dict[str, Any]:
        guest_kind = self._guest_kind(object_type)
        data = self.get(f"nodes/{quote(node, safe='')}/{guest_kind}/{vmid}/status/current")
        if not isinstance(data, dict):
            raise ProxmoxAPIError(f"Unexpected guest status response for {object_type} {vmid}")
        return data

    def guest_config(self, *, node: str, object_type: str, vmid: int) -> dict[str, Any]:
        guest_kind = self._guest_kind(object_type)
        data = self.get(f"nodes/{quote(node, safe='')}/{guest_kind}/{vmid}/config")
        if not isinstance(data, dict):
            raise ProxmoxAPIError(f"Unexpected guest config response for {object_type} {vmid}")
        return data

    def power_action(
        self,
        *,
        node: str,
        object_type: str,
        vmid: int,
        action: str,
        parameters: dict[str, Any] | None = None,
    ) -> str:
        if not settings.SCHEDULED_ACTIONS_ENABLED:
            raise ProxmoxAPIError("Scheduled Proxmox actions are disabled.")

        guest_kind = self._guest_kind(object_type)

        if action not in {"start", "shutdown", "stop", "reboot", "reset"}:
            raise ProxmoxAPIError(f"Unsupported power action: {action}")

        data = self.post(
            f"nodes/{quote(node, safe='')}/{guest_kind}/{vmid}/status/{action}",
            data=parameters or {},
        )
        if not isinstance(data, str) or not data:
            raise ProxmoxAPIError(f"Unexpected task response for {object_type} {vmid} {action}")
        return data

    def task_status(self, *, node: str, upid: str) -> dict[str, Any]:
        data = self.get(f"nodes/{quote(node, safe='')}/tasks/{quote(upid, safe='')}/status")
        if not isinstance(data, dict):
            raise ProxmoxAPIError(f"Unexpected task status response for {upid}")
        return data

    def wait_for_task(
        self,
        *,
        node: str,
        upid: str,
        timeout_seconds: int | None = None,
        poll_interval_seconds: float | None = None,
        sleep_func=time.sleep,
        monotonic_func=time.monotonic,
    ) -> ProxmoxTaskResult:
        timeout = timeout_seconds
        if timeout is None:
            timeout = settings.SCHEDULED_ACTION_TIMEOUT_SECONDS
        poll_interval = poll_interval_seconds
        if poll_interval is None:
            poll_interval = settings.SCHEDULED_ACTION_POLL_INTERVAL_SECONDS
        poll_interval = max(float(poll_interval), 0.1)

        deadline = monotonic_func() + max(timeout, 0)
        last_status: dict[str, Any] = {}

        while True:
            last_status = self.task_status(node=node, upid=upid)
            status = str(last_status.get("status") or "")
            exitstatus = str(last_status.get("exitstatus") or "")
            if status == "stopped" or exitstatus:
                return ProxmoxTaskResult(
                    node=node,
                    upid=upid,
                    status=status,
                    exitstatus=exitstatus,
                    raw=last_status,
                )

            now = monotonic_func()
            if now >= deadline:
                raise ProxmoxTaskTimeout(f"Timed out waiting for Proxmox task {upid}")

            sleep_func(min(poll_interval, max(deadline - now, 0.1)))

    def get(self, path: str, *, timeout: float | None = None) -> Any:
        return self._request("GET", path, timeout=timeout)

    def post(self, path: str, *, data: dict[str, Any] | None = None) -> Any:
        return self._request("POST", path, data=data or {})

    def put(self, path: str, *, data: dict[str, Any] | None = None) -> Any:
        return self._request("PUT", path, data=data or {})

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path)

    def set_guest_config(
        self,
        *,
        node: str,
        object_type: str,
        vmid: int,
        updates: dict[str, Any],
        delete: list[str] | None = None,
        digest: str | None = None,
    ) -> Any:
        if not settings.VM_WRITE_ENABLED:
            raise ProxmoxAPIError("VM/CT config writes are disabled.")
        guest_kind = self._guest_kind(object_type)
        data: dict[str, Any] = dict(updates)
        if delete:
            data["delete"] = ",".join(delete)
        if digest:
            data["digest"] = digest
        if not data:
            return None
        return self.put(f"nodes/{quote(node, safe='')}/{guest_kind}/{vmid}/config", data=data)

    def _guest_kind(self, object_type: str) -> str:
        if object_type == "vm":
            return "qemu"
        if object_type == "ct":
            return "lxc"
        raise ProxmoxAPIError(f"Unsupported guest type: {object_type}")

    def _request(self, method: str, path: str, *, data: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        verify: bool | str = settings.PVE_VERIFY_TLS
        if settings.PVE_CA_BUNDLE:
            verify = settings.PVE_CA_BUNDLE

        headers = {}
        token_id = settings.PVE_API_TOKEN_ID
        token_secret = settings.PVE_API_TOKEN_SECRET
        if token_id and token_secret:
            headers["Authorization"] = f"PVEAPIToken={token_id}={token_secret}"

        try:
            response = httpx.request(
                method,
                f"{self.endpoint}/api2/json/{path.lstrip('/')}",
                headers=headers,
                data=data,
                timeout=timeout or 15,
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

    def _storage_config_map(self) -> dict[str, dict[str, Any]]:
        try:
            data = self.get("storage")
        except ProxmoxAPIError:
            return {}

        storages = data if isinstance(data, list) else []
        return {
            str(storage.get("storage") or ""): storage
            for storage in storages
            if storage.get("storage")
        }

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


def fetch_live_guest_status() -> dict[tuple[str, str, int], str]:
    cached = cache.get(LIVE_GUEST_STATUS_CACHE_KEY)
    if isinstance(cached, dict):
        return cached

    result = _fetch_live_guest_status_uncached()
    cache.set(LIVE_GUEST_STATUS_CACHE_KEY, result, LIVE_GUEST_STATUS_CACHE_SECONDS)
    return result


def fetch_live_guest_inventory(*, use_cache: bool = True) -> list[ProxmoxGuestSummary]:
    if use_cache:
        cached = cache.get(LIVE_GUEST_INVENTORY_CACHE_KEY)
        if isinstance(cached, list):
            return cached

    result = _fetch_live_guest_inventory_uncached()
    if use_cache:
        cache.set(LIVE_GUEST_INVENTORY_CACHE_KEY, result, LIVE_GUEST_INVENTORY_CACHE_SECONDS)
    return result


def clear_live_guest_caches() -> None:
    cache.delete_many([LIVE_GUEST_STATUS_CACHE_KEY, LIVE_GUEST_INVENTORY_CACHE_KEY])


def _fetch_live_guest_status_uncached() -> dict[tuple[str, str, int], str]:
    """Return {(node, object_type, vmid): status} for all guests across all endpoints."""
    return {
        (guest.node, guest.object_type, guest.vmid): guest.status
        for guest in _fetch_live_guest_inventory_uncached()
        if guest.status
    }


def _fetch_live_guest_inventory_uncached() -> list[ProxmoxGuestSummary]:
    """Return lightweight guest inventory across all configured endpoints."""
    guests_by_key: dict[tuple[str, str, int], ProxmoxGuestSummary] = {}
    for client in configured_clients():
        try:
            resources = client.get("cluster/resources?type=vm")
        except ProxmoxAPIError:
            resources = []
        if isinstance(resources, list):
            guest_count_before = len(guests_by_key)
            for resource in resources:
                _add_guest_summary(guests_by_key, resource)
            if len(guests_by_key) > guest_count_before:
                continue

        try:
            nodes = client.get("nodes")
        except ProxmoxAPIError:
            continue
        if not isinstance(nodes, list):
            continue
        for node_info in nodes:
            node = str(node_info.get("node") or "")
            if not node:
                continue
            for resource_type in ("qemu", "lxc"):
                object_type = "vm" if resource_type == "qemu" else "ct"
                try:
                    guests = client.get(f"nodes/{quote(node)}/{resource_type}")
                except ProxmoxAPIError:
                    continue
                if not isinstance(guests, list):
                    continue
                for guest in guests:
                    _add_guest_summary(guests_by_key, guest, node=node, object_type=object_type)
    return sorted(guests_by_key.values(), key=lambda guest: (guest.object_type, guest.vmid, guest.node))


def _add_guest_summary(
    guests_by_key: dict[tuple[str, str, int], ProxmoxGuestSummary],
    data: dict[str, Any],
    *,
    node: str | None = None,
    object_type: str | None = None,
) -> None:
    if not isinstance(data, dict):
        return

    resource_type = object_type
    if resource_type is None:
        raw_type = str(data.get("type") or "")
        if raw_type == "qemu":
            resource_type = "vm"
        elif raw_type == "lxc":
            resource_type = "ct"
        else:
            return

    vmid = data.get("vmid")
    if vmid is None:
        return
    try:
        vmid_int = int(vmid)
    except (TypeError, ValueError):
        return

    guest_node = str(node or data.get("node") or "")
    key = (guest_node, resource_type, vmid_int)
    guests_by_key.setdefault(
        key,
        ProxmoxGuestSummary(
            node=guest_node,
            object_type=resource_type,
            vmid=vmid_int,
            name=str(data.get("name") or data.get("hostname") or ""),
            status=str(data.get("status") or ""),
            cpu=_float_or_zero(data.get("cpu")),
            mem=_int_or_zero(data.get("mem")),
            maxmem=_int_or_zero(data.get("maxmem")),
            disk=_int_or_zero(data.get("disk")),
            maxdisk=_int_or_zero(data.get("maxdisk")),
            uptime=_int_or_zero(data.get("uptime")),
        ),
    )


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

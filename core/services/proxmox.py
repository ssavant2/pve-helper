from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote

import httpx
from django.conf import settings
from django.core.cache import cache

from .classification import extract_disk_references


logger = logging.getLogger(__name__)


LIVE_GUEST_STATUS_CACHE_KEY = "pve-helper:live-guest-status:v1"
LIVE_GUEST_INVENTORY_CACHE_KEY = "pve-helper:live-guest-inventory:v1"
LIVE_GUEST_LOCKS_CACHE_KEY = "pve-helper:live-guest-locks:v1"
LIVE_GUEST_LINEAGE_CACHE_KEY = "pve-helper:live-guest-lineage:v1"
LIVE_GUEST_STATUS_CACHE_SECONDS = 2
LIVE_GUEST_INVENTORY_CACHE_SECONDS = 30
LIVE_GUEST_LOCKS_CACHE_SECONDS = 3
LIVE_GUEST_LINEAGE_CACHE_SECONDS = 30
LIVE_GUEST_DISPLAY_TIMEOUT_SECONDS = 2.5

# A linked clone's disk is a qcow2 whose storage-content `parent` names the
# template's base volume, e.g. "../102/base-102-disk-0.qcow2" → parent VMID 102.
_BASE_VOLUME_RE = re.compile(r"base-(\d+)-disk-")


_http_client: httpx.Client | None = None
_http_client_lock = threading.Lock()


class _TestNetworkDisabledClient:
    def request(self, *_args, **_kwargs):
        raise AssertionError(
            "Test attempted an unmocked Proxmox HTTP request. Patch the client or use an explicit integration suite."
        )


def _shared_http_client() -> httpx.Client:
    """Process-wide pooled HTTP client so Proxmox calls reuse keep-alive TLS
    connections instead of doing a fresh handshake on every request (inventory
    loops and polling endpoints issue many small calls to the same hosts)."""
    if settings.PVE_TEST_NETWORK_DISABLED:
        return _TestNetworkDisabledClient()  # type: ignore[return-value]

    global _http_client
    client = _http_client
    if client is None:
        with _http_client_lock:
            client = _http_client
            if client is None:
                verify: bool | str = settings.PVE_CA_BUNDLE or settings.PVE_VERIFY_TLS
                client = httpx.Client(
                    verify=verify,
                    timeout=15,
                    limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
                )
                _http_client = client
    return client


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
    lock: str = ""
    is_template: bool = False
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerifiedGuestInventory:
    """Uncached inventory with explicit endpoint coverage.

    Display callers intentionally tolerate partial Proxmox responses. Callers
    that use absence as a destructive postcondition must instead require
    ``complete`` before acting on that absence.
    """

    guests: tuple[ProxmoxGuestSummary, ...]
    attempted_endpoints: tuple[str, ...]
    successful_endpoints: tuple[str, ...]
    errors: tuple[str, ...]

    @property
    def complete(self) -> bool:
        """Whether the cluster answered authoritatively — not whether every
        endpoint did.

        This used to require every attempted endpoint to succeed, which conflated
        transport health with logical coverage and punished redundancy: a
        single-endpoint install already treated one answer as complete, while
        adding a second endpoint made an unreachable one block retirement.
        `cluster/resources` is a cluster-wide response from any member, so one
        authoritative answer covers the cluster.
        """
        return bool(self.successful_endpoints)


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


class ProxmoxTransportError(ProxmoxAPIError):
    """The call failed below the API: no Proxmox response was interpreted.

    `request_sent` is the part that matters for writes. A connection that was never
    established proves the mutation did not happen, so another endpoint in the same
    cluster may safely be tried. Anything after the bytes may have left — a read
    timeout, a half-written request, a dropped connection — is ambiguous: the
    mutation may already have been applied, and replaying it elsewhere could double
    it. Ambiguity is assumed unless the failure proves otherwise.

    An HTTP status error is deliberately *not* a transport error: the server
    received the request and decided, so retrying it on another endpoint would ask a
    second member of the same control plane the same settled question.
    """

    def __init__(self, message: str, *, request_sent: bool = True):
        super().__init__(message)
        self.request_sent = request_sent

    @property
    def ambiguous(self) -> bool:
        return self.request_sent


# Failures that prove the request never reached the server. Everything else is
# treated as ambiguous, so this set must only ever contain the provably-unsent.
_UNSENT_TRANSPORT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)


def _proxmox_error_detail(response) -> str:
    """Pull Proxmox's human-readable reason out of an error response.

    Proxmox returns the reason in the JSON body ``message`` and/or in ``errors``
    (per-parameter messages); fall back to the status reason phrase. Without this
    the caller only ever sees a bare status code like ``500``.
    """
    try:
        payload = response.json()
    except (ValueError, TypeError):
        payload = None
    parts: list[str] = []
    if isinstance(payload, dict):
        message = payload.get("message")
        if message:
            parts.append(str(message).strip())
        errors = payload.get("errors")
        if isinstance(errors, dict):
            parts.extend(f"{key}: {value}" for key, value in errors.items())
    detail = " ".join(part for part in parts if part).strip()
    if not detail:
        detail = (getattr(response, "reason_phrase", "") or "").strip()
    return detail


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

    def stop_task(self, *, node: str, upid: str) -> Any:
        return self.delete(f"nodes/{quote(node, safe='')}/tasks/{quote(upid, safe='')}")

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
        guest_kind = self._guest_kind(object_type)
        data: dict[str, Any] = dict(updates)
        if delete:
            data["delete"] = ",".join(delete)
        if digest:
            data["digest"] = digest
        if not data:
            return None
        return self.put(f"nodes/{quote(node, safe='')}/{guest_kind}/{vmid}/config", data=data)

    def cluster_options(self) -> dict[str, Any]:
        result = self.get("cluster/options")
        if not isinstance(result, dict):
            raise ProxmoxAPIError("Proxmox returned an invalid cluster options response.")
        return result

    def set_cluster_options(self, updates: dict[str, Any], *, delete: list[str] | None = None) -> Any:
        data = dict(updates)
        if delete:
            data["delete"] = ",".join(delete)
        if not data:
            return None
        return self.put("cluster/options", data=data)

    def set_storage_content(self, storage_id: str, content: list[str]) -> Any:
        if not settings.STORAGE_WRITE_ENABLED:
            raise ProxmoxAPIError("Storage content writes are disabled.")
        content = [item for index, item in enumerate(content) if item and item not in content[:index]]
        normalized = ",".join(content)
        if not normalized:
            raise ProxmoxAPIError("Storage content cannot be empty.")
        return self.put(f"storage/{quote(storage_id, safe='')}", data={"content": normalized})

    def storage_config(self, storage_id: str) -> dict[str, Any]:
        config = self._storage_config_map().get(storage_id)
        if config is None:
            raise ProxmoxAPIError(f"Storage '{storage_id}' was not found in Proxmox storage config.")
        return config

    def _guest_kind(self, object_type: str) -> str:
        if object_type == "vm":
            return "qemu"
        if object_type == "ct":
            return "lxc"
        raise ProxmoxAPIError(f"Unsupported guest type: {object_type}")

    def _request(self, method: str, path: str, *, data: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        headers = {}
        token_id = settings.PVE_API_TOKEN_ID
        token_secret = settings.PVE_API_TOKEN_SECRET
        if token_id and token_secret:
            headers["Authorization"] = f"PVEAPIToken={token_id}={token_secret}"

        url = f"{self.endpoint}/api2/json/{path.lstrip('/')}"
        request_kwargs: dict[str, Any] = {"headers": headers, "data": data}
        if timeout is not None:
            request_kwargs["timeout"] = timeout
        try:
            response = _shared_http_client().request(method, url, **request_kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _proxmox_error_detail(exc.response)
            suffix = f": {detail}" if detail else f" from {path}"
            raise ProxmoxAPIError(f"{exc.response.status_code}{suffix}") from exc
        except httpx.HTTPError as exc:
            raise ProxmoxTransportError(
                f"{exc.__class__.__name__} from {path}",
                request_sent=not isinstance(exc, _UNSENT_TRANSPORT_ERRORS),
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProxmoxAPIError(f"Invalid JSON response from {path}") from exc
        if not isinstance(payload, dict) or "data" not in payload:
            raise ProxmoxAPIError(f"Unexpected response schema from {path}")
        return payload["data"]

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


def fetch_verified_guest_inventory(*, cluster=None) -> VerifiedGuestInventory:
    """Read one cluster's guest membership authoritatively.

    Unlike :func:`fetch_live_guest_inventory`, this has no display deadline, cache
    or per-node fallback. A list response (including an empty list) is a successful
    authoritative response; a failed or malformed response leaves the result
    incomplete so destructive callers can fail closed.

    `cluster/resources` is a cluster-wide response regardless of which member
    answers, so one authoritative answer is complete coverage for the cluster and
    the guests are stored once rather than merged per endpoint. A redundant
    endpoint being unreachable degrades that endpoint's health without making the
    cluster's answer partial.
    """
    # Imported lazily: cluster_resolver sits above this transport layer and imports
    # from it, so a module-level import here would be circular. The cluster-aware
    # read living in the transport module is the reason; Phase 2 moves this read
    # into the cluster-scoped read model and the seam disappears.
    from core.services.cluster_resolver import (
        cluster_wide_read,
        require_sole_enabled_cluster_for_legacy_caller,
    )

    if cluster is None:
        try:
            cluster = require_sole_enabled_cluster_for_legacy_caller()
        except Exception as exc:
            return VerifiedGuestInventory(
                guests=(),
                attempted_endpoints=(),
                successful_endpoints=(),
                errors=(str(exc),),
            )

    def _read(client) -> list:
        resources = client.get("cluster/resources?type=vm")
        if not isinstance(resources, list):
            raise ProxmoxAPIError("cluster guest inventory returned an invalid response.")
        return resources

    result = cluster_wide_read(cluster, operation="verified_guest_inventory", call=_read)

    guests_by_key: dict[tuple[str, str, int], ProxmoxGuestSummary] = {}
    for resource in result.value or ():
        _add_guest_summary(guests_by_key, resource)
    guests = tuple(
        sorted(guests_by_key.values(), key=lambda guest: (guest.object_type, guest.vmid, guest.node))
    )

    attempted = tuple(attempt.endpoint_name for attempt in result.attempted)
    errors = tuple(
        f"{attempt.endpoint_name}: {attempt.error}" for attempt in result.attempted if not attempt.ok
    )
    if not attempted:
        errors = errors + (f"Cluster '{result.cluster_key}' has no enabled Proxmox endpoint.",)

    return VerifiedGuestInventory(
        guests=guests,
        attempted_endpoints=attempted,
        successful_endpoints=(result.answering_endpoint,) if result.complete else (),
        errors=errors,
    )


def fetch_live_guest_locks() -> dict[tuple[str, str, int], str]:
    """Return {(node, object_type, vmid): lock} for guests that carry a Proxmox
    config lock (``backup``, ``migrate``, ``snapshot``, ``suspended``, ...). The
    lock rides on the per-node ``qemu``/``lxc`` listing (one call per node per
    type), so this is a cluster-wide health signal without any per-VM polling."""
    cached = cache.get(LIVE_GUEST_LOCKS_CACHE_KEY)
    if isinstance(cached, dict):
        return cached
    result = _fetch_live_guest_locks_uncached()
    cache.set(LIVE_GUEST_LOCKS_CACHE_KEY, result, LIVE_GUEST_LOCKS_CACHE_SECONDS)
    return result


def _display_cluster():
    """The cluster a passive display read renders, or None.

    Display reads tolerate missing data by design, so an unresolvable scope
    produces an empty result rather than an exception: a page must not break
    because no cluster is configured yet. Destructive callers use
    fetch_verified_guest_inventory(), which reports coverage explicitly instead.
    """
    from core.services.cluster_resolver import require_sole_enabled_cluster_for_legacy_caller

    try:
        return require_sole_enabled_cluster_for_legacy_caller()
    except Exception:
        return None


def _cluster_nodes(cluster, *, operation: str, deadline: float):
    """List the cluster's nodes from whichever member answers first.

    `nodes` is a cluster-wide response, so one answer covers the cluster and the
    follow-up node-local reads ride on the endpoint that just proved reachable.
    Asking a second member would return the same list and reach the same nodes.
    """
    from core.services.cluster_resolver import cluster_wide_read

    def _read(client):
        timeout = _remaining_display_timeout(deadline)
        if timeout is None:
            raise ProxmoxAPIError("display timeout budget exhausted")
        nodes = client.get("nodes", timeout=timeout)
        if not isinstance(nodes, list):
            raise ProxmoxAPIError("node listing returned an invalid response")
        return nodes

    result = cluster_wide_read(cluster, operation=operation, call=_read)
    return (result.client, result.value or []) if result.complete else (None, [])


def _cluster_resources(cluster, *, operation: str, deadline: float) -> list:
    """Read cluster/resources for guests from whichever member answers first."""
    from core.services.cluster_resolver import cluster_wide_read

    def _read(client):
        timeout = _remaining_display_timeout(deadline)
        if timeout is None:
            raise ProxmoxAPIError("display timeout budget exhausted")
        resources = client.get("cluster/resources?type=vm", timeout=timeout)
        if not isinstance(resources, list):
            raise ProxmoxAPIError("cluster resources returned an invalid response")
        return resources

    result = cluster_wide_read(cluster, operation=operation, call=_read)
    return result.value or []


def _fetch_live_guest_locks_uncached(*, cluster=None) -> dict[tuple[str, str, int], str]:
    locks: dict[tuple[str, str, int], str] = {}
    cluster = cluster or _display_cluster()
    if cluster is None:
        return locks
    deadline = time.monotonic() + LIVE_GUEST_DISPLAY_TIMEOUT_SECONDS
    client, nodes = _cluster_nodes(cluster, operation="guest_locks_nodes", deadline=deadline)
    if client is None:
        return locks

    for node_info in nodes:
        node = str(node_info.get("node") or "") if isinstance(node_info, dict) else ""
        if not node or str(node_info.get("status") or "") != "online":
            continue
        for resource_type, object_type in (("qemu", "vm"), ("lxc", "ct")):
            timeout = _remaining_display_timeout(deadline)
            if timeout is None:
                return locks
            try:
                guests = client.get(f"nodes/{quote(node, safe='')}/{resource_type}", timeout=timeout)
            except ProxmoxAPIError:
                continue
            if not isinstance(guests, list):
                continue
            for guest in guests:
                lock = str(guest.get("lock") or "").strip() if isinstance(guest, dict) else ""
                if not lock:
                    continue
                try:
                    vmid = int(guest.get("vmid"))
                except (TypeError, ValueError):
                    continue
                locks[(node, object_type, vmid)] = lock
    return locks


def fetch_live_guest_lineage() -> dict[int, int]:
    """Return {child VMID: parent-template VMID} for linked clones — a qcow2 whose
    storage-content ``parent`` points at a template's ``base-<N>-disk-`` volume.
    One content listing per images-storage (deduped across nodes); no per-file
    ``qemu-img`` probing. VM/qcow2/file-storage only; other backends yield nothing."""
    cached = cache.get(LIVE_GUEST_LINEAGE_CACHE_KEY)
    if isinstance(cached, dict):
        return cached
    result = _fetch_live_guest_lineage_uncached()
    cache.set(LIVE_GUEST_LINEAGE_CACHE_KEY, result, LIVE_GUEST_LINEAGE_CACHE_SECONDS)
    return result


def _fetch_live_guest_lineage_uncached(*, cluster=None) -> dict[int, int]:
    lineage: dict[int, int] = {}
    cluster = cluster or _display_cluster()
    if cluster is None:
        return lineage
    seen_storages: set[str] = set()
    deadline = time.monotonic() + LIVE_GUEST_DISPLAY_TIMEOUT_SECONDS
    client, nodes = _cluster_nodes(cluster, operation="linked_clone_nodes", deadline=deadline)
    if client is None:
        return lineage

    for node_info in nodes:
        node = str(node_info.get("node") or "") if isinstance(node_info, dict) else ""
        if not node or str(node_info.get("status") or "") != "online":
            continue
        timeout = _remaining_display_timeout(deadline)
        if timeout is None:
            return lineage
        try:
            storages = client.get(f"nodes/{quote(node, safe='')}/storage?content=images", timeout=timeout)
        except ProxmoxAPIError as exc:
            logger.warning(
                "Proxmox read failed: cluster=%s endpoint=%s operation=linked_clone_storages node=%s error=%s",
                cluster.key,
                client.endpoint,
                node,
                exc,
                extra={
                    "proxmox_cluster": cluster.key,
                    "proxmox_endpoint": client.endpoint,
                    "proxmox_operation": "linked_clone_storages",
                    "proxmox_node": node,
                },
            )
            continue
        if not isinstance(storages, list):
            continue
        for storage in storages:
            if not isinstance(storage, dict) or not storage.get("storage"):
                continue
            storage_id = str(storage["storage"])
            # Shared storage is reported by every node that mounts it; its content
            # only needs reading once per cluster.
            if storage_id in seen_storages:
                continue
            seen_storages.add(storage_id)
            timeout = _remaining_display_timeout(deadline)
            if timeout is None:
                return lineage
            try:
                content = client.get(
                    f"nodes/{quote(node, safe='')}/storage/{quote(storage_id, safe='')}/content?content=images",
                    timeout=timeout,
                )
            except ProxmoxAPIError as exc:
                logger.warning(
                    "Proxmox read failed: cluster=%s endpoint=%s operation=linked_clone_content node=%s storage=%s error=%s",
                    cluster.key,
                    client.endpoint,
                    node,
                    storage_id,
                    exc,
                    extra={
                        "proxmox_cluster": cluster.key,
                        "proxmox_endpoint": client.endpoint,
                        "proxmox_operation": "linked_clone_content",
                        "proxmox_node": node,
                        "proxmox_storage": storage_id,
                    },
                )
                continue
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                match = _BASE_VOLUME_RE.search(str(item.get("parent") or ""))
                if not match:
                    continue
                try:
                    child = int(item.get("vmid"))
                    parent = int(match.group(1))
                except (TypeError, ValueError):
                    continue
                if child and parent and child != parent:
                    lineage[child] = parent
    return lineage


def clear_live_guest_caches() -> None:
    cache.delete_many(
        [
            LIVE_GUEST_STATUS_CACHE_KEY,
            LIVE_GUEST_INVENTORY_CACHE_KEY,
            LIVE_GUEST_LOCKS_CACHE_KEY,
            LIVE_GUEST_LINEAGE_CACHE_KEY,
        ]
    )


def _paused_vm_keys(unknown_vm_keys, deadline, *, cluster) -> set[tuple[str, str, int]]:
    """A RAM-suspended VM reports status ``unknown`` in cluster/resources; the
    real signal is ``qmpstatus == paused`` from status/current. Resolve only the
    (rare) unknown VMs, best-effort within the display timeout budget.

    Unlike a cluster-wide listing this addresses a specific node, so the endpoint
    loop is transport failover rather than a fan-out: it is bounded to this
    cluster's endpoints, and a node reached through either member is the same node.
    """
    from core.services.cluster_resolver import cluster_clients

    paused: set[tuple[str, str, int]] = set()
    if not unknown_vm_keys:
        return paused
    for client in cluster_clients(cluster):
        remaining = [key for key in unknown_vm_keys if key not in paused]
        if not remaining:
            break
        for node, object_type, vmid in remaining:
            timeout = _remaining_display_timeout(deadline)
            if timeout is None:
                return paused
            try:
                current = client.get(
                    f"nodes/{quote(node, safe='')}/qemu/{vmid}/status/current", timeout=timeout
                )
            except ProxmoxAPIError:
                continue
            if isinstance(current, dict) and current.get("qmpstatus") == "paused":
                paused.add((node, object_type, vmid))
    return paused


def _hibernated_vm_keys(stopped_vm_keys, deadline, *, cluster) -> set[tuple[str, str, int]]:
    """A hibernated (suspend-to-disk) VM is reported ``stopped`` but carries
    ``lock == suspended``. Resolve it cheaply with one ``nodes/<node>/qemu`` call
    per node (which includes ``lock`` for every VM), not per VM.

    As with the paused lookup, the endpoint loop is transport failover for a
    node-addressed read and stays inside the selected cluster.
    """
    from core.services.cluster_resolver import cluster_clients

    hibernated: set[tuple[str, str, int]] = set()
    if not stopped_vm_keys:
        return hibernated
    stopped_by_node: dict[str, set[int]] = {}
    for node, _object_type, vmid in stopped_vm_keys:
        stopped_by_node.setdefault(node, set()).add(vmid)
    pending_nodes = set(stopped_by_node)
    for client in cluster_clients(cluster):
        for node in list(pending_nodes):
            timeout = _remaining_display_timeout(deadline)
            if timeout is None:
                return hibernated
            try:
                vms = client.get(f"nodes/{quote(node, safe='')}/qemu", timeout=timeout)
            except ProxmoxAPIError:
                continue
            if not isinstance(vms, list):
                continue
            for vm in vms:
                try:
                    vmid = int(vm.get("vmid"))
                except (TypeError, ValueError):
                    continue
                if vmid in stopped_by_node.get(node, set()) and vm.get("lock") == "suspended":
                    hibernated.add((node, "vm", vmid))
            pending_nodes.discard(node)
        if not pending_nodes:
            break
    return hibernated


def _fetch_live_guest_status_uncached() -> dict[tuple[str, str, int], str]:
    """Return {(node, object_type, vmid): status} for all guests.

    This is the hot display path for power state. Keep it status-only and
    bounded; safety checks use direct guest APIs elsewhere.
    """
    statuses: dict[tuple[str, str, int], str] = {}
    cluster = _display_cluster()
    if cluster is None:
        return statuses
    deadline = time.monotonic() + LIVE_GUEST_DISPLAY_TIMEOUT_SECONDS
    resources = _cluster_resources(cluster, operation="live_guest_status", deadline=deadline)

    guests_by_key: dict[tuple[str, str, int], ProxmoxGuestSummary] = {}
    for resource in resources:
        _add_guest_summary(guests_by_key, resource)
    for guest in guests_by_key.values():
        if guest.status:
            statuses[(guest.node, guest.object_type, guest.vmid)] = guest.status

    unknown_vm_keys = [key for key, status in statuses.items() if status == "unknown" and key[1] == "vm"]
    for key in _paused_vm_keys(unknown_vm_keys, deadline, cluster=cluster):
        statuses[key] = "paused"
    stopped_vm_keys = [key for key, status in statuses.items() if status == "stopped" and key[1] == "vm"]
    for key in _hibernated_vm_keys(stopped_vm_keys, deadline, cluster=cluster):
        statuses[key] = "hibernated"
    return statuses


def _fetch_live_guest_inventory_uncached() -> list[ProxmoxGuestSummary]:
    """Return lightweight guest inventory across all configured endpoints."""
    guests_by_key: dict[tuple[str, str, int], ProxmoxGuestSummary] = {}
    cluster = _display_cluster()
    if cluster is None:
        return []
    deadline = time.monotonic() + LIVE_GUEST_DISPLAY_TIMEOUT_SECONDS

    for resource in _cluster_resources(cluster, operation="live_guest_inventory", deadline=deadline):
        _add_guest_summary(guests_by_key, resource)

    if not guests_by_key:
        # cluster/resources gave nothing usable, so ask each node directly through
        # the endpoint that answers. Kept as a display-resilience path only: the
        # authoritative membership read is fetch_verified_guest_inventory().
        client, nodes = _cluster_nodes(cluster, operation="live_guest_inventory_nodes", deadline=deadline)
        if client is not None:
            for node_info in nodes:
                node = str(node_info.get("node") or "") if isinstance(node_info, dict) else ""
                if not node:
                    continue
                for resource_type in ("qemu", "lxc"):
                    object_type = "vm" if resource_type == "qemu" else "ct"
                    timeout = _remaining_display_timeout(deadline)
                    if timeout is None:
                        break
                    try:
                        guests = client.get(f"nodes/{quote(node)}/{resource_type}", timeout=timeout)
                    except ProxmoxAPIError:
                        continue
                    if not isinstance(guests, list):
                        continue
                    for guest in guests:
                        _add_guest_summary(guests_by_key, guest, node=node, object_type=object_type)

    unknown_vm_keys = [key for key, guest in guests_by_key.items() if guest.status == "unknown" and key[1] == "vm"]
    for key in _paused_vm_keys(unknown_vm_keys, deadline, cluster=cluster):
        guests_by_key[key] = replace(guests_by_key[key], status="paused")
    stopped_vm_keys = [key for key, guest in guests_by_key.items() if guest.status == "stopped" and key[1] == "vm"]
    for key in _hibernated_vm_keys(stopped_vm_keys, deadline, cluster=cluster):
        guests_by_key[key] = replace(guests_by_key[key], status="hibernated")
    for key, lock in fetch_live_guest_locks().items():
        if key in guests_by_key:
            guests_by_key[key] = replace(guests_by_key[key], lock=lock)
    return sorted(guests_by_key.values(), key=lambda guest: (guest.object_type, guest.vmid, guest.node))


def _remaining_display_timeout(deadline: float) -> float | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return max(0.1, min(remaining, LIVE_GUEST_DISPLAY_TIMEOUT_SECONDS))


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
            lock=str(data.get("lock") or ""),
            is_template=_int_or_zero(data.get("template")) == 1,
            tags=tuple(part for part in re.split(r"[;,\s]+", str(data.get("tags") or "").strip()) if part),
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

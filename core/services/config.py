from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from django.conf import settings

from core.models import ProxmoxStorageConsumer, StorageMount


@dataclass(frozen=True)
class EndpointDefinition:
    name: str
    url: str


@dataclass(frozen=True)
class StorageDefinition:
    storage_id: str
    display_name: str
    export: str
    path: str
    trash_path: str
    relative_path: str
    trash_relative_path: str
    expected_consumers: list[str]


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


def configured_storage_definitions() -> list[StorageDefinition]:
    expected_consumers = list(settings.PVE_EXPECTED_CONSUMERS)
    candidates = [
        StorageDefinition(
            storage_id=settings.TRUENAS_FS_STORAGE_ID,
            display_name=settings.TRUENAS_FS_STORAGE_ID,
            export=settings.TRUENAS_FS_EXPORT,
            path=settings.TRUENAS_FS_CONTAINER_PATH,
            trash_path=f"{settings.TRUENAS_FS_CONTAINER_PATH.rstrip('/')}/.trash/pve-helper",
            relative_path=settings.TRUENAS_FS_CONTAINER_PATH.removeprefix("/storages/").strip("/"),
            trash_relative_path=f"{settings.TRUENAS_FS_CONTAINER_PATH.removeprefix('/storages/').strip('/')}/.trash/pve-helper",
            expected_consumers=expected_consumers,
        ),
        StorageDefinition(
            storage_id=settings.TRUENAS_VM_STORAGE_ID,
            display_name=settings.TRUENAS_VM_STORAGE_ID,
            export=settings.TRUENAS_VM_EXPORT,
            path=settings.TRUENAS_VM_CONTAINER_PATH,
            trash_path=f"{settings.TRUENAS_VM_CONTAINER_PATH.rstrip('/')}/.trash/pve-helper",
            relative_path=settings.TRUENAS_VM_CONTAINER_PATH.removeprefix("/storages/").strip("/"),
            trash_relative_path=f"{settings.TRUENAS_VM_CONTAINER_PATH.removeprefix('/storages/').strip('/')}/.trash/pve-helper",
            expected_consumers=expected_consumers,
        ),
    ]
    return [
        storage for storage in candidates if storage.storage_id and not storage.storage_id.startswith("replace-with-")
    ]


def sync_storage_consumers(storage: StorageMount, cluster) -> None:
    """Reconcile the storage's expected consumers within one cluster.

    Consumers are cluster-qualified, so this only ever adds, removes or reads rows
    for `cluster`; another cluster's expectations for the same mount are untouched.
    """
    expected = set(storage.expected_consumers or [])
    for node_name in expected:
        ProxmoxStorageConsumer.objects.get_or_create(
            storage=storage,
            cluster=cluster,
            expected_node_name=node_name,
        )

    storage.consumer_statuses.filter(cluster=cluster).exclude(expected_node_name__in=expected).delete()

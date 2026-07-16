from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from django.conf import settings

from core.models import ProxmoxEndpoint, ProxmoxStorageConsumer, StorageMount


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
    expected_consumers: list[str]


def endpoint_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or urlparse(f"https://{url}").hostname or url
    return host.split(".", 1)[0]


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
            expected_consumers=expected_consumers,
        ),
        StorageDefinition(
            storage_id=settings.TRUENAS_VM_STORAGE_ID,
            display_name=settings.TRUENAS_VM_STORAGE_ID,
            export=settings.TRUENAS_VM_EXPORT,
            path=settings.TRUENAS_VM_CONTAINER_PATH,
            trash_path=f"{settings.TRUENAS_VM_CONTAINER_PATH.rstrip('/')}/.trash/pve-helper",
            expected_consumers=expected_consumers,
        ),
    ]
    return [
        storage
        for storage in candidates
        if storage.storage_id and not storage.storage_id.startswith("replace-with-")
    ]


def sync_runtime_configuration() -> tuple[list[ProxmoxEndpoint], list[StorageMount]]:
    endpoints = []
    for definition in configured_endpoint_definitions():
        endpoint, created = ProxmoxEndpoint.objects.get_or_create(
            name=definition.name,
            defaults={
                "url": definition.url,
                "enabled": True,
            },
        )
        if not created and endpoint.url != definition.url:
            endpoint.url = definition.url
            endpoint.save(update_fields=["url", "updated_at"])
        endpoints.append(endpoint)

    storages = []
    for definition in configured_storage_definitions():
        storage, created = StorageMount.objects.get_or_create(
            storage_id=definition.storage_id,
            defaults={
                "display_name": definition.display_name,
                "export": definition.export,
                "path": definition.path,
                "trash_path": definition.trash_path,
                "expected_consumers": definition.expected_consumers,
                "enabled": True,
            },
        )
        if not created:
            storage.display_name = definition.display_name
            storage.export = definition.export
            storage.path = definition.path
            storage.trash_path = definition.trash_path
            storage.expected_consumers = definition.expected_consumers
            storage.save(
                update_fields=[
                    "display_name",
                    "export",
                    "path",
                    "trash_path",
                    "expected_consumers",
                    "updated_at",
                ]
            )
        storages.append(storage)
        sync_storage_consumers(storage)

    return endpoints, storages


def sync_storage_consumers(storage: StorageMount) -> None:
    expected = set(storage.expected_consumers or [])
    for node_name in expected:
        ProxmoxStorageConsumer.objects.get_or_create(
            storage=storage,
            expected_node_name=node_name,
        )

    storage.consumer_statuses.exclude(expected_node_name__in=expected).delete()

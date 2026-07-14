from __future__ import annotations

from django.core.cache import cache

from core.services.proxmox import ProxmoxAPIError, configured_clients
from core.services.tags import RegisteredTag, parse_registered_tags


TAG_REGISTRY_CACHE_KEY = "pve-helper:tag-registry:v1"
TAG_REGISTRY_CACHE_SECONDS = 60


def cluster_options() -> tuple[object | None, dict, str]:
    error = "No Proxmox endpoint could read cluster tag options."
    for client in configured_clients():
        try:
            return client, client.cluster_options(), ""
        except ProxmoxAPIError as exc:
            error = str(exc)
    return None, {}, error


def cache_registered_tags(registered: dict[str, RegisteredTag]) -> None:
    cache.set(TAG_REGISTRY_CACHE_KEY, (registered, ""), TAG_REGISTRY_CACHE_SECONDS)


def registered_tags() -> tuple[dict[str, RegisteredTag], str]:
    cached = cache.get(TAG_REGISTRY_CACHE_KEY)
    if isinstance(cached, tuple) and len(cached) == 2:
        return cached
    _client, options, error = cluster_options()
    result = (parse_registered_tags(options), error)
    if not error:
        cache_registered_tags(result[0])
    return result


def refresh_registered_tags() -> tuple[dict[str, RegisteredTag], str]:
    """Bypass the display cache and replace it only after a verified read."""
    _client, options, error = cluster_options()
    result = (parse_registered_tags(options), error)
    if not error:
        cache_registered_tags(result[0])
    return result

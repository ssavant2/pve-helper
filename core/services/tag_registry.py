from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import contextmanager

from django.core.cache import cache
from django.db import connection

from core.services.proxmox import ProxmoxAPIError, configured_clients
from core.services.public_errors import public_exception_message
from core.services.tags import (
    RegisteredTag,
    join_tags,
    parse_color_map,
    parse_registered_tags,
    parse_tag_style,
    serialize_color_map,
    serialize_tag_style,
)


TAG_REGISTRY_CACHE_KEY = "pve-helper:tag-registry:v1"
TAG_REGISTRY_CACHE_SECONDS = 60
TAG_REGISTRY_CONFLICT_ERROR = (
    "The tag registry changed concurrently. The current Proxmox state was reloaded; review it and try again."
)
_REGISTRY_MUTATION_LOCK_ID = 0x50564554414703
_registry_process_lock = threading.Lock()


@contextmanager
def _registry_mutation_lock():
    """Serialize pve-helper registry writers across threads and processes."""
    with _registry_process_lock:
        if connection.vendor != "postgresql":
            yield
            return
        acquired = False
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)", [_REGISTRY_MUTATION_LOCK_ID])
                acquired = True
            yield
        finally:
            if acquired:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [_REGISTRY_MUTATION_LOCK_ID])


def cluster_options() -> tuple[object | None, dict, str]:
    error = "No Proxmox endpoint could read cluster tag options."
    for client in configured_clients():
        try:
            return client, client.cluster_options(), ""
        except ProxmoxAPIError as exc:
            error = public_exception_message(
                exc,
                operation="tag_registry_read",
                fallback="No Proxmox endpoint could read cluster tag options.",
            )
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


def mutate_registered_tags(
    mutator: Callable[[list[str], dict[str, tuple[str, str]]], None],
    *,
    postcondition: Callable[[dict[str, RegisteredTag]], bool],
) -> tuple[dict[str, RegisteredTag], str]:
    """Serialize one registry mutation and verify its actual Proxmox result.

    Proxmox provides no digest/CAS for cluster options. The advisory lock keeps
    pve-helper writers from racing each other; the authoritative read after the
    write detects a later external winner. We deliberately do not retry a
    conflict because that could overwrite the external administrator's change.
    """
    with _registry_mutation_lock():
        client, options, error = cluster_options()
        if client is None:
            return {}, error

        names = list(parse_registered_tags(options))
        style = parse_tag_style(options.get("tag-style"))
        colors = parse_color_map(style.get("color-map", ""))
        mutator(names, colors)
        names = sorted(dict.fromkeys(names))

        updates: dict[str, str] = {}
        delete: list[str] = []
        if names:
            updates["registered-tags"] = join_tags(names)
        else:
            delete.append("registered-tags")
        if colors:
            style["color-map"] = serialize_color_map(colors)
        else:
            style.pop("color-map", None)
        serialized_style = serialize_tag_style(style)
        if serialized_style:
            updates["tag-style"] = serialized_style
        elif options.get("tag-style") is not None:
            delete.append("tag-style")

        try:
            client.set_cluster_options(updates, delete=delete)
        except ProxmoxAPIError as exc:
            return {}, public_exception_message(
                exc,
                operation="tag_registry_write",
                fallback="Proxmox could not update the tag registry.",
            )
        try:
            final_options = client.cluster_options()
        except ProxmoxAPIError as exc:
            cache.delete(TAG_REGISTRY_CACHE_KEY)
            return {}, public_exception_message(
                exc,
                operation="tag_registry_verify",
                fallback=(
                    "The tag registry write was submitted but its final state could not be verified. "
                    "Refresh before making another registry change."
                ),
            )

        actual = parse_registered_tags(final_options)
        cache_registered_tags(actual)
        if not postcondition(actual):
            return actual, TAG_REGISTRY_CONFLICT_ERROR
        return actual, ""

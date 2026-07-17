from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import contextmanager

from django.core.cache import cache
from django.db import connection

from core.services.proxmox import ProxmoxAPIError
from core.services.cluster_state_identity import (
    cluster_advisory_lock_id,
    cluster_cache_key,
    invalidate_cluster_cache,
)
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


TAG_REGISTRY_CACHE_NAMESPACE = "tag-registry:v2"
TAG_REGISTRY_CACHE_SECONDS = 60
TAG_REGISTRY_CONFLICT_ERROR = (
    "The tag registry changed concurrently. The current Proxmox state was reloaded; review it and try again."
)
_REGISTRY_MUTATION_LOCK_ID = 0x50564554414703
_registry_process_locks: dict[str, threading.Lock] = {}
_registry_process_locks_guard = threading.Lock()


def _process_lock_for(cluster) -> threading.Lock:
    with _registry_process_locks_guard:
        return _registry_process_locks.setdefault(cluster.key, threading.Lock())


@contextmanager
def _registry_mutation_lock(cluster):
    """Serialize registry writers for one cluster, without blocking another."""
    lock_id = cluster_advisory_lock_id(_REGISTRY_MUTATION_LOCK_ID, cluster)
    with _process_lock_for(cluster):
        if connection.vendor != "postgresql":
            yield
            return
        acquired = False
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)", [lock_id])
                acquired = True
            yield
        finally:
            if acquired:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])


def resolve_tag_registry_cluster(cluster=None):
    if cluster is not None:
        return cluster, ""
    from core.services.cluster_resolver import (
        ClusterResolutionError,
        require_sole_enabled_cluster_for_legacy_caller,
    )

    try:
        return require_sole_enabled_cluster_for_legacy_caller(), ""
    except ClusterResolutionError as exc:
        return None, str(exc)


def tag_registry_cache_key(cluster) -> str:
    return cluster_cache_key(TAG_REGISTRY_CACHE_NAMESPACE, cluster)


def cluster_options(cluster=None) -> tuple[object | None, dict, str]:
    """Read one cluster's tag registry options.

    `/cluster/options` is a cluster-wide response, and tag registries are
    per-cluster: same-named tags in different clusters are distinct entries and may
    carry different colours, so a fallback to another cluster's options would merge
    two registries into one writable catalog.
    """
    from core.services.cluster_resolver import cluster_wide_read

    cluster, error = resolve_tag_registry_cluster(cluster)
    if cluster is None:
        return None, {}, error

    result = cluster_wide_read(
        cluster,
        operation="tag_registry_read",
        call=lambda client: client.cluster_options(),
    )
    if not result.complete:
        error = "No Proxmox endpoint could read cluster tag options."
        if result.attempted:
            error = public_exception_message(
                ProxmoxAPIError(result.errors[-1]),
                operation="tag_registry_read",
                fallback=error,
            )
        return None, {}, error
    return result.client, result.value, ""


def cache_registered_tags(registered: dict[str, RegisteredTag], *, cluster) -> None:
    cache.set(
        tag_registry_cache_key(cluster),
        (registered, ""),
        TAG_REGISTRY_CACHE_SECONDS,
    )


def registered_tags(*, cluster=None) -> tuple[dict[str, RegisteredTag], str]:
    cluster, error = resolve_tag_registry_cluster(cluster)
    if cluster is None:
        return {}, error
    cache_key = tag_registry_cache_key(cluster)
    cached = cache.get(cache_key)
    if isinstance(cached, tuple) and len(cached) == 2:
        return cached
    _client, options, error = cluster_options(cluster)
    result = (parse_registered_tags(options), error)
    if not error:
        cache_registered_tags(result[0], cluster=cluster)
    return result


def refresh_registered_tags(*, cluster=None) -> tuple[dict[str, RegisteredTag], str]:
    """Bypass the display cache and replace it only after a verified read."""
    cluster, error = resolve_tag_registry_cluster(cluster)
    if cluster is None:
        return {}, error
    _client, options, error = cluster_options(cluster)
    result = (parse_registered_tags(options), error)
    if not error:
        cache_registered_tags(result[0], cluster=cluster)
    return result


def mutate_registered_tags(
    mutator: Callable[[list[str], dict[str, tuple[str, str]]], None],
    *,
    postcondition: Callable[[dict[str, RegisteredTag]], bool],
    cluster=None,
) -> tuple[dict[str, RegisteredTag], str]:
    """Serialize one registry mutation and verify its actual Proxmox result.

    Proxmox provides no digest/CAS for cluster options. The advisory lock keeps
    pve-helper writers from racing each other; the authoritative read after the
    write detects a later external winner. We deliberately do not retry a
    conflict because that could overwrite the external administrator's change.
    """
    cluster, error = resolve_tag_registry_cluster(cluster)
    if cluster is None:
        return {}, error
    with _registry_mutation_lock(cluster):
        client, options, error = cluster_options(cluster)
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
        # The write may affect tag chips and guest views as well as the registry;
        # advance the shared cluster generation before publishing verified state.
        invalidate_cluster_cache(cluster)
        try:
            final_options = client.cluster_options()
        except ProxmoxAPIError as exc:
            return {}, public_exception_message(
                exc,
                operation="tag_registry_verify",
                fallback=(
                    "The tag registry write was submitted but its final state could not be verified. "
                    "Refresh before making another registry change."
                ),
            )

        actual = parse_registered_tags(final_options)
        cache_registered_tags(actual, cluster=cluster)
        if not postcondition(actual):
            return actual, TAG_REGISTRY_CONFLICT_ERROR
        return actual, ""

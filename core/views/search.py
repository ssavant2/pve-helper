"""Global search across guests, storage and infrastructure objects."""

from __future__ import annotations

import re
from types import SimpleNamespace

from core.models import ProxmoxEndpoint, StorageMount
from core.services.datastore_nav import datastore_url

from .common import *  # noqa: F401,F403
from .guests.read_model_support import _guest_agent_summary, _guest_rows


@app_login_required
def global_search(request):
    query = request.GET.get("q", "").strip()
    tokens = _search_tokens(query)
    if not tokens:
        return JsonResponse({"query": query, "results": []})

    results = []
    results.extend(_global_search_guests(tokens))
    results.extend(_global_search_storages(tokens))
    results.extend(_global_search_hosts(tokens))
    results.extend(_global_search_endpoints(tokens))
    results.sort(key=lambda item: (item["score"], item["category"], item["label"].casefold()))
    return JsonResponse({"query": query, "results": [_public_search_result(item) for item in results[:14]]})


def _global_search_guests(tokens: list[str]) -> list[dict]:
    rows, _live_available, _scan_at = _guest_rows()
    results = []
    for row in rows:
        detail = SimpleNamespace(
            cluster=row.cluster,
            cluster_key=row.cluster_key,
            object_type=row.object_type,
            vmid=row.vmid,
            name=row.name,
            node=row.node,
            status=row.status,
            config=getattr(row, "config", {}),
        )
        agent = _guest_agent_summary(detail, allow_fetch=False)
        agent_ips = agent.get("ips") or []
        agent_os = agent.get("os_pretty_name") or agent.get("os_name") or ""
        agent_hostname = agent.get("hostname") or ""
        parts = [
            row.cluster.display_name if row.cluster else "",
            row.cluster_key,
            row.name,
            row.vmid,
            row.type_label,
            row.node,
            row.state_label,
            row.guest_os_label,
            row.ip_label,
            row.mac_label,
            row.storage_label,
            row.tags_label,
            agent_hostname,
            agent_os,
            *agent_ips,
        ]
        if not _matches_search(parts, tokens):
            continue
        meta = [row.cluster.display_name if row.cluster else row.cluster_key, row.type_label]
        if row.vmid is not None:
            meta.append(str(row.vmid))
        if row.node:
            meta.append(row.node)
        if row.state_label:
            meta.append(row.state_label)
        ip_text = ", ".join([*agent_ips, *getattr(row, "ip_addresses", [])][:3])
        if ip_text:
            meta.append(ip_text)
        results.append(
            _search_result(
                category="Guests",
                kind=row.type_label,
                label=row.name or f"{row.type_label} {row.vmid}",
                meta=" • ".join(meta),
                url=row.detail_url,
                icon_family="vicon",
                icon=(
                    "template"
                    if getattr(row, "is_template", False)
                    else ("container" if row.object_type == ProxmoxInventory.ObjectType.CT else "vm")
                ),
                score=_search_score(
                    tokens,
                    row.cluster.display_name if row.cluster else row.cluster_key,
                    row.name,
                    row.vmid,
                    row.node,
                    agent_hostname,
                    *agent_ips,
                ),
            )
        )
    return results


def _global_search_storages(tokens: list[str]) -> list[dict]:
    results = []
    mounted_ids = set()
    for storage in StorageMount.objects.filter(enabled=True).order_by("display_name"):
        mounted_ids.add(storage.storage_id)
        parts = [
            storage.display_name,
            storage.storage_id,
            storage.export,
            storage.path,
            *(storage.expected_consumers or []),
        ]
        if not _matches_search(parts, tokens):
            continue
        meta = ["Mounted datastore", storage.storage_id]
        if storage.expected_consumers:
            meta.append(", ".join(storage.expected_consumers))
        results.append(
            _search_result(
                category="Storage",
                kind="Datastore",
                label=storage.display_name or storage.storage_id,
                meta=" • ".join(meta),
                url=reverse("core:storage_summary", args=[storage.mount_ref]),
                icon_family="vicon",
                icon="storage",
                score=_search_score(tokens, storage.display_name, storage.storage_id),
            )
        )

    latest_scan = _latest_proxmox_inventory_scan()
    if latest_scan:
        seen_api_storage: set[tuple[str, str, str]] = set()
        for obj in ProxmoxInventory.objects.filter(
            scan_run=latest_scan,
            object_type=ProxmoxInventory.ObjectType.STORAGE,
        ).order_by("node", "name"):
            if not obj.node or not obj.name or obj.name in mounted_ids:
                continue
            if obj.cluster_id is None:
                continue
            key = (obj.cluster.key, obj.node or "", obj.name or "")
            if key in seen_api_storage:
                continue
            seen_api_storage.add(key)
            content = _config_text(obj.config, "content")
            storage_type = _config_text(obj.config, "type")
            shared = _config_text(obj.config, "shared")
            if not _matches_search(
                [obj.cluster.display_name, obj.cluster.key, obj.name, obj.node, storage_type, content, shared],
                tokens,
            ):
                continue
            meta = [obj.cluster.display_name, "API datastore"]
            if obj.node:
                meta.append(obj.node)
            if storage_type:
                meta.append(storage_type)
            if content:
                meta.append(content)
            results.append(
                _search_result(
                    category="Storage",
                    kind="Datastore",
                    label=obj.name or "Storage",
                    meta=" • ".join(meta),
                    url=datastore_url("core:api_storage_summary", obj.cluster.key, obj.name, obj.node),
                    icon_family="vicon",
                    icon="storage",
                    score=_search_score(
                        tokens,
                        obj.cluster.display_name,
                        obj.cluster.key,
                        obj.name,
                        obj.node,
                        storage_type,
                    ),
                )
            )
    return results


def _global_search_hosts(tokens: list[str]) -> list[dict]:
    latest_scan = _latest_proxmox_inventory_scan()
    seen: set[tuple[str, str]] = set()
    candidates = []
    if latest_scan:
        for obj in ProxmoxInventory.objects.filter(
            scan_run=latest_scan,
            object_type=ProxmoxInventory.ObjectType.NODE,
        ).order_by("name"):
            if not obj.name:
                continue
            if obj.cluster_id is None:
                continue
            seen.add((obj.cluster.key, obj.name))
            candidates.append((obj.cluster, obj.name, obj.status, obj.config))
        for obj in (
            ProxmoxInventory.objects.filter(scan_run=latest_scan)
            .exclude(object_type=ProxmoxInventory.ObjectType.NODE)
            .exclude(node="")
            .order_by("node")
        ):
            if obj.cluster_id is None or not obj.node or (obj.cluster.key, obj.node) in seen:
                continue
            seen.add((obj.cluster.key, obj.node))
            candidates.append((obj.cluster, obj.node, "", {}))
    for row in _guest_rows()[0]:
        key = (row.cluster_key, row.node)
        if row.cluster is not None and row.node and key not in seen:
            seen.add(key)
            candidates.append((row.cluster, row.node, "", {}))

    results = []
    for cluster, node, status, config in sorted(
        candidates, key=lambda item: (item[0].display_name.casefold(), item[1].casefold())
    ):
        parts = [
            cluster.display_name,
            cluster.key,
            node,
            status,
            _config_text(config, "cpu"),
            _config_text(config, "mem"),
        ]
        if not _matches_search(parts, tokens):
            continue
        meta = [cluster.display_name, "Host"]
        if status:
            meta.append(status)
        results.append(
            _search_result(
                category="Infrastructure",
                kind="Host",
                label=node,
                meta=" • ".join(meta),
                url=f"{reverse('core:vms_overview')}?{urlencode({'q': node})}",
                icon_family="vicon",
                icon="host",
                score=_search_score(tokens, cluster.display_name, cluster.key, node, status),
            )
        )
    return results


def _global_search_endpoints(tokens: list[str]) -> list[dict]:
    results = []
    for endpoint in (
        ProxmoxEndpoint.objects.filter(enabled=True).select_related("cluster").order_by("cluster__display_name", "name")
    ):
        if not _matches_search(
            [
                endpoint.cluster.display_name,
                endpoint.cluster.key,
                endpoint.name,
                endpoint.url,
                endpoint.last_health_status,
            ],
            tokens,
        ):
            continue
        meta = [endpoint.cluster.display_name, "Proxmox endpoint"]
        if endpoint.last_health_status:
            meta.append(endpoint.last_health_status)
        results.append(
            _search_result(
                category="Infrastructure",
                kind="Cluster",
                label=endpoint.name,
                meta=" • ".join(meta),
                url=reverse("core:dashboard"),
                icon_family="vicon",
                icon="cluster",
                score=_search_score(
                    tokens, endpoint.cluster.display_name, endpoint.cluster.key, endpoint.name, endpoint.url
                ),
            )
        )
    return results


def _search_tokens(query: str) -> list[str]:
    return [token.casefold() for token in re.split(r"\s+", query.strip()) if token.strip()]


def _matches_search(parts: list[object], tokens: list[str]) -> bool:
    haystack = " ".join(str(part) for part in parts if part not in (None, "", "-")).casefold()
    return bool(haystack) and all(token in haystack for token in tokens)


def _search_score(tokens: list[str], *priority_parts: object) -> int:
    priority = " ".join(str(part) for part in priority_parts if part not in (None, "", "-")).casefold()
    if any(priority == token for token in tokens):
        return 0
    if any(priority.startswith(token) for token in tokens):
        return 10
    if all(token in priority for token in tokens):
        return 20
    return 50


def _search_result(
    *,
    category: str,
    kind: str,
    label: str,
    meta: str,
    url: str,
    icon_family: str,
    icon: str,
    score: int,
) -> dict:
    return {
        "category": category,
        "kind": kind,
        "label": label,
        "meta": meta,
        "url": url,
        "icon_family": icon_family,
        "icon": icon,
        "score": score,
    }


def _public_search_result(result: dict) -> dict:
    return {key: value for key, value in result.items() if key != "score"}


def _config_text(config: dict, key: str) -> str:
    if not isinstance(config, dict):
        return ""
    value = config.get(key, "")
    if value is None or isinstance(value, (dict, list, tuple)):
        return ""
    return str(value)

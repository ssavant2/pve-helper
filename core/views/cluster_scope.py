"""Explicit path scope and bounded redirects for pre-multicluster URLs."""

from __future__ import annotations

from collections.abc import Callable

from django.http import Http404, HttpResponse, HttpResponseRedirect, QueryDict
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from core.models import (
    CurrentGuestInventory,
    ProxmoxCluster,
    ProxmoxInventory,
    ProxmoxStorageConsumer,
)

from .common import app_login_required

SAFE_REDIRECT_METHODS = {"GET", "HEAD"}


def cluster_from_path(cluster_key: str) -> ProxmoxCluster:
    cluster = ProxmoxCluster.objects.filter(key=cluster_key).first()
    if cluster is None:
        raise Http404("Proxmox cluster not found")
    return cluster


def _redirect_with_query(request, route_name: str, *, kwargs: dict) -> HttpResponse:
    target = reverse(route_name, kwargs=kwargs)
    query = QueryDict(request.META.get("QUERY_STRING", "")).urlencode()
    location = f"{target}?{query}" if query else target
    if not url_has_allowed_host_and_scheme(
        location,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        raise Http404("Unsafe legacy redirect target")
    return HttpResponseRedirect(location)


def _choice_url(request, route_name: str, *, kwargs: dict) -> str:
    target = reverse(route_name, kwargs=kwargs)
    query = request.META.get("QUERY_STRING", "")
    return f"{target}?{query}" if query else target


def _scope_required_response(request, *, title: str, choices: list[dict]) -> HttpResponse:
    return render(
        request,
        "core/cluster_scope_required.html",
        {"title": title, "cluster_choices": choices},
        status=409,
    )


def _reject_legacy_mutation() -> HttpResponse:
    return HttpResponse(
        "This legacy URL has no cluster scope. Reopen the object through its canonical "
        "cluster-qualified page before retrying the operation.",
        status=409,
        content_type="text/plain; charset=utf-8",
    )


def legacy_cluster_redirect(route_name: str) -> Callable:
    """Redirect an unscoped cluster view only when its scope is unambiguous."""

    @app_login_required
    def view(request, **route_kwargs):
        if request.method not in SAFE_REDIRECT_METHODS:
            return _reject_legacy_mutation()
        clusters = list(ProxmoxCluster.objects.filter(enabled=True).order_by("display_name", "key"))
        if not clusters:
            destination = (
                reverse("core:clusters_overview") if ProxmoxCluster.objects.exists() else reverse("core:cluster_add")
            )
            return HttpResponseRedirect(destination)
        if len(clusters) == 1:
            return _redirect_with_query(
                request,
                route_name,
                kwargs={"cluster_key": clusters[0].key, **route_kwargs},
            )
        choices = [
            {
                "key": cluster.key,
                "label": cluster.display_name,
                "url": _choice_url(
                    request,
                    route_name,
                    kwargs={"cluster_key": cluster.key, **route_kwargs},
                ),
            }
            for cluster in clusters
        ]
        return _scope_required_response(
            request,
            title="Choose a Proxmox cluster",
            choices=choices,
        )

    return view


def legacy_guest_redirect(route_name: str) -> Callable:
    """Resolve an old VMID-only URL without ever guessing across clusters."""

    @app_login_required
    def view(request, object_type: str, vmid: int, **route_kwargs):
        if request.method not in SAFE_REDIRECT_METHODS:
            return _reject_legacy_mutation()
        matches = list(
            CurrentGuestInventory.objects.filter(
                cluster__isnull=False,
                object_type=object_type,
                vmid=vmid,
            )
            .select_related("cluster")
            .order_by("cluster__display_name", "cluster__key")
        )
        clusters = {guest.cluster.key: guest.cluster for guest in matches}
        if not clusters:
            raise Http404("Guest not found")
        common_kwargs = {
            "object_type": object_type,
            "vmid": vmid,
            **route_kwargs,
        }
        if len(clusters) == 1:
            cluster = next(iter(clusters.values()))
            return _redirect_with_query(
                request,
                route_name,
                kwargs={"cluster_key": cluster.key, **common_kwargs},
            )
        choices = [
            {
                "key": cluster.key,
                "label": cluster.display_name,
                "url": _choice_url(
                    request,
                    route_name,
                    kwargs={"cluster_key": cluster.key, **common_kwargs},
                ),
            }
            for cluster in clusters.values()
        ]
        return _scope_required_response(
            request,
            title=f"Choose the cluster containing {object_type.upper()} {vmid}",
            choices=choices,
        )

    return view


def legacy_node_redirect(route_name: str) -> Callable:
    """Resolve an old node/storage URL from cluster-qualified inventory evidence."""

    @app_login_required
    def view(request, node: str, storage: str, **route_kwargs):
        if request.method not in SAFE_REDIRECT_METHODS:
            return _reject_legacy_mutation()
        cluster_ids = set(
            CurrentGuestInventory.objects.filter(
                cluster__enabled=True,
                node=node,
            ).values_list("cluster_id", flat=True)
        )
        cluster_ids.update(
            ProxmoxInventory.objects.filter(
                cluster__enabled=True,
                node=node,
            ).values_list("cluster_id", flat=True)
        )
        # Storage identity, not just guest evidence: a fresh/evacuated node hosts no
        # guest yet is a declared storage consumer, so a storage URL still resolves
        # (or offers the picker) instead of 404-ing for lack of a guest row.
        cluster_ids.update(
            ProxmoxStorageConsumer.objects.filter(
                cluster__enabled=True,
                expected_node_name=node,
            ).values_list("cluster_id", flat=True)
        )
        clusters = list(ProxmoxCluster.objects.filter(pk__in=cluster_ids).order_by("display_name", "key"))
        if not clusters:
            raise Http404("Node not found")
        common_kwargs = {"node": node, "storage": storage, **route_kwargs}
        if len(clusters) == 1:
            return _redirect_with_query(
                request,
                route_name,
                kwargs={"cluster_key": clusters[0].key, **common_kwargs},
            )
        choices = [
            {
                "key": cluster.key,
                "label": cluster.display_name,
                "url": _choice_url(
                    request,
                    route_name,
                    kwargs={"cluster_key": cluster.key, **common_kwargs},
                ),
            }
            for cluster in clusters
        ]
        return _scope_required_response(
            request,
            title=f"Choose the cluster containing node {node}",
            choices=choices,
        )

    return view

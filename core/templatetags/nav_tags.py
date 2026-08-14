"""Tab-persistent ("sticky") object switching.

When the user is on a specific per-object tab (a guest's Networks tab, a
storage's Monitor tab, ...) and switches to a different object, keep them on the
same tab instead of resetting to Summary — vSphere-style sticky tabs.

Deliberately generic so it covers every current tabbed view AND future ones
(Clusters, Network, Tags, new tabs, new nodes) with zero per-view wiring: it
reads the tab segment from the current request path and grafts it onto the
target object's detail prefix. Any module whose per-object URLs look like
``<prefix>/<tab>/`` with a ``summary`` tab gets this for free.
"""

from __future__ import annotations

from django import template
from django.urls import Resolver404, resolve

register = template.Library()


def _active_tab(path: str) -> str:
    """The tab the current page *is*, or empty when it is not an object tab at all.

    The last path segment alone is not evidence. ``/vms/`` is the guest inventory
    list, and its last segment is the literal name of a tab several object families
    have — so reading the segment on its own made every sidebar leaf on that page
    point at its own VMs tab, which is neither where the operator was nor a tab they
    chose. The Datastores tree has been doing exactly that.

    What distinguishes a tab from a coincidence is the sibling: a real object tab
    sits beside a Summary under the same prefix. Asking the URL resolver for that
    sibling keeps the rule generic — no view registers anything, and a family whose
    detail URLs do not end in ``<tab>/`` beside a ``summary/`` simply never sticks.

    One rule, not two. "A list page is short" would also have excluded ``/vms/``,
    and it holds today only because this app's list pages happen to be one segment
    deep — a two-segment overview added later would start sticking with nothing to
    catch it. The sibling is the property that is actually being relied on, so it is
    the property that is tested.
    """

    segments = [segment for segment in path.split("/") if segment]
    if not segments or segments[-1] == "summary":
        return ""
    parent = "/".join(segments[:-1])
    sibling = f"/{parent}/summary/" if parent else "/summary/"
    try:
        resolve(sibling)
    except Resolver404:
        return ""
    return segments[-1]


@register.simple_tag(takes_context=True)
def sticky_object_url(context, summary_url):
    """Return the target object's URL on the tab the user is currently viewing.

    ``summary_url`` is the target object's Summary-tab URL (e.g.
    ``/vms/ct/501/summary/`` or ``/storage/x/summary/``). The tab is taken from
    the current request path's last segment and appended to the target's detail
    prefix (everything before the trailing ``summary/``).

    Falls back to ``summary_url`` whenever the result would not resolve — a
    different object family, a list page, a POST-only subpath, or a tab the
    target does not have — so switching never lands on a broken URL.
    """
    request = context.get("request")
    if not request or not summary_url:
        return summary_url

    trimmed = summary_url.rstrip("/")
    if not trimmed.endswith("/summary"):
        return summary_url
    prefix = trimmed[: -len("summary")]  # keeps trailing slash, e.g. '/vms/ct/501/'

    tab = _active_tab(request.path)
    if not tab:
        return summary_url

    candidate = f"{prefix}{tab}/"
    if candidate == summary_url:
        return summary_url
    try:
        resolve(candidate)
    except Resolver404:
        return summary_url
    return candidate


@register.inclusion_tag("core/partials/nav_cluster_state_badge.html")
def cluster_degraded_badge(cluster):
    """Mark a navigation entry whose cluster is managed but not operational.

    Navigation lists the managed scope, so a disabled or quarantined cluster is
    reachable — its retained inventory, schedules and history are exactly what
    disabling promises to keep. Reachable and indistinguishable from healthy is the
    part that would mislead, so the entry carries the reason it will refuse writes.
    """
    from core.services.cluster_state_labels import cluster_degraded_label

    return {"label": cluster_degraded_label(cluster)}

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

    segments = [seg for seg in request.path.split("/") if seg]
    if not segments:
        return summary_url
    tab = segments[-1]

    candidate = f"{prefix}{tab}/"
    if candidate == summary_url:
        return summary_url
    try:
        resolve(candidate)
    except Resolver404:
        return summary_url
    return candidate

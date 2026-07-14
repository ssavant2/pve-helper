from __future__ import annotations

from functools import wraps

from django.conf import settings
from django.http import Http404, HttpResponseNotAllowed, JsonResponse

from core.models import ProxmoxInventory
from core.services.guests import is_template
from core.services.integration_tokens import authenticate_token
from core.services.tag_catalog import load_tag_catalog
from core.services.tags import parse_tags


def integration_api(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not settings.BACKUP_INTEGRATION_API_ENABLED:
            raise Http404
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])
        if not request.is_secure():
            return JsonResponse({}, status=404)
        header = request.headers.get("Authorization", "")
        scheme, separator, raw = header.partition(" ")
        if not separator or scheme.lower() != "bearer" or authenticate_token(raw) is None:
            return JsonResponse({}, status=401)
        return view(request, *args, **kwargs)

    return wrapped


def _inventory():
    catalog = load_tag_catalog()
    return catalog, catalog.guests, catalog.summaries


def _guest_json(guest):
    type_name = "template" if guest.object_type == ProxmoxInventory.ObjectType.VM and is_template(guest.config) else guest.object_type
    return {
        "vmid": guest.vmid,
        "name": guest.name,
        "node": guest.node,
        "type": type_name,
        "status": guest.status,
        "tags": parse_tags(guest.config),
    }


@integration_api
def api_tags(request):
    catalog, _guests, rows = _inventory()
    return JsonResponse({"meta": catalog.metadata(), "tags": [
        {
            "name": row.name,
            "registered": row.registered,
            "guest_count": row.guest_count,
        }
        for row in rows
    ]})


@integration_api
def api_tag_guests(request, tag: str):
    tag = tag.strip().lower()
    catalog, _guests, rows = _inventory()
    summary = next((row for row in rows if row.name == tag), None)
    if summary is None:
        raise Http404
    return JsonResponse({
        "tag": tag,
        "meta": catalog.metadata(),
        "guests": [_guest_json(guest) for guest in summary.guests],
    })


@integration_api
def api_backup_groups(request):
    catalog, guests, _rows = _inventory()
    prefix = settings.BACKUP_POLICY_TAG_PREFIX
    groups: dict[str, list] = {}
    unassigned, conflicts = [], []
    for guest in guests:
        policies = [tag for tag in parse_tags(guest.config) if tag.startswith(prefix)]
        payload = _guest_json(guest)
        if not policies:
            unassigned.append(payload)
        elif len(policies) > 1:
            conflicts.append({**payload, "policy_tags": policies})
        else:
            groups.setdefault(policies[0], []).append(payload)
    return JsonResponse({
        "meta": catalog.metadata(),
        "policy_prefix": prefix,
        "groups": groups,
        "unassigned": unassigned,
        "conflicts": conflicts,
    })

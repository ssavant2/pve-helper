from __future__ import annotations

from urllib.parse import urlencode

from django.contrib import messages
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.models import ProxmoxInventory, ScanRun
from core.services.guests import is_template
from core.services.proxmox import ProxmoxAPIError
from core.services.tag_actions import prepare_tag_operation, recolor_tag, register_tag, registered_tags
from core.services.tags import inventory_rows, parse_tags

from . import common
from .common import app_login_required, navigation_context, record_audit_event


def _latest_scan():
    return common._latest_proxmox_inventory_scan()


def _tag_context(*, selected: str = "") -> dict:
    registered, registry_error = registered_tags()
    scan = _latest_scan()
    rows = inventory_rows(scan, registered)
    for row in rows:
        row.detail_url = f"{reverse('core:tag_detail')}?{urlencode({'tag': row.name})}"
    return {
        **navigation_context("tags"),
        "tag_rows": rows,
        "registry_error": registry_error,
        "scan": scan,
        "selected_tag": selected,
    }


@app_login_required
def tags_overview(request):
    return render(request, "core/tags.html", _tag_context())


def _wants_json(request) -> bool:
    return request.headers.get("X-Requested-With") == "fetch" or "application/json" in request.headers.get("Accept", "")


def _tag_form_response(request, *, ok: bool, error: str = "", redirect_name: str = "core:tags_overview"):
    if _wants_json(request):
        return JsonResponse({"ok": ok, "error": error}, status=200 if ok else 400)
    if error:
        messages.error(request, error)
    return redirect(redirect_name)


@app_login_required
def tag_detail(request):
    tag = request.GET.get("tag", "").strip().lower()
    if not tag:
        raise Http404("Tag not specified")
    context = _tag_context(selected=tag)
    summary = next((row for row in context["tag_rows"] if row.name == tag), None)
    if summary is None:
        raise Http404("Tag not found")
    context["tag"] = summary
    if context["scan"] is not None:
        try:
            linked_clones = set(common.fetch_live_guest_lineage())
        except Exception:
            # Type is presentation metadata; a lineage outage must not make tag
            # membership administration unavailable.
            linked_clones = set()
        assigned = {(guest.object_type, guest.vmid) for guest in summary.guests}
        assignable = list(
            ProxmoxInventory.objects.filter(
                scan_run=context["scan"],
                object_type__in=[ProxmoxInventory.ObjectType.VM, ProxmoxInventory.ObjectType.CT],
                vmid__isnull=False,
            ).order_by("name", "vmid", "node")
        )
        for guest in assignable:
            guest.tag_target = f"{guest.object_type}:{guest.vmid}@{guest.node}"
            guest.tag_type_label = _tag_type_label(guest, linked_clones)
        for guest in summary.guests:
            guest.tag_type_label = _tag_type_label(guest, linked_clones)
        context["assignable_guests"] = [
            guest for guest in assignable
            if (guest.object_type, guest.vmid) not in assigned and summary.name not in parse_tags(guest.config)
        ]
    return render(request, "core/tag_detail.html", context)


@require_POST
@app_login_required
def tag_create(request):
    _tags, error = register_tag(request.POST.get("tag", ""), request.POST.get("color", ""))
    if error:
        return _tag_form_response(request, ok=False, error=error)
    else:
        name = request.POST.get("tag", "").strip().lower()
        record_audit_event(request, action="tag.registered", object_type="cluster", object_id=name, details={"tag": name})
    return _tag_form_response(request, ok=True)


@require_POST
@app_login_required
def tag_recolor(request):
    name = request.POST.get("tag", "").strip().lower()
    _tags, error = recolor_tag(name, request.POST.get("color", ""))
    if error:
        return _tag_form_response(request, ok=False, error=error)
    else:
        record_audit_event(request, action="tag.recolored", object_type="cluster", object_id=name, details={"tag": name})
    if _wants_json(request):
        return JsonResponse({"ok": True, "error": ""})
    return redirect(f"{reverse('core:tag_detail')}?{urlencode({'tag': name})}")


@require_POST
@app_login_required
def tag_operation(request):
    operation = request.POST.get("operation", "")
    source = request.POST.get("tag", "").strip().lower()
    new_tag = request.POST.get("new_tag", "").strip().lower()
    if operation not in {"delete", "rename"}:
        raise Http404("Unknown tag operation")
    event = record_audit_event(
        request,
        action="tag.bulk_operation",
        object_type="tag",
        object_id=source,
        outcome="queued",
        details={"username": request.user.get_username()},
    )
    error = prepare_tag_operation(event, operation=operation, source_tag=source, new_tag=new_tag)
    if error:
        event.outcome = "failed"
        event.details = {**(event.details or {}), "error": error}
        event.save(update_fields=["outcome", "details"])
        messages.error(request, error)
    elif event.outcome == "queued":
        task_id = common.enqueue_bulk_task("core.services.tag_actions.execute_tag_operation", event.id)
        event.details = {**event.details, "worker_task_id": task_id}
        event.save(update_fields=["details"])
    return redirect("core:tags_overview")


@require_POST
@app_login_required
def tags_refresh(request):
    scan = _latest_scan()
    if scan is None:
        messages.error(request, "Run an inventory scan before refreshing tags.")
        return redirect("core:tags_overview")
    try:
        live = common.fetch_live_guest_inventory(use_cache=False)
    except ProxmoxAPIError as exc:
        messages.error(request, str(exc))
        return redirect("core:tags_overview")
    for guest in live:
        row = ProxmoxInventory.objects.filter(
            scan_run=scan, node=guest.node, object_type=guest.object_type, vmid=guest.vmid
        ).first()
        if row is None:
            continue
        config = dict(row.config or {})
        if guest.tags:
            config["tags"] = ";".join(guest.tags)
        else:
            config.pop("tags", None)
        row.config = config
        row.status = guest.status
        row.save(update_fields=["config", "status", "updated_at"])
    return redirect("core:tags_overview")


def _tag_type_label(guest, linked_clones: set[int]) -> str:
    if guest.object_type == ProxmoxInventory.ObjectType.CT:
        return "ct"
    if guest.object_type == ProxmoxInventory.ObjectType.VM and is_template(guest.config):
        return "template"
    if guest.object_type == ProxmoxInventory.ObjectType.VM and guest.vmid in linked_clones:
        return "linked clone"
    return "vm"

from __future__ import annotations

import logging
from urllib.parse import urlencode

from django.contrib import messages
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import CurrentGuestInventory, ProxmoxCluster, ProxmoxInventory
from core.services.guests import is_template
from core.services.proxmox import ProxmoxAPIError
from core.services.tag_inventory_refresh import (
    TagInventoryRefreshAlreadyActive,
    TagInventoryRefreshQueueError,
    queue_tag_inventory_refresh,
)
from core.services.tag_operation_confirmation import (
    issue_tag_operation_confirmation,
    validate_tag_operation_confirmation,
)
from core.services.tag_actions import (
    TagOperationQueueError,
    enqueue_tag_operation,
    prepare_tag_operation,
    recolor_tag,
    register_tag,
)
from core.services.tag_catalog import load_tag_catalog
from core.services.tags import parse_tags

from . import common
from .common import app_login_required, navigation_context, record_audit_event


logger = logging.getLogger(__name__)


def _catalog_cluster(catalog):
    return ProxmoxCluster.objects.filter(key=catalog.cluster_key).first()


def _tag_context(*, selected: str = "") -> dict:
    catalog = load_tag_catalog()
    rows = list(catalog.summaries)
    for row in rows:
        row.detail_url = f"{reverse('core:tag_detail')}?{urlencode({'tag': row.name})}"
    return {
        **navigation_context("tags"),
        "tag_rows": rows,
        "registry_error": catalog.registry_error,
        "inventory_state": catalog,
        "inventory_errors": catalog.inventory_errors,
        "inventory_refreshed_at": catalog.inventory_refreshed_at,
        "tag_view_rendered_at_ms": int(timezone.now().timestamp() * 1000),
        "selected_tag": selected,
        "cluster_key": catalog.cluster_key,
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
    context["tag_rename_confirmation"] = issue_tag_operation_confirmation(
        operation="rename",
        tag=tag,
        summary=summary,
        user_id=request.user.pk,
        cluster_key=context["cluster_key"],
    )
    context["tag_delete_confirmation"] = issue_tag_operation_confirmation(
        operation="delete",
        tag=tag,
        summary=summary,
        user_id=request.user.pk,
        cluster_key=context["cluster_key"],
    )
    if CurrentGuestInventory.objects.exists():
        cluster = _catalog_cluster(context["inventory_state"])
        try:
            linked_clones = set(common.fetch_live_guest_lineage(cluster=cluster))
        except ProxmoxAPIError as exc:
            # Type is presentation metadata; a lineage outage must not make tag
            # membership administration unavailable.
            logger.warning(
                "Proxmox read failed: operation=tag_detail_linked_clone_lineage tag=%s error=%s",
                tag,
                exc,
                extra={
                    "proxmox_operation": "tag_detail_linked_clone_lineage",
                    "tag_name": tag,
                },
            )
            linked_clones = set()
            context["lineage_error"] = "Linked-clone classification is temporarily unavailable."
        assigned = {
            guest.guest_ref().identity_tuple
            for guest in summary.guests
            if guest.guest_ref() is not None
        }
        assignable = list(context["inventory_state"].guests)
        assignable.sort(key=lambda guest: ((guest.name or "").casefold(), guest.vmid, guest.node))
        for guest in assignable:
            ref = guest.guest_ref()
            guest.tag_target = ref.serialize() if ref is not None else ""
            guest.tag_type_label = _tag_type_label(guest, linked_clones)
        for guest in summary.guests:
            ref = guest.guest_ref()
            guest.tag_target = ref.serialize() if ref is not None else ""
            guest.tag_type_label = _tag_type_label(guest, linked_clones)
        context["assignable_guests"] = [
            guest for guest in assignable
            if guest.tag_target
            and guest.guest_ref().identity_tuple not in assigned
            and summary.name not in parse_tags(guest.config)
        ]
    return render(request, "core/tag_detail.html", context)


@require_POST
@app_login_required
def tag_create(request):
    catalog = load_tag_catalog()
    cluster = _catalog_cluster(catalog)
    _tags, error = register_tag(
        request.POST.get("tag", ""), request.POST.get("color", ""), cluster=cluster
    )
    if error:
        return _tag_form_response(request, ok=False, error=error)
    else:
        name = request.POST.get("tag", "").strip().lower()
        record_audit_event(
            request,
            action="tag.registered",
            object_type="cluster",
            object_id=name,
            cluster=cluster,
            details={"tag": name},
        )
    return _tag_form_response(request, ok=True)


@require_POST
@app_login_required
def tag_recolor(request):
    name = request.POST.get("tag", "").strip().lower()
    catalog = load_tag_catalog()
    cluster = _catalog_cluster(catalog)
    _tags, error = recolor_tag(name, request.POST.get("color", ""), cluster=cluster)
    if error:
        return _tag_form_response(request, ok=False, error=error)
    else:
        record_audit_event(
            request,
            action="tag.recolored",
            object_type="cluster",
            object_id=name,
            cluster=cluster,
            details={"tag": name},
        )
    if _wants_json(request):
        return JsonResponse({"ok": True, "error": ""})
    return redirect(reverse("core:tag_detail") + "?" + urlencode({"tag": name}))


@require_POST
@app_login_required
def tag_operation(request):
    operation = request.POST.get("operation", "")
    source = request.POST.get("tag", "").strip().lower()
    new_tag = request.POST.get("new_tag", "").strip().lower()
    if operation not in {"delete", "rename"}:
        raise Http404("Unknown tag operation")
    catalog = load_tag_catalog()
    cluster = _catalog_cluster(catalog)
    summary = next((row for row in catalog.summaries if row.name == source), None)
    confirmation, error = validate_tag_operation_confirmation(
        request.POST.get("confirmation", ""),
        operation=operation,
        tag=source,
        summary=summary,
        user_id=request.user.pk,
        cluster_key=catalog.cluster_key,
    )
    if error:
        messages.error(request, error)
        if summary is not None:
            return redirect(reverse("core:tag_detail") + "?" + urlencode({"tag": source}))
        return redirect("core:tags_overview")
    event = record_audit_event(
        request,
        action="tag.bulk_operation",
        object_type="tag",
        object_id=source,
        outcome="queued",
        cluster=cluster,
        details={
            "username": request.user.get_username(),
            "cluster_key": confirmation.cluster_key,
        },
    )
    error = prepare_tag_operation(
        event,
        operation=operation,
        source_tag=source,
        new_tag=new_tag,
        confirmed_membership_fingerprint=confirmation.membership_fingerprint,
        cluster_key=confirmation.cluster_key,
    )
    if error:
        event.outcome = "failed"
        event.details = {**(event.details or {}), "error": error}
        event.save(update_fields=["outcome", "details"])
        messages.error(request, error)
    elif event.outcome == "queued":
        try:
            enqueue_tag_operation(event)
        except TagOperationQueueError:
            messages.error(request, "The tag operation could not be queued; retry is safe.")
    return redirect("core:tags_overview")


@require_POST
@app_login_required
def tags_refresh(request):
    catalog = load_tag_catalog()
    try:
        event, task_id = queue_tag_inventory_refresh(
            request=request,
            cluster=_catalog_cluster(catalog),
        )
    except TagInventoryRefreshAlreadyActive:
        error = "A tag inventory refresh is already queued or running."
        if _wants_json(request):
            return JsonResponse({"ok": False, "error": error}, status=409)
        messages.error(request, error)
    except TagInventoryRefreshQueueError:
        error = "The tag inventory refresh could not be queued."
        if _wants_json(request):
            return JsonResponse({"ok": False, "error": error}, status=503)
        messages.error(request, error)
    else:
        if _wants_json(request):
            return JsonResponse(
                {"ok": True, "task_id": f"guest:{event.id}", "queued_task_id": task_id},
                status=202,
            )
    return redirect("core:tags_overview")


def _tag_type_label(guest, linked_clones: set[int]) -> str:
    if guest.object_type == ProxmoxInventory.ObjectType.CT:
        return "ct"
    if guest.object_type == ProxmoxInventory.ObjectType.VM and is_template(guest.config):
        return "template"
    if guest.object_type == ProxmoxInventory.ObjectType.VM and guest.vmid in linked_clones:
        return "linked clone"
    return "vm"

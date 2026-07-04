from __future__ import annotations

from .common import *  # noqa: F401,F403
from . import common


@app_login_required
def audit_log(request):
    try:
        audit_page = int(request.GET.get("page", "0"))
    except ValueError:
        audit_page = 0
    audit_page = max(0, audit_page)

    valid_modules = {"all", "auth", "clusters", "vms", "storage", "network", "system"}
    module_filter = request.GET.get("filter", "all")
    if module_filter not in valid_modules:
        module_filter = "all"
    query = request.GET.get("q", "").strip()[:200]

    events_qs = AuditEvent.objects.all()
    if module_filter != "all":
        events_qs = events_qs.filter(module=module_filter)
    if query:
        events_qs = events_qs.filter(
            Q(username__icontains=query)
            | Q(action__icontains=query)
            | Q(object_id__icontains=query)
            | Q(object_type__icontains=query)
            | Q(source_ip__icontains=query)
            | Q(path__icontains=query)
        )

    event_total = events_qs.count()
    max_page = (event_total - 1) // AUDIT_PAGE_SIZE if event_total else 0
    audit_page = min(audit_page, max_page)
    event_offset = audit_page * AUDIT_PAGE_SIZE
    events = list(events_qs.order_by("-timestamp")[event_offset:event_offset + AUDIT_PAGE_SIZE])
    _decorate_audit_events(events)
    context = {
        **navigation_context("audit"),
        "events": events,
        "audit_page": audit_page,
        "audit_has_prev": audit_page > 0,
        "audit_has_next": event_offset + len(events) < event_total,
        "audit_start": event_offset + 1 if event_total else 0,
        "audit_end": event_offset + len(events),
        "audit_total": event_total,
        "audit_filter": module_filter,
        "audit_query": query,
        "audit_retention_schedule": audit_retention_schedule_state(),
        "audit_filters": [
            {"key": "all", "label": "All"},
            {"key": "auth", "label": "Auth"},
            {"key": "clusters", "label": "Clusters"},
            {"key": "vms", "label": "VMs"},
            {"key": "storage", "label": "Storage"},
            {"key": "network", "label": "Network"},
            {"key": "system", "label": "System"},
        ],
    }
    return render(request, "core/audit_log.html", context)


@require_POST
@app_login_required
def update_audit_retention_schedule_view(request):
    redirect_to = _safe_next_url(request)
    enabled = request.POST.get("enabled") == "on"
    try:
        retention_days = int(request.POST.get("retention_days", "90"))
        state = update_audit_retention_schedule(enabled=enabled, retention_days=retention_days)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_to)

    record_audit_event(
        request,
        action="audit.retention.schedule.updated",
        object_type="audit_retention_schedule",
        object_id="automatic-audit-retention",
        details={
            "enabled": state.enabled,
            "retention_days": state.retention_days,
            "next_run": state.next_run.isoformat() if state.next_run else "",
        },
    )

    return redirect(redirect_to)

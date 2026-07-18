"""Guest create + configure tab + agent-summary enrichment API (from _core)."""
from __future__ import annotations
from core.models import ProxmoxCluster
from ..common import *  # noqa: F401,F403
from .. import common
from ._core import _create_guest
from .presenters import _guest_config_sections
from .read_model_support import (_guest_agent_summary,_guest_os_label,_guest_tab_context,_require_guest)


@app_login_required
def guest_create(request, cluster_key: str, object_type: str):
    if object_type not in GUEST_OBJECT_TYPES:
        raise Http404("Unknown guest type")

    is_vm = object_type == ProxmoxInventory.ObjectType.VM
    node_param = request.POST.get("node") if request.method == "POST" else request.GET.get("node")
    cluster = ProxmoxCluster.objects.filter(key=cluster_key).first()
    if cluster is None:
        raise Http404("Proxmox cluster not found")
    options = create_options(object_type, node_param, cluster=cluster)
    if not options.get("available"):
        messages.error(request, "Could not load creation options from Proxmox (no reachable node).")
        return redirect("core:vms")

    if request.method == "POST":
        error = _create_guest(request, object_type, options, cluster=cluster)
        if error is None:
            return redirect("core:vms")
        messages.error(request, error)
        form_values = request.POST
    else:
        form_values = {
            "vmid": options.get("nextid", ""),
            "cores": "1",
            "sockets": "1",
            "memory": "2048" if is_vm else "512",
            "disk_size": "32" if is_vm else "8",
            "swap": "512",
            "ip": "dhcp",
        }

    context = {
        **navigation_context("vms"),
        "object_type": object_type,
        "cluster_key": cluster.key,
        "is_vm": is_vm,
        "options": options,
        "form_values": form_values,
    }
    return render(request, "core/guest_create.html", context)




@app_login_required
def guest_configure(request, cluster_key: str, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid, cluster_key=cluster_key)
    agent_summary = _guest_agent_summary(detail, allow_fetch=True)
    actions = list(
        ScheduledAction.objects.filter(
            cluster=detail.cluster,
            target_type=object_type,
            target_vmid=vmid,
            deleted_at__isnull=True,
        ).order_by("-enabled", "next_run_at", "name")
    )
    for action in actions:
        action.display_schedule = _scheduled_action_schedule_label(action)
        action.display_status_class = _scheduled_action_status_class(action.last_status)
    context = _guest_tab_context(detail, "configure")
    context["config_sections"] = _guest_config_sections(detail.config, agent_summary=agent_summary)
    context["scheduled_actions"] = actions
    context["scheduled_task_create_url"] = (
        f"{reverse('core:scheduled_task_create')}?{urlencode({'target': detail.guest_ref.without_node().serialize()})}"
    )
    return render(request, "core/guest_configure.html", context)




@app_login_required
def guest_agent_summary_api(request, cluster_key: str, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid, cluster_key=cluster_key)
    summary = _guest_agent_summary(detail, allow_fetch=True)
    rows = []
    if summary.get("os_name"):
        rows.append({"label": "OS name", "value": summary["os_name"]})
    if summary.get("os_version"):
        version = str(summary["os_version"])
        if summary.get("os_version_id"):
            version = f"{version} ({summary['os_version_id']})"
        rows.append({"label": "Version", "value": version})
    if summary.get("architecture"):
        rows.append({"label": "Architecture", "value": summary["architecture"]})
    if summary.get("kernel_release"):
        rows.append({"label": "Kernel", "value": summary["kernel_release"]})
    if summary.get("hostname"):
        rows.append({"label": "DNS name", "value": summary["hostname"]})
    if summary.get("ips"):
        rows.append({"label": "IP addresses", "value": "\n".join(summary["ips"])})

    return JsonResponse(
        {
            "enabled": summary.get("enabled", False),
            "running": summary.get("running", False),
            "guest_status": detail.status,
            "os_label": summary.get("os_pretty_name") or summary.get("os_name") or _guest_os_label(detail.config),
            "rows": rows,
            "status_label": "Running" if summary.get("running") else "Not running",
        }
    )

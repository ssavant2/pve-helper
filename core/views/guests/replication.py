"""Guest replication tab + create/delete (extracted from _core)."""

from .. import common
from ..common import (
    ProxmoxAPIError,
    app_login_required,
    messages,
    quote,
    redirect,
    render,
    require_POST,
)
from .operation_lifecycle import _guest_write, _write_result
from .read_model_support import _guest_tab_context, _require_guest


@app_login_required
def guest_replication(request, cluster_key: str, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid, cluster_key=cluster_key)
    jobs = []
    error = ""
    clients = common.cluster_scoped_clients(detail.cluster)
    try:
        raw = clients[0].get("cluster/replication") if clients else []
        for job in raw if isinstance(raw, list) else []:
            if str(job.get("guest", "")) == str(vmid) or str(job.get("id", "")).startswith(f"{vmid}-"):
                jobs.append(
                    {
                        "id": job.get("id", ""),
                        "target": job.get("target", ""),
                        "schedule": job.get("schedule", ""),
                        "rate": job.get("rate", ""),
                        "disabled": str(job.get("disable", "0")) in ("1", "True", "true"),
                        "comment": job.get("comment", ""),
                    }
                )
    except ProxmoxAPIError as exc:
        error = str(exc)
    target_nodes = []
    if clients:
        target_nodes = [n for n in clients[0].node_names(fallback="") if n != detail.node]
    context = _guest_tab_context(detail, "replication")
    context.update({"replication_jobs": jobs, "replication_error": error, "target_nodes": target_nodes})
    return render(request, "core/guest_replication.html", context)


@require_POST
@app_login_required
def guest_replication_create(request, cluster_key, object_type, vmid):
    detail = _require_guest(object_type, vmid, cluster_key=cluster_key)
    target = request.POST.get("target", "").strip()
    if not target:
        messages.error(request, "Select a target node.")
        return redirect(
            "core:guest_replication",
            cluster_key=cluster_key,
            object_type=object_type,
            vmid=vmid,
        )
    body = {"id": f"{vmid}-0", "type": "local", "target": target}
    schedule = request.POST.get("schedule", "").strip()
    if schedule:
        body["schedule"] = schedule
    # A replication job must not be created twice: a create whose response was
    # lost may already have registered the job.
    err = _guest_write(
        detail,
        operation="guest_replication_create",
        fallback="Proxmox could not create the replication job.",
        call=lambda client: client.post("cluster/replication", data=body),
    ).error
    return _write_result(request, detail, "core:guest_replication", err, "guest.replication.create", {"target": target})


@require_POST
@app_login_required
def guest_replication_delete(request, cluster_key, object_type, vmid):
    detail = _require_guest(object_type, vmid, cluster_key=cluster_key)
    job_id = request.POST.get("job_id", "").strip()
    if not job_id:
        messages.error(request, "Missing job id.")
        return redirect(
            "core:guest_replication",
            cluster_key=cluster_key,
            object_type=object_type,
            vmid=vmid,
        )
    err = _guest_write(
        detail,
        operation="guest_replication_delete",
        fallback="Proxmox could not delete the replication job.",
        call=lambda client: client.delete(f"cluster/replication/{quote(job_id, safe='')}"),
    ).error
    return _write_result(request, detail, "core:guest_replication", err, "guest.replication.delete", {"job_id": job_id})

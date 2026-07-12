"""Guest replication tab + create/delete (extracted from _core)."""
from ..common import *  # noqa: F401,F403
from .. import common
from ._core import (_guest_tab_context,_require_guest,_vm_write_disabled_redirect,_write_result)


@app_login_required
def guest_replication(request, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid)
    jobs = []
    error = ""
    try:
        raw = common.configured_clients()[0].get("cluster/replication") if common.configured_clients() else []
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
    if common.configured_clients():
        target_nodes = [n for n in common.configured_clients()[0].node_names(fallback="") if n != detail.node]
    context = _guest_tab_context(detail, "replication")
    context.update({"replication_jobs": jobs, "replication_error": error, "target_nodes": target_nodes})
    return render(request, "core/guest_replication.html", context)




@require_POST
@app_login_required
def guest_replication_create(request, object_type, vmid):
    disabled = _vm_write_disabled_redirect(request, object_type, vmid, "core:guest_replication")
    if disabled:
        return disabled
    detail = _require_guest(object_type, vmid)
    target = request.POST.get("target", "").strip()
    if not target:
        messages.error(request, "Select a target node.")
        return redirect("core:guest_replication", object_type=object_type, vmid=vmid)
    body = {"id": f"{vmid}-0", "type": "local", "target": target}
    schedule = request.POST.get("schedule", "").strip()
    if schedule:
        body["schedule"] = schedule
    err = ""
    for client in common.configured_clients():
        try:
            client.post("cluster/replication", data=body)
            err = ""
            break
        except ProxmoxAPIError as exc:
            err = str(exc)
    return _write_result(request, detail, "core:guest_replication", err, "guest.replication.create", {"target": target})




@require_POST
@app_login_required
def guest_replication_delete(request, object_type, vmid):
    disabled = _vm_write_disabled_redirect(request, object_type, vmid, "core:guest_replication")
    if disabled:
        return disabled
    detail = _require_guest(object_type, vmid)
    job_id = request.POST.get("job_id", "").strip()
    if not job_id:
        messages.error(request, "Missing job id.")
        return redirect("core:guest_replication", object_type=object_type, vmid=vmid)
    err = ""
    for client in common.configured_clients():
        try:
            client.delete(f"cluster/replication/{quote(job_id, safe='')}")
            err = ""
            break
        except ProxmoxAPIError as exc:
            err = str(exc)
    return _write_result(request, detail, "core:guest_replication", err, "guest.replication.delete", {"job_id": job_id})





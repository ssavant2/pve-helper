"""Register / import a VM from an existing disk image (wizard views).

Two entry points feed the same wizard:
- orphan finder → ``?mode=adopt&volid=<derived_volid>``
- file browser  → ``?mode=import&storage=<id>&path=<rel>``

Adopt is quick and runs inline; import is a worker task (the disk copy can take
minutes) tracked through Recent Tasks like other Proxmox tasks. Backed by
``core.services.vm_register`` (mechanism verified against a live token).
"""

from __future__ import annotations

import os

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect, render
from django_q.tasks import async_task

from core.models import StorageMount
from core.services.guest_create import create_options
from core.services import vm_register as reg

from .common import app_login_required, navigation_context, record_audit_event


def _register_options(node: str | None = None) -> dict:
    options = create_options("vm", node)
    options.update(
        {
            "disk_buses": reg.DISK_BUSES,
            "nic_models": reg.NIC_MODELS,
            "bios_choices": reg.BIOS_CHOICES,
            "machine_choices": reg.MACHINE_CHOICES,
        }
    )
    return options


def _sanitize_name(raw: str) -> str:
    base = os.path.splitext(os.path.basename(raw or ""))[0]
    cleaned = "".join(ch if (ch.isalnum() or ch in "-.") else "-" for ch in base).strip("-.")
    return cleaned or "imported-vm"


def _defaults() -> dict:
    return {
        "cores": "2",
        "sockets": "1",
        "memory": "2048",
        "bios": "seabios",
        "machine": "i440fx",
        "ostype": "l26",
        "disk_bus": "sata",
        "format": "qcow2",
        "nic0_model": "e1000",
    }


def _parse_nics(post) -> list[dict]:
    nics = []
    for index in range(8):
        bridge = post.get(f"nic{index}_bridge", "").strip()
        if not bridge:
            continue
        nics.append(
            {
                "model": post.get(f"nic{index}_model", "e1000").strip() or "e1000",
                "bridge": bridge,
                "vlan": post.get(f"nic{index}_vlan", "").strip(),
            }
        )
    return nics


def _params_from_post(post, node: str) -> dict:
    return {
        "node": node,
        "vmid": post.get("vmid", "").strip(),
        "name": post.get("name", "").strip(),
        "cores": post.get("cores", "2").strip() or "2",
        "sockets": post.get("sockets", "1").strip() or "1",
        "memory": post.get("memory", "2048").strip() or "2048",
        "bios": post.get("bios", "seabios").strip() or "seabios",
        "machine": post.get("machine", "i440fx").strip() or "i440fx",
        "ostype": post.get("ostype", "l26").strip() or "l26",
        "disk_bus": post.get("disk_bus", "sata").strip() or "sata",
        "nics": _parse_nics(post),
        "start": post.get("start") == "on",
    }


@app_login_required
def register_vm(request):
    if not settings.VM_WRITE_ENABLED:
        messages.error(request, "VM creation is disabled (VM_WRITE_ENABLED is off).")
        return redirect("core:vms")

    src = request.POST if request.method == "POST" else request.GET
    mode = src.get("mode", "")
    if mode not in ("adopt", "import"):
        messages.error(request, "Unknown register mode.")
        return redirect("core:vms")

    options = _register_options(src.get("node") or None)
    if not options.get("available"):
        messages.error(request, "Could not load creation options from Proxmox.")
        return redirect("core:vms")

    if request.method == "POST":
        error = _register_submit(request, mode, options)
        if error is None:
            return redirect("core:vms")
        messages.error(request, error)
        form_values = request.POST
    elif mode == "adopt":
        volid = request.GET.get("volid", "")
        vmid = reg.vmid_from_volid(volid)
        form_values = {
            **_defaults(),
            "mode": "adopt",
            "volid": volid,
            "vmid": str(vmid) if vmid else options.get("nextid", ""),
            "name": f"adopted-{vmid}" if vmid else "",
            "node": options.get("node", ""),
        }
    else:
        path = request.GET.get("path", "")
        form_values = {
            **_defaults(),
            "mode": "import",
            "source_storage": request.GET.get("storage", ""),
            "source_path": path,
            "vmid": options.get("nextid", ""),
            "name": _sanitize_name(path),
            "node": options.get("node", ""),
            "target_storage": (options.get("disk_storages") or [""])[0],
        }

    default_bridge = (options.get("bridges") or [""])[0]
    if request.method == "POST":
        nic_rows = _parse_nics(request.POST)
    else:
        nic_rows = []
    if not nic_rows:
        nic_rows = [{"model": "e1000", "bridge": default_bridge, "vlan": ""}]

    context = {
        **navigation_context("vms"),
        "mode": mode,
        "options": options,
        "form_values": form_values,
        "nic_rows": nic_rows,
        "default_bridge": default_bridge,
    }
    return render(request, "core/vm_register.html", context)


def _register_submit(request, mode: str, options: dict) -> str | None:
    post = request.POST
    node = post.get("node", "").strip() or options.get("node", "")
    params = _params_from_post(post, node)
    if not params["vmid"].isdigit():
        return "VMID must be a whole number."
    if not params["name"]:
        return "Name is required."
    if params["bios"] == "ovmf":
        params["efidisk_storage"] = (
            post.get("efidisk_storage", "").strip() or post.get("target_storage", "").strip()
        )
        efi_source = post.get("efidisk_source", "").strip()
        if efi_source:
            params["efidisk_source"] = efi_source

    if mode == "adopt":
        volid = post.get("volid", "").strip()
        if not volid:
            return "Missing orphan disk volume."
        if params["bios"] == "ovmf" and not params.get("efidisk_storage"):
            params["efidisk_storage"] = volid.split(":", 1)[0]
        try:
            _upid, err = reg.adopt_orphan_disk(node, volid, params)
        except reg.VmRegisterError as exc:
            return str(exc)
        if err:
            return err
        record_audit_event(
            request,
            action="guest.register.adopt",
            object_type="guest",
            object_id=f"vm:{params['vmid']}",
            outcome="success",
            details={"vmid": params["vmid"], "name": params["name"], "volid": volid, "node": node},
        )
        messages.success(
            request, f"VM {params['vmid']} ({params['name']}) created from the adopted disk."
        )
        return None

    # import
    storage_id = post.get("source_storage", "").strip()
    source_path = post.get("source_path", "").strip()
    if not storage_id or not source_path:
        return "Missing source image."
    if not StorageMount.objects.filter(storage_id=storage_id, enabled=True).exists():
        return "Unknown source storage."
    params["target_storage"] = post.get("target_storage", "").strip()
    if not params["target_storage"]:
        return "Select a target storage for the imported disk."
    params["format"] = post.get("format", "qcow2").strip() or "qcow2"

    event = record_audit_event(
        request,
        action="guest.register.import",
        object_type="guest",
        object_id=f"vm:{params['vmid']}",
        outcome="running",
        details={
            "vmid": params["vmid"],
            "name": params["name"],
            "node": node,
            "source": f"{storage_id}:{source_path}",
            "target_storage": params["target_storage"],
        },
    )
    task_id = async_task(
        "core.tasks.register_import_vm_task",
        event.id,
        node,
        params,
        storage_id,
        source_path,
    )
    event.details = {**event.details, "poll_task_id": task_id}
    event.save(update_fields=["details"])
    messages.success(
        request,
        f"Importing disk into VM {params['vmid']} ({params['name']}); watch Recent Tasks for progress.",
    )
    return None

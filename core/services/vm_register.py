"""Register / import a VM from an existing disk image.

Two flows, both verified against a live PVE token (see docs/vm-addons-module3):

- **adopt**: an orphan disk volume (already a catalogued `vm-<id>-disk-N`
  volume with no VM config) is attached to a freshly created, empty VM that
  reuses the orphan's VMID. No copy, no import.
- **import**: a loose disk image the file browser can see is turned into a new
  VM. The API token may not pass an arbitrary filesystem path to `import-from`
  ("Only root can pass arbitrary filesystem paths"), so the source is first
  *staged* as a real storage volume via a hardlink into `images/<tmpid>/` on the
  same mount (instant, no extra space), giving a volid that `import-from`
  accepts. The stage is removed once the import task finishes.

The long-running import is meant to be driven from the worker: it creates the
VM, polls the resulting UPID, then cleans up the stage.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from core.models import StorageMount
from core.services.proxmox import ProxmoxAPIError, configured_clients

# Disk bus -> config key prefix. The default bus is SATA (boots almost any image
# without extra guest drivers); virtio-scsi is faster but needs drivers.
DISK_BUSES = [
    ("sata", "SATA (most compatible)"),
    ("scsi", "SCSI (virtio-scsi, fastest)"),
    ("virtio", "VirtIO Block"),
    ("ide", "IDE (legacy)"),
]
NIC_MODELS = [
    ("e1000", "Intel E1000 (most compatible)"),
    ("virtio", "VirtIO (fastest)"),
    ("rtl8139", "Realtek RTL8139"),
    ("vmxnet3", "VMware vmxnet3"),
]
BIOS_CHOICES = [("seabios", "SeaBIOS (legacy)"), ("ovmf", "OVMF (UEFI)")]
MACHINE_CHOICES = [("i440fx", "i440fx (most compatible)"), ("q35", "q35 (modern, PCIe)")]

_VOLID_VMID_RE = re.compile(r"/(?:vm|base)-(\d+)-disk-")


class VmRegisterError(Exception):
    """Raised for pre-flight / staging problems before any Proxmox write."""


def _client():
    clients = configured_clients()
    if not clients:
        raise VmRegisterError("No Proxmox endpoint is configured.")
    return clients[0]


def vmid_from_volid(volid: str) -> int | None:
    """Parse the owning VMID out of a disk volid (``storage:9999/vm-9999-disk-0``)."""
    match = _VOLID_VMID_RE.search(volid or "")
    return int(match.group(1)) if match else None


def _bus_key(bus: str) -> str:
    if bus not in {b for b, _ in DISK_BUSES}:
        raise VmRegisterError(f"Unsupported disk bus: {bus}")
    return f"{bus}0"


def _base_body(params: dict[str, Any]) -> dict[str, Any]:
    """Build the shared ``POST .../qemu`` body (everything except the boot disk)."""
    body: dict[str, Any] = {
        "vmid": params["vmid"],
        "name": params["name"],
        "cores": params.get("cores", 2),
        "sockets": params.get("sockets", 1),
        "memory": params.get("memory", 2048),
        "ostype": params.get("ostype", "l26"),
        "bios": params.get("bios", "seabios"),
    }
    # Proxmox's machine values are "q35" or "pc"; i440fx is the default, so it is
    # sent only when explicitly q35 (passing "i440fx" is rejected with a 400).
    if params.get("machine") == "q35":
        body["machine"] = "q35"
    if params.get("bios") == "ovmf":
        source = params.get("efidisk_source")
        storage = params.get("efidisk_storage") or params.get("target_storage")
        if source:
            body["efidisk0"] = f"{storage}:0,import-from={source},efitype=4m,pre-enrolled-keys=1"
        else:
            body["efidisk0"] = f"{storage}:1,efitype=4m,pre-enrolled-keys=1"
    if params.get("scsihw"):
        body["scsihw"] = params["scsihw"]
    for index, nic in enumerate(params.get("nics") or []):
        model = nic.get("model") or "e1000"
        bridge = nic.get("bridge")
        if not bridge:
            continue
        spec = f"{model},bridge={bridge}"
        if nic.get("vlan"):
            spec += f",tag={nic['vlan']}"
        body[f"net{index}"] = spec
    # An empty CD/DVD so an ISO can be mounted later.
    body["ide2"] = "none,media=cdrom"
    return body


def _poll_task(client, node: str, upid: str, *, timeout: int = 3600) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get(f"nodes/{quote(node, safe='')}/tasks/{quote(upid, safe='')}/status")
        if status.get("status") == "stopped":
            return str(status.get("exitstatus") or "")
        time.sleep(2)
    return "timeout"


# --------------------------------------------------------------------------- #
# Adopt: attach an existing orphan disk to a new, empty VM.
# --------------------------------------------------------------------------- #
def adopt_orphan_disk(node: str, volid: str, params: dict[str, Any]) -> tuple[str, str | None]:
    """Create an empty VM (reusing the orphan's VMID) and attach ``volid``.

    Returns ``(upid_of_create, error)``. The create task is quick; the disk is
    attached with a follow-up config write, which is why create alone is not
    enough (a create-time disk reference to an existing volume is dropped).
    """
    client = _client()
    if params.get("bios") == "ovmf":
        params.setdefault("efidisk_storage", _storage_from_volid(volid))
    bus_key = _bus_key(params.get("disk_bus", "sata"))
    if params.get("disk_bus") == "scsi":
        params.setdefault("scsihw", "virtio-scsi-single")

    body = _base_body(params)
    try:
        upid = client.post(f"nodes/{quote(node, safe='')}/qemu", data=body)
    except ProxmoxAPIError as exc:
        return "", str(exc)

    exit_status = _poll_task(client, node, upid, timeout=120)
    if exit_status not in ("OK", ""):
        return upid, f"VM shell creation failed: {exit_status}"

    try:
        client.put(
            f"nodes/{quote(node, safe='')}/qemu/{params['vmid']}/config",
            data={bus_key: volid, "boot": f"order={bus_key}"},
        )
    except ProxmoxAPIError as exc:
        return upid, f"VM created but attaching the disk failed: {exc}"
    return upid, None


def _storage_from_volid(volid: str) -> str:
    return volid.split(":", 1)[0] if ":" in volid else ""


# --------------------------------------------------------------------------- #
# Import: stage a loose image as a volume, then import-from it.
# --------------------------------------------------------------------------- #
def _detect_image_format(path: Path) -> str:
    """Return the on-disk image format (qcow2/raw/vmdk) via qemu-img."""
    try:
        result = subprocess.run(
            ["qemu-img", "info", "--output=json", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        fmt = str(json.loads(result.stdout).get("format", "")).lower()
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise VmRegisterError(f"Could not detect the image format: {exc}") from exc
    if fmt not in {"qcow2", "raw", "vmdk"}:
        raise VmRegisterError(f"Unsupported image format: {fmt or 'unknown'}")
    return fmt


def _stage_source(storage: StorageMount, relative_path: str) -> tuple[str, Path]:
    """Hardlink ``relative_path`` into ``images/<tmpid>/`` on the same mount.

    Returns ``(staging_volid, staging_dir)``. The hardlink is instant and uses
    no extra space (same filesystem); it must live until the import task ends.
    """
    root = Path(storage.path)
    source = (root / relative_path).resolve()
    if not source.is_file():
        raise VmRegisterError("Source image is not available on the mount.")
    if root not in source.parents:
        raise VmRegisterError("Source image is outside the storage mount.")

    ext = source.suffix.lstrip(".").lower()
    if ext not in {"qcow2", "raw", "vmdk"}:
        # .img and other/blank extensions: detect the real on-disk format so the
        # staged volume gets a name Proxmox recognises (its content, unchanged).
        ext = _detect_image_format(source)

    for _ in range(10):
        tmpid = secrets.randbelow(9000) + 90000  # 90000-98999, well clear of real VMIDs
        staging_dir = root / "images" / str(tmpid)
        if not staging_dir.exists():
            break
    else:
        raise VmRegisterError("Could not allocate a staging id.")

    staging_dir.mkdir(parents=True, exist_ok=False)
    target = staging_dir / f"vm-{tmpid}-disk-0.{ext}"
    try:
        os.link(source, target)
    except OSError as exc:
        _remove_stage(staging_dir)
        raise VmRegisterError(f"Could not stage the source image: {exc}") from exc
    volid = f"{storage.storage_id}:{tmpid}/{target.name}"
    return volid, staging_dir


def _remove_stage(staging_dir: Path) -> None:
    try:
        for child in staging_dir.iterdir():
            child.unlink(missing_ok=True)
        staging_dir.rmdir()
    except OSError:
        pass


def import_disk_as_vm(
    node: str,
    params: dict[str, Any],
    *,
    source_storage: StorageMount,
    source_path: str,
) -> tuple[str, str | None]:
    """Stage the source image, create a VM importing it, then clean up.

    Intended to run in the worker (the import task can take minutes). The
    staging hardlink is removed only after the import task finishes.
    """
    client = _client()
    bus_key = _bus_key(params.get("disk_bus", "sata"))
    if params.get("disk_bus") == "scsi":
        params.setdefault("scsihw", "virtio-scsi-single")
    target_storage = params["target_storage"]
    fmt = params.get("format", "qcow2")

    staging_volid, staging_dir = _stage_source(source_storage, source_path)
    try:
        body = _base_body(params)
        body[bus_key] = f"{target_storage}:0,import-from={staging_volid},format={fmt}"
        body["boot"] = f"order={bus_key}"
        if params.get("start"):
            body["start"] = 1
        try:
            upid = client.post(f"nodes/{quote(node, safe='')}/qemu", data=body)
        except ProxmoxAPIError as exc:
            return "", str(exc)
        exit_status = _poll_task(client, node, upid)
        if exit_status not in ("OK", ""):
            return upid, f"Import failed: {exit_status}"
        return upid, None
    finally:
        _remove_stage(staging_dir)

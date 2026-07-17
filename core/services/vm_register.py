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
from typing import Any, Callable
from urllib.parse import quote

from core.models import StorageMount
from core.services.ovf_import import OvfImportError, OvfPackage, package_disk_volids, parse_ovf_package
from core.services.proxmox import ProxmoxAPIError

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
    from core.services.cluster_resolver import (
        ClusterResolutionError,
        pin_cluster_write_client,
        require_sole_enabled_cluster_for_legacy_caller,
    )

    try:
        cluster = require_sole_enabled_cluster_for_legacy_caller()
        _endpoint, client = pin_cluster_write_client(cluster)
    except ClusterResolutionError as exc:
        raise VmRegisterError(str(exc)) from exc
    return client


def vmid_from_volid(volid: str) -> int | None:
    """Parse the owning VMID out of a disk volid (``storage:9999/vm-9999-disk-0``)."""
    match = _VOLID_VMID_RE.search(volid or "")
    return int(match.group(1)) if match else None


def _bus_key(bus: str) -> str:
    if bus not in {b for b, _ in DISK_BUSES}:
        raise VmRegisterError(f"Unsupported disk bus: {bus}")
    return f"{bus}0"


def _disk_key(bus: str, index: int) -> str:
    if bus not in {item for item, _label in DISK_BUSES} or index < 0:
        raise VmRegisterError(f"Unsupported disk bus: {bus}")
    if bus == "ide" and index > 1:
        raise VmRegisterError("IDE supports at most two imported disks; use SATA or SCSI for this package.")
    return f"{bus}{index}"


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


def _create_vm_importing(node: str, params: dict[str, Any], import_volid: str) -> tuple[str, str | None]:
    """Create a VM whose boot disk is imported from ``import_volid`` (a volid)."""
    client = _client()
    bus_key = _bus_key(params.get("disk_bus", "sata"))
    if params.get("disk_bus") == "scsi":
        params.setdefault("scsihw", "virtio-scsi-single")
    body = _base_body(params)
    body[bus_key] = (
        f"{params['target_storage']}:0,import-from={import_volid},format={params.get('format', 'qcow2')}"
    )
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


def import_disk_as_vm(
    node: str,
    params: dict[str, Any],
    *,
    source_storage: StorageMount,
    source_path: str,
) -> tuple[str, str | None]:
    """Stage a browsable image as a volume, import it into a new VM, then clean up.

    Intended to run in the worker (the import can take minutes); the staging
    hardlink is removed only after the import task finishes.
    """
    staging_volid, staging_dir = _stage_source(source_storage, source_path)
    try:
        return _create_vm_importing(node, params, staging_volid)
    finally:
        _remove_stage(staging_dir)


def import_volid_as_vm(node: str, params: dict[str, Any], *, source_volid: str) -> tuple[str, str | None]:
    """Import an already-catalogued volume (e.g. a local ``import``-content image)
    into a new VM. No staging needed — the volid is passed straight to import-from."""
    return _create_vm_importing(node, params, source_volid)


# --------------------------------------------------------------------------- #
# OVA / OVF import: parser metadata plus one import-from action per disk.
# --------------------------------------------------------------------------- #
def _stage_ovf_package_sources(storage: StorageMount, source_path: str, package: OvfPackage) -> tuple[list[str], list[Path]]:
    """Return Proxmox import volids, hard-linking non-import sources temporarily.

    A source already under ``import/`` is directly usable by Proxmox.  Browser
    files elsewhere on the same mounted directory storage are linked into
    ``import/`` for the duration of the worker task so users need not shuffle a
    large OVA merely to begin an import.
    """
    direct = package_disk_volids(package)
    if package.source_path.startswith("import/"):
        return direct, []

    root = Path(storage.path).resolve()
    source = (root / package.source_path).resolve()
    if root not in source.parents or not source.is_file():
        raise VmRegisterError("Package is not available on the source storage.")
    import_dir = root / "import"
    try:
        import_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise VmRegisterError(f"Could not access the storage import directory: {exc}") from exc

    token = secrets.token_hex(8)
    staged: list[Path] = []
    try:
        if package.kind == "ova":
            staged_archive = import_dir / f"pve-helper-{token}.ova"
            os.link(source, staged_archive)
            staged.append(staged_archive)
            archive_rel = f"import/{staged_archive.name}"
            return [f"{storage.storage_id}:{archive_rel}/{disk.href}" for disk in package.disks], staged

        volids: list[str] = []
        source_root = source.parent.resolve()
        for index, disk in enumerate(package.disks):
            source_disk = (source_root / disk.href).resolve()
            if source_root not in source_disk.parents or not source_disk.is_file():
                raise VmRegisterError(f"OVF disk {disk.href} is not available beside the descriptor.")
            suffix = source_disk.suffix.lower() or ".vmdk"
            staged_disk = import_dir / f"pve-helper-{token}-{index}{suffix}"
            os.link(source_disk, staged_disk)
            staged.append(staged_disk)
            volids.append(f"{storage.storage_id}:import/{staged_disk.name}")
        return volids, staged
    except OSError as exc:
        _remove_staged_files(staged)
        raise VmRegisterError(f"Could not stage OVA/OVF package: {exc}") from exc


def _remove_staged_files(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _wait_for_upid(client, node: str, upid: object, *, timeout: int = 3600) -> str | None:
    if not isinstance(upid, str) or not upid.startswith("UPID:"):
        return None
    result = client.wait_for_task(node=node, upid=upid, timeout_seconds=timeout)
    if not result.success:
        return result.exitstatus or result.status or "unknown error"
    return None


def import_ovf_package_as_vm(
    node: str,
    params: dict[str, Any],
    *,
    source_storage: StorageMount,
    source_path: str,
    progress: Callable[[str, int, int], None] | None = None,
) -> tuple[list[str], str | None]:
    """Create a VM from every disk described by an OVF/OVA package.

    The first disk is imported by the VM-create request and establishes the boot
    order.  Remaining disks are attached sequentially with config writes.  On
    failure the stopped partial VM remains intentionally, with all completed
    UPIDs returned to the caller for an exact audit trail.
    """
    try:
        package = parse_ovf_package(source_storage, source_path, validate_manifest=True)
    except OvfImportError as exc:
        return [], str(exc)
    if not package.disks:
        return [], "OVF package does not contain an importable disk."
    bus = str(params.get("disk_bus") or "sata")
    try:
        disk_keys = [_disk_key(bus, index) for index in range(len(package.disks))]
    except VmRegisterError as exc:
        return [], str(exc)
    try:
        source_volids, staged_paths = _stage_ovf_package_sources(source_storage, source_path, package)
    except VmRegisterError as exc:
        return [], str(exc)

    client = _client()
    upids: list[str] = []
    try:
        if bus == "scsi":
            params.setdefault("scsihw", "virtio-scsi-single")
        body = _base_body(params)
        body[disk_keys[0]] = f"{params['target_storage']}:0,import-from={source_volids[0]},format={params.get('format', 'qcow2')}"
        body["boot"] = f"order={disk_keys[0]}"
        if progress:
            progress("create and import boot disk", 1, len(source_volids))
        first_upid = client.post(f"nodes/{quote(node, safe='')}/qemu", data=body)
        if isinstance(first_upid, str):
            upids.append(first_upid)
        error = _wait_for_upid(client, node, first_upid)
        if error:
            return upids, f"Boot-disk import failed: {error}"

        for index, source_volid in enumerate(source_volids[1:], start=1):
            if progress:
                progress(f"import disk {index + 1} of {len(source_volids)}", index + 1, len(source_volids))
            upid = client.put(
                f"nodes/{quote(node, safe='')}/qemu/{params['vmid']}/config",
                data={disk_keys[index]: f"{params['target_storage']}:0,import-from={source_volid},format={params.get('format', 'qcow2')}"},
            )
            if isinstance(upid, str):
                upids.append(upid)
            error = _wait_for_upid(client, node, upid)
            if error:
                return upids, f"Disk {index + 1} import failed: {error}"

        if params.get("start"):
            if progress:
                progress("start imported VM", len(source_volids), len(source_volids))
            upid = client.post(f"nodes/{quote(node, safe='')}/qemu/{params['vmid']}/status/start", data={})
            if isinstance(upid, str):
                upids.append(upid)
            error = _wait_for_upid(client, node, upid, timeout=300)
            if error:
                return upids, f"VM imported but could not start: {error}"
        return upids, None
    except ProxmoxAPIError as exc:
        return upids, str(exc)
    finally:
        _remove_staged_files(staged_paths)

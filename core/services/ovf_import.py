"""Read and validate OVF/OVA metadata without extracting package contents.

The Proxmox ``import-metadata`` endpoint does not support directory-backed NFS
volumes reliably.  This module deliberately does only package inspection; the
actual VMDK conversion still happens through Proxmox ``import-from``.
"""

from __future__ import annotations

import hashlib
import re
import tarfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from core.models import StorageMount


MAX_OVF_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 256
MAX_OVF_DISKS = 32
MAX_MANIFEST_MEMBERS = 256
MAX_DECLARED_DISK_BYTES = 16 * 1024**4
_MANIFEST_LINE = re.compile(r"^([A-Za-z0-9-]+)\((.+)\)=\s*([0-9A-Fa-f]+)\s*$")


class OvfImportError(Exception):
    """The source package is malformed, unsafe, or cannot be imported."""


@dataclass(frozen=True)
class OvfDisk:
    disk_id: str
    href: str
    capacity_bytes: int | None


@dataclass(frozen=True)
class OvfNic:
    network_name: str
    model: str


@dataclass(frozen=True)
class OvfPackage:
    storage_id: str
    source_path: str
    kind: str
    name: str
    cores: int | None
    memory_mib: int | None
    ostype: str
    disks: tuple[OvfDisk, ...]
    nics: tuple[OvfNic, ...]
    manifest_present: bool


def parse_ovf_package(storage: StorageMount, source_path: str, *, validate_manifest: bool = False) -> OvfPackage:
    """Return safe, reviewable OVF/OVA metadata for a mounted import package.

    A manifest is checksum-validated only by the worker, immediately before it
    creates a guest.  The GET path merely parses it so opening a wizard never
    reads a multi-gigabyte VMDK.
    """
    path = _source_path(storage, source_path)
    suffix = path.suffix.lower()
    if suffix not in {".ovf", ".ova"}:
        raise OvfImportError("Select an .ova or .ovf package.")
    if suffix == ".ova":
        metadata, members, manifest = _read_ova(path, validate_manifest=validate_manifest)
        kind = "ova"
    else:
        metadata, manifest = _read_ovf(path)
        kind = "ovf"
    package = _parse_ovf_xml(metadata)
    if kind == "ovf":
        members = _directory_disk_members(path.parent.resolve(), package.disks)
        if validate_manifest and manifest is not None:
            _validate_directory_manifest(path.parent.resolve(), manifest)
    _validate_disks(package.disks, members)
    package_name = package.name
    if package_name == "imported-vm":
        package_name = _safe_name(path.stem)
    return OvfPackage(
        storage_id=storage.storage_id,
        source_path=_normalized_relative_path(source_path),
        kind=kind,
        name=package_name,
        cores=package.cores,
        memory_mib=package.memory_mib,
        ostype=package.ostype,
        disks=package.disks,
        nics=package.nics,
        manifest_present=manifest is not None,
    )


def package_disk_volids(package: OvfPackage) -> list[str]:
    """Build Proxmox catalogued ``import-from`` volume IDs for each disk."""
    root = PurePosixPath(package.source_path)
    base = root if package.kind == "ova" else root.parent
    return [f"{package.storage_id}:{base.as_posix()}/{disk.href}" for disk in package.disks]


def _normalized_relative_path(raw: str) -> str:
    value = str(raw or "").replace("\\", "/").strip("/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise OvfImportError("Package path is invalid.")
    return path.as_posix()


def _source_path(storage: StorageMount, source_path: str) -> Path:
    root = Path(storage.path).resolve()
    source = (root / _normalized_relative_path(source_path)).resolve()
    if root not in source.parents or not source.is_file():
        raise OvfImportError("Package is not available on the selected storage mount.")
    return source


def _safe_member_name(raw: str) -> str:
    value = str(raw or "").replace("\\", "/").strip("/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise OvfImportError("Package contains an unsafe member path.")
    return path.as_posix()


def _read_limited(stream: BinaryIO, size: int) -> bytes:
    if size > MAX_OVF_BYTES:
        raise OvfImportError("OVF metadata is larger than the supported limit.")
    data = stream.read(MAX_OVF_BYTES + 1)
    if len(data) > MAX_OVF_BYTES:
        raise OvfImportError("OVF metadata is larger than the supported limit.")
    return data


def _read_ova(path: Path, *, validate_manifest: bool) -> tuple[bytes, set[str], str | None]:
    try:
        with tarfile.open(path, "r:*") as archive:
            members = archive.getmembers()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise OvfImportError("OVA contains too many archive members.")
            names: set[str] = set()
            for member in members:
                if member.issym() or member.islnk():
                    raise OvfImportError("OVA may not contain symbolic or hard links.")
                safe_name = _safe_member_name(member.name)
                if member.isdir():
                    continue
                if not member.isfile():
                    raise OvfImportError("OVA may only contain regular files and directories.")
                names.add(safe_name)
            ovfs = [member for member in members if member.name.lower().endswith(".ovf")]
            if len(ovfs) != 1:
                raise OvfImportError("OVA must contain exactly one OVF descriptor.")
            handle = archive.extractfile(ovfs[0])
            if handle is None:
                raise OvfImportError("Could not read the OVF descriptor from the OVA.")
            metadata = _read_limited(handle, ovfs[0].size)
            manifest = _archive_manifest(archive, members)
            if validate_manifest and manifest is not None:
                _validate_archive_manifest(archive, members, manifest)
            return metadata, names, manifest
    except (tarfile.TarError, OSError) as exc:
        raise OvfImportError(f"Could not read OVA archive: {exc}") from exc


def _read_ovf(path: Path) -> tuple[bytes, str | None]:
    try:
        with path.open("rb") as handle:
            metadata = _read_limited(handle, path.stat().st_size)
    except OSError as exc:
        raise OvfImportError(f"Could not read OVF descriptor: {exc}") from exc
    manifest_path = path.with_suffix(".mf")
    manifest = _read_directory_manifest(manifest_path) if manifest_path.is_file() else None
    return metadata, manifest


def _read_directory_manifest(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            return _read_limited(handle, path.stat().st_size).decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        raise OvfImportError(f"Could not read OVF manifest: {exc}") from exc


def _directory_disk_members(root: Path, disks: tuple[OvfDisk, ...]) -> set[str]:
    """Check only disks named by the descriptor; never walk the datastore."""
    members: set[str] = set()
    for disk in disks:
        candidate = (root / disk.href).resolve()
        if root not in candidate.parents or not candidate.is_file() or candidate.is_symlink():
            continue
        members.add(disk.href)
    return members


def _archive_manifest(archive: tarfile.TarFile, members: list[tarfile.TarInfo]) -> str | None:
    entries = [member for member in members if member.name.lower().endswith(".mf")]
    if not entries:
        return None
    if len(entries) != 1:
        raise OvfImportError("OVA contains more than one manifest.")
    handle = archive.extractfile(entries[0])
    if handle is None or entries[0].size > MAX_OVF_BYTES:
        raise OvfImportError("Could not read OVA manifest.")
    return handle.read().decode("utf-8", errors="strict")


def _manifest_entries(manifest: str) -> list[tuple[str, str, str]]:
    result: list[tuple[str, str, str]] = []
    for raw_line in manifest.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _MANIFEST_LINE.match(line)
        if not match:
            raise OvfImportError("OVF manifest contains an invalid checksum entry.")
        algorithm, name, expected = match.groups()
        if algorithm.lower() not in hashlib.algorithms_available:
            raise OvfImportError(f"OVF manifest uses unsupported checksum {algorithm}.")
        result.append((algorithm.lower(), _safe_member_name(name), expected.lower()))
        if len(result) > MAX_MANIFEST_MEMBERS:
            raise OvfImportError("OVF manifest contains too many checksum entries.")
    return result


def _hash_stream(stream: BinaryIO, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest().lower()


def _validate_archive_manifest(archive: tarfile.TarFile, members: list[tarfile.TarInfo], manifest: str) -> None:
    by_name = {_safe_member_name(member.name): member for member in members}
    for algorithm, name, expected in _manifest_entries(manifest):
        member = by_name.get(name)
        handle = archive.extractfile(member) if member else None
        if handle is None or _hash_stream(handle, algorithm) != expected:
            raise OvfImportError(f"OVA manifest checksum failed for {name}.")


def _validate_directory_manifest(root: Path, manifest: str) -> None:
    for algorithm, name, expected in _manifest_entries(manifest):
        candidate = (root / name).resolve()
        if root not in candidate.parents or not candidate.is_file() or candidate.is_symlink():
            raise OvfImportError(f"OVF manifest references unavailable file {name}.")
        with candidate.open("rb") as handle:
            if _hash_stream(handle, algorithm) != expected:
                raise OvfImportError(f"OVF manifest checksum failed for {name}.")


def _tag(element: ET.Element, name: str) -> str | None:
    for child in element:
        if child.tag.rsplit("}", 1)[-1] == name:
            return (child.text or "").strip()
    return None


def _attribute(element: ET.Element, name: str) -> str:
    for key, value in element.attrib.items():
        if key.rsplit("}", 1)[-1] == name:
            return str(value)
    return ""


def _parse_ovf_xml(data: bytes) -> OvfPackage:
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise OvfImportError("OVF external entities are not allowed.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise OvfImportError(f"OVF XML is invalid: {exc}") from exc

    refs = {_attribute(element, "id"): _safe_member_name(_attribute(element, "href")) for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "File"}
    disks_by_id: dict[str, OvfDisk] = {}
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "Disk":
            continue
        disk_id = _attribute(element, "diskId")
        file_ref = _attribute(element, "fileRef")
        href = refs.get(file_ref, "")
        if not disk_id or not href:
            raise OvfImportError("OVF disk is missing its file reference.")
        capacity = _capacity_bytes(_attribute(element, "capacity"), _attribute(element, "capacityAllocationUnits") or _attribute(element, "allocationUnits"))
        disks_by_id[disk_id] = OvfDisk(disk_id=disk_id, href=href, capacity_bytes=capacity)

    name = "imported-vm"
    cores: int | None = None
    memory_mib: int | None = None
    nics: list[OvfNic] = []
    disk_order: list[str] = []
    for element in root.iter():
        local = element.tag.rsplit("}", 1)[-1]
        if local == "VirtualSystem":
            for child in element:
                if child.tag.rsplit("}", 1)[-1] == "Name" and (child.text or "").strip():
                    name = (child.text or "").strip()
                    break
        if local != "Item":
            continue
        resource_type = _tag(element, "ResourceType")
        quantity = _tag(element, "VirtualQuantity")
        if resource_type == "3" and quantity and quantity.isdigit():
            cores = max(1, int(quantity))
        elif resource_type == "4" and quantity:
            memory_mib = _memory_mib(quantity, _tag(element, "AllocationUnits"))
        elif resource_type == "10":
            nics.append(OvfNic(network_name=_tag(element, "Connection") or "", model=_nic_model(_tag(element, "ResourceSubType"))))
        elif resource_type == "17":
            host_resource = _tag(element, "HostResource") or ""
            disk_id = host_resource.rsplit("/", 1)[-1]
            if disk_id:
                disk_order.append(disk_id)

    ordered_disks = [disks_by_id.pop(disk_id) for disk_id in disk_order if disk_id in disks_by_id]
    ordered_disks.extend(disks_by_id.values())
    if not ordered_disks:
        raise OvfImportError("OVF does not reference any importable disks.")
    return OvfPackage(
        storage_id="",
        source_path="",
        kind="",
        name=_safe_name(name),
        cores=cores,
        memory_mib=memory_mib,
        ostype=_ovf_ostype(root),
        disks=tuple(ordered_disks),
        nics=tuple(nics),
        manifest_present=False,
    )


def _capacity_bytes(value: str, units: str) -> int | None:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    multiplier = _unit_multiplier(units) or 1
    result = number * multiplier
    if result <= 0 or result > MAX_DECLARED_DISK_BYTES:
        raise OvfImportError("OVF disk capacity is outside the supported range.")
    return result


def _memory_mib(value: str, units: str | None) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    multiplier = _unit_multiplier(units or "") or 1
    return max(16, int((number * multiplier) / 1024**2))


def _unit_multiplier(units: str) -> int | None:
    text = str(units or "").lower().replace(" ", "")
    if not text:
        return None
    if text in {"byte", "bytes"}:
        return 1
    match = re.fullmatch(r"(?:byte|bytes)\*2\^(\d+)", text)
    if match:
        return 2 ** int(match.group(1))
    match = re.fullmatch(r"(?:byte|bytes)\*10\^(\d+)", text)
    if match:
        return 10 ** int(match.group(1))
    return {"kb": 1000, "mb": 1000**2, "gb": 1000**3, "kib": 1024, "mib": 1024**2, "gib": 1024**3}.get(text)


def _nic_model(raw: str | None) -> str:
    text = str(raw or "").lower()
    if "vmxnet3" in text:
        return "vmxnet3"
    if "rtl8139" in text:
        return "rtl8139"
    if "virtio" in text:
        return "virtio"
    return "e1000"


def _ovf_ostype(root: ET.Element) -> str:
    values = " ".join(str(value).lower() for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "OperatingSystemSection" for value in element.attrib.values())
    if "windows" in values or "win" in values:
        if any(value in values for value in ("2022", "2025", "11")):
            return "win11"
        if any(value in values for value in ("2016", "2019", "10")):
            return "win10"
        if "2008" in values:
            return "w2k8"
        return "win8"
    if "solaris" in values:
        return "solaris"
    if any(value in values for value in ("freebsd", "openbsd", "netbsd", "other")):
        return "other"
    return "l26"


def _safe_name(value: str) -> str:
    name = "".join(character if (character.isalnum() or character in "-.") else "-" for character in value).strip("-.")
    return name[:63] or "imported-vm"


def _validate_disks(disks: tuple[OvfDisk, ...], members: set[str]) -> None:
    if len(disks) > MAX_OVF_DISKS:
        raise OvfImportError("OVF declares too many disks.")
    for disk in disks:
        if disk.href not in members:
            raise OvfImportError(f"OVF references missing disk {disk.href}.")

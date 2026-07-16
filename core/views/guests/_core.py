from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path, PurePosixPath

from ..common import *  # noqa: F401,F403
from .. import common
from core.models import StorageMount
from core.services.public_errors import public_exception_message
from core.services.current_guest_inventory import (
    delete_current_guest,
    update_current_guest_config,
)
from .operation_lifecycle import (
    _audit_guest,
    _guest_delete_wait_task,
    _guest_put,
)
from .read_model_support import _guest_api_get, _is_disk_device_key


SNAPSHOT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
SNAPSHOT_NAME_HELP = "Snapshot names must start with a letter and can then contain letters, digits, _ and -."


def _guest_snapshot_entries(detail: SimpleNamespace) -> tuple[list[dict], str]:
    data, error = _guest_api_get(detail, "snapshot")
    entries = []
    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            snaptime = entry.get("snaptime")
            entries.append(
                {
                    "name": entry.get("name", ""),
                    "description": entry.get("description", ""),
                    "parent": entry.get("parent", "") or "",
                    "snaptime": datetime.fromtimestamp(int(snaptime), dt_timezone.utc) if snaptime else None,
                    "vmstate": bool(entry.get("vmstate")),
                    "is_current": entry.get("name") == "current",
                }
            )
    return entries, error or ""


def _ordered_snapshot_entries(entries: list[dict]) -> list[dict]:
    # Build the snapshot tree from the parent links and flatten it depth-first.
    by_name = {item["name"]: item for item in entries}
    children: dict[str, list] = {}
    roots = []
    for item in entries:
        parent = item["parent"]
        if parent and parent in by_name:
            children.setdefault(parent, []).append(item)
        else:
            roots.append(item)

    def _sort_key(item):
        if item["is_current"]:
            return datetime.max.replace(tzinfo=dt_timezone.utc)
        return item["snaptime"] or datetime.min.replace(tzinfo=dt_timezone.utc)

    ordered = []

    def _walk(node, depth):
        ordered.append({**node, "depth": depth, "indent": depth * 22})
        for child in sorted(children.get(node["name"], []), key=_sort_key):
            _walk(child, depth + 1)

    for root in sorted(roots, key=_sort_key):
        _walk(root, 0)
    return ordered


def _delete_all_guest_snapshots(detail: SimpleNamespace) -> tuple[int, str]:
    entries, error = _guest_snapshot_entries(detail)
    if error:
        return 0, error
    snapshots = [snap for snap in _ordered_snapshot_entries(entries) if not snap["is_current"] and snap.get("name")]
    deleted = 0
    for snap in reversed(snapshots):
        _data, err = _guest_delete_wait_task(detail, f"snapshot/{quote(snap['name'], safe='')}")
        if err:
            return deleted, err
        deleted += 1
    return deleted, ""


def _storage_supports_content(storage: dict, content_type: str) -> bool:
    return content_type in {value.strip() for value in str(storage.get("content", "")).split(",") if value.strip()}


def _guest_backup_archives(detail: SimpleNamespace) -> tuple[list[dict], list[dict], str]:
    """Return backup-capable storage and archive records from the endpoint that
    owns this guest.  Storage is node-scoped in PVE, so never assume client 0.
    """
    if not detail.node:
        return [], [], "The guest's node could not be resolved."
    error = ""
    for client in common.configured_clients():
        try:
            # A cheap live request also proves this configured endpoint owns the
            # guest instead of accepting a same-named node on another endpoint.
            client.guest_current(node=detail.node, object_type=detail.object_type, vmid=detail.vmid)
            storages = client.get(f"nodes/{quote(detail.node, safe='')}/storage")
        except ProxmoxAPIError as exc:
            error = public_exception_message(
                exc,
                operation="guest_backup_inventory",
                fallback="Proxmox backup data is temporarily unavailable.",
            )
            continue
        backup_storages = [
            {"id": str(storage.get("storage") or ""), "label": str(storage.get("storage") or "")}
            for storage in (storages if isinstance(storages, list) else [])
            if storage.get("storage") and _storage_supports_content(storage, "backup") and storage.get("active", 1)
        ]
        backups: list[dict] = []
        for storage in backup_storages:
            try:
                content = client.get(
                    f"nodes/{quote(detail.node, safe='')}/storage/{quote(storage['id'], safe='')}/content?content=backup&vmid={detail.vmid}"
                )
            except ProxmoxAPIError:
                continue
            for entry in content if isinstance(content, list) else []:
                volid = str(entry.get("volid") or "")
                if not volid:
                    continue
                backups.append(
                    {
                        "volid": volid,
                        "size": entry.get("size"),
                        "ctime": datetime.fromtimestamp(int(entry["ctime"]), dt_timezone.utc) if entry.get("ctime") else None,
                        "notes": entry.get("notes", ""),
                        "storage": storage["id"],
                        "source_endpoint": str(getattr(client, "endpoint", "")),
                        "source_type": detail.object_type,
                        "source_vmid": detail.vmid,
                        "source_node": detail.node,
                    }
                )
        backups.sort(key=lambda item: item["ctime"] or datetime.min.replace(tzinfo=dt_timezone.utc), reverse=True)
        return backups, backup_storages, ""
    return [], [], error or "No Proxmox endpoint could read this guest's backup storage."


def _guest_backup_storages(detail: SimpleNamespace) -> tuple[list[dict], str]:
    """Return backup-capable storage without enumerating every archive."""
    if not detail.node:
        return [], "The guest's node could not be resolved."
    error = ""
    for client in common.configured_clients():
        try:
            client.guest_current(node=detail.node, object_type=detail.object_type, vmid=detail.vmid)
            storages = client.get(f"nodes/{quote(detail.node, safe='')}/storage")
        except ProxmoxAPIError as exc:
            error = public_exception_message(
                exc,
                operation="guest_backup_storage_options",
                fallback="Proxmox backup storage data is temporarily unavailable.",
            )
            continue
        return (
            [
                {"id": str(storage.get("storage") or ""), "label": str(storage.get("storage") or "")}
                for storage in (storages if isinstance(storages, list) else [])
                if storage.get("storage")
                and _storage_supports_content(storage, "backup")
                and storage.get("active", 1)
            ],
            "",
        )
    return [], error or "No Proxmox endpoint could read this guest's backup storage."


def _backup_job_covers(job: dict, vmid: int) -> bool:
    if str(job.get("all", "0")) in ("1", "True", "true"):
        return True
    vmids = str(job.get("vmid", ""))
    return str(vmid) in [v.strip() for v in vmids.split(",") if v.strip()]


def _backup_error(err: str) -> str:
    if "403" in err:
        return proxmox_permission_hint("VM.Backup")
    return f"Backup failed: {err}"


def _submit_guest_backup(request, detail: SimpleNamespace):
    storage = request.POST.get("storage", "").strip()
    mode = request.POST.get("mode", "snapshot").strip()
    compress = request.POST.get("compress", "zstd").strip()
    if not storage:
        return None, "Select a backup storage.", None, {}
    if mode not in {"snapshot", "suspend", "stop"}:
        return None, "Choose a valid backup mode.", None, {"storage": storage}
    if compress not in {"zstd", "gzip", "lzo", "0"}:
        return None, "Choose a valid compression mode.", None, {"storage": storage}
    if detail.config.get("lock") or detail.current.get("lock"):
        return None, f"This guest is locked ({detail.config.get('lock') or detail.current.get('lock')}).", None, {"storage": storage}

    body: dict[str, object] = {
        "vmid": detail.vmid,
        "storage": storage,
        "mode": mode,
        "compress": compress,
        "remove": 0,
        "protected": 1 if request.POST.get("protected") in {"1", "on", "true"} else 0,
    }
    notification_mode = request.POST.get("notification_mode", "auto").strip()
    if notification_mode not in {"auto", "legacy-sendmail", "notification-system"}:
        return None, "Choose a valid notification mode.", None, {"storage": storage}
    body["notification-mode"] = notification_mode
    notes_template = request.POST.get("notes_template", "").strip()
    if notes_template:
        body["notes-template"] = notes_template
    audit_details = {
        "storage": storage,
        "mode": mode,
        "compression": compress or "none",
        "protected": bool(body["protected"]),
        "notification_mode": notification_mode,
        "notes_template": notes_template,
    }

    if not detail.node:
        return None, "The guest's node could not be resolved.", None, audit_details
    last_error = "No Proxmox endpoint could reach this guest."
    for client in common.configured_clients():
        try:
            # Resolve storage through the endpoint that currently owns the guest.
            client.guest_current(node=detail.node, object_type=detail.object_type, vmid=detail.vmid)
            storages = client.get(f"nodes/{quote(detail.node, safe='')}/storage")
            match = next(
                (
                    item
                    for item in (storages if isinstance(storages, list) else [])
                    if str(item.get("storage") or "") == storage
                ),
                None,
            )
            if not match or not match.get("active", 1) or not _storage_supports_content(match, "backup"):
                return None, f"Storage '{storage}' is not an active backup storage on {detail.node}.", client, audit_details
            return client.post(f"nodes/{quote(detail.node, safe='')}/vzdump", data=body), None, client, audit_details
        except ProxmoxAPIError as exc:
            last_error = public_exception_message(
                exc,
                operation="guest_backup_submit",
                fallback="Proxmox could not start the backup.",
            )
    return None, last_error, None, audit_details


MIGRATE_KINDS = {"host", "storage", "both"}
# States where a VM/CT is not fully stopped, so a host migration must go online
# (VM live migration) or restart (LXC) rather than plain offline.
_MIGRATE_ACTIVE_STATES = {"running", "paused", "hibernated"}


def _guest_movable_disks(detail: SimpleNamespace) -> list[dict]:
    """Owned disks/volumes that a storage-only migration can relocate."""
    config = detail.config if isinstance(detail.config, dict) else {}
    disks: list[dict] = []
    for key in sorted(config):
        value = config[key]
        if not isinstance(value, str):
            continue
        if detail.object_type == ProxmoxInventory.ObjectType.VM:
            # data/system disks + the EFI vars and TPM state volumes (all move via
            # move_disk); skip CD-ROM/cloudinit-style entries.
            if not (_is_disk_device_key(key) or key in ("efidisk0", "tpmstate0")) or "media=cdrom" in value:
                continue
        elif key != "rootfs" and not re.match(r"^mp\d+$", key):
            continue
        storage = value.split(":", 1)[0] if ":" in value else ""
        # A volume with no storage prefix (e.g. an ISO path) can't be moved.
        if not storage:
            continue
        disks.append({"key": key, "storage": storage, "label": f"{key} ({storage})" if storage else key})
    return disks


def _apply_migrate_net_remap(request, detail: SimpleNamespace) -> tuple[str, dict]:
    """Rewrite selected NICs' bridge before a host migration.

    Reads ``migrate_net_remap`` (JSON ``{"net0": "vmbr0", ...}``) and PUTs the
    changed netX lines. Returns ``(error, {net: bridge} applied)``.
    """
    raw = request.POST.get("migrate_net_remap", "").strip()
    if not raw:
        return "", {}
    try:
        remap = json.loads(raw)
    except ValueError:
        return "Invalid network remap request.", {}
    if not isinstance(remap, dict) or not remap:
        return "", {}
    config = detail.config if isinstance(detail.config, dict) else {}
    applied: dict[str, str] = {}
    for net_key in sorted(remap):
        new_bridge = str(remap[net_key] or "").strip()
        if not re.match(r"^net\d+$", str(net_key)) or not new_bridge:
            continue
        current = config.get(net_key)
        if not isinstance(current, str) or "bridge=" not in current:
            continue
        new_value = re.sub(r"(^|,)bridge=[^,]+", lambda m: f"{m.group(1)}bridge={new_bridge}", current)
        if new_value == current:
            continue
        _response, err = _guest_put(detail, "config", {net_key: new_value})
        if err:
            return f"Could not remap {net_key} to '{new_bridge}': {err}", applied
        applied[net_key] = new_bridge
    return "", applied


def _guest_cpu_model(detail: SimpleNamespace) -> str:
    """The VM's configured CPU model (e.g. ``x86-64-v2-AES``, ``host``), or ``""``
    for the portable default. CT has no CPU model (shares the host kernel)."""
    if detail.object_type != ProxmoxInventory.ObjectType.VM:
        return ""
    config = detail.config if isinstance(detail.config, dict) else {}
    raw = config.get("cpu")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    return raw.split(",", 1)[0].strip()


def _node_cpu_models(client, node: str) -> set[str]:
    try:
        caps = client.get(f"nodes/{quote(node, safe='')}/capabilities/qemu/cpu")
    except ProxmoxAPIError:
        return set()
    if not isinstance(caps, list):
        return set()
    return {str(item.get("name")) for item in caps if isinstance(item, dict) and item.get("name")}


def _node_cpu_signature(client, node: str) -> tuple[str, frozenset[str]] | None:
    """(model name, flag set) for a node's physical CPU — used to decide whether a
    ``cpu=host`` guest can be **live**-migrated between two hosts (only safe when
    the exposed CPU is identical)."""
    try:
        status = client.get(f"nodes/{quote(node, safe='')}/status")
    except ProxmoxAPIError:
        return None
    cpuinfo = status.get("cpuinfo") if isinstance(status, dict) else None
    if not isinstance(cpuinfo, dict):
        return None
    return (str(cpuinfo.get("model") or ""), frozenset(str(cpuinfo.get("flags") or "").split()))


def _guest_nic_bridges(detail: SimpleNamespace) -> list[dict]:
    """The guest's NICs and the bridge each is attached to (netX → bridge=...)."""
    config = detail.config if isinstance(detail.config, dict) else {}
    nics: list[dict] = []
    for key in sorted(config):
        if not re.match(r"^net\d+$", key):
            continue
        value = config[key]
        if not isinstance(value, str):
            continue
        match = re.search(r"(?:^|,)bridge=([^,]+)", value)
        if match:
            nics.append({"key": key, "bridge": match.group(1)})
    return nics


def _node_available_bridges(client, node: str, sdn_vnets: set[str]) -> list[str]:
    """Bridges a NIC can attach to on ``node``: Linux/OVS bridges + realized SDN
    vnets. Proxmox has no per-host port-group concept, so a NIC's bridge name
    must exist on the target node or the guest lands without a network there."""
    try:
        raw = client.get(f"nodes/{quote(node, safe='')}/network")
    except ProxmoxAPIError:
        return []
    if not isinstance(raw, list):
        return []
    bridges: set[str] = set()
    for iface in raw:
        if not isinstance(iface, dict):
            continue
        name = str(iface.get("iface") or "")
        if not name:
            continue
        if str(iface.get("type") or "") in {"bridge", "OVSBridge"} or name in sdn_vnets:
            bridges.add(name)
    return sorted(bridges)


def _migrate_not_allowed_reason(reason: object) -> str:
    if isinstance(reason, dict):
        parts: list[str] = []
        for key, label in (
            ("unavailable_storages", "missing storage"),
            ("unavailable_networks", "missing network"),
            ("local_resources", "local resources"),
        ):
            value = reason.get(key)
            if isinstance(value, list) and value:
                parts.append(f"{label}: " + ", ".join(str(item) for item in value))
        return "; ".join(parts) or "not a valid target"
    if reason:
        return str(reason)
    return "not a valid target"


def _template_storage_paths(disk_references: list[str]) -> tuple[dict[str, set[str]], str]:
    """Return template-disk paths by storage, limited to app-mounted file storage."""
    mounted_storages = {
        storage.storage_id: storage
        for storage in StorageMount.objects.filter(enabled=True).only("storage_id", "path")
    }
    paths: dict[str, set[str]] = {}
    for reference in disk_references:
        storage_id, separator, relative_path = str(reference).partition(":")
        normalized = _normalized_storage_relative_path(relative_path)
        storage = mounted_storages.get(storage_id)
        if not separator or not normalized or storage is None or not Path(storage.path).is_dir():
            return {}, (
                "Template-to-VM conversion currently supports only disk volumes on configured, mounted file storage. "
                f"Unsupported volume: {reference}."
            )
        paths.setdefault(storage_id, set()).add(normalized)
    return paths, ""


def _template_linked_clone_children(client, node: str, storage_paths: dict[str, set[str]]) -> tuple[list[dict], str]:
    children: list[dict] = []
    for storage_id, template_paths in storage_paths.items():
        try:
            content = client.get(
                f"nodes/{quote(node, safe='')}/storage/{quote(storage_id, safe='')}/content"
            )
        except ProxmoxAPIError as exc:
            public_exception_message(
                exc,
                operation="linked_clone_verification",
                fallback="Linked-clone verification failed.",
            )
            return [], f"Could not verify linked clones on storage '{storage_id}'."
        if not isinstance(content, list):
            return [], f"Could not verify linked clones on storage '{storage_id}': unexpected Proxmox response."
        for item in content:
            if not isinstance(item, dict):
                continue
            parent = str(item.get("parent") or "")
            if not parent:
                continue
            child_vmid = item.get("vmid")
            candidates = _linked_parent_candidates(parent, child_vmid)
            if candidates.intersection(template_paths):
                children.append(
                    {
                        "vmid": child_vmid,
                        "volid": str(item.get("volid") or ""),
                        "parent": parent,
                    }
                )
    return children, ""


def _normalized_storage_relative_path(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if ":" in text:
        _storage, _separator, text = text.partition(":")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        return ""
    return posixpath.normpath(str(path))


def _linked_parent_candidates(parent: str, child_vmid: object) -> set[str]:
    """Normalize PVE's relative ``parent`` values against a child VMID.

    The content API reports linked-clone parents as e.g.
    ``../102/base-102-disk-0.qcow2`` for VMID 103.  Some backends return the
    direct relative form instead, so accept both representations.
    """
    raw = str(parent or "").strip()
    if ":" in raw:
        _storage, _separator, raw = raw.partition(":")
    if not raw or raw.startswith("/"):
        return set()
    candidates = {posixpath.normpath(raw)}
    if child_vmid not in {None, ""}:
        candidates.add(posixpath.normpath(posixpath.join(str(child_vmid), raw)))
    return {candidate for candidate in candidates if candidate and not candidate.startswith("../")}


def _split_tag_text(value: str) -> list[str]:
    return _unique_tags(t for t in re.split(r"[;,\s]+", str(value or "").strip()) if t)


def _unique_tags(tags) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        tag = str(tag).strip()
        key = tag.lower()
        if not tag or key in seen:
            continue
        seen.add(key)
        result.append(tag)
    return result


def _update_current_guest_config(detail: SimpleNamespace, updates: dict[str, str], delete: list[str]) -> None:
    update_current_guest_config(
        object_type=detail.object_type,
        vmid=detail.vmid,
        node=detail.node,
        updates=updates,
        delete=delete,
    )


def _delete_current_guest_object(detail: SimpleNamespace) -> None:
    delete_current_guest(
        object_type=detail.object_type,
        vmid=detail.vmid,
    )


def _snapshot_error(err: str) -> str:
    if "403" in err:
        return proxmox_permission_hint("VM.Snapshot (and VM.Snapshot.Rollback for rollback)")
    return f"Snapshot operation failed: {err}"


def _create_guest(request, object_type: str, options: dict):
    post = request.POST
    node = post.get("node", "").strip() or options.get("node", "")
    vmid = post.get("vmid", "").strip()
    if not vmid.isdigit():
        return "VMID must be a whole number."
    disk_storage = post.get("disk_storage", "").strip()
    if not disk_storage:
        return "Select a storage for the disk."
    bridge = post.get("bridge", "").strip()

    common = {
        "vmid": vmid,
        "cores": post.get("cores", "1").strip() or "1",
        "memory": post.get("memory", "512").strip() or "512",
        "disk_storage": disk_storage,
        "disk_size": post.get("disk_size", "8").strip() or "8",
        "bridge": bridge,
        "vlan": post.get("vlan", "").strip(),
        "start": post.get("start") == "on",
    }

    if object_type == ProxmoxInventory.ObjectType.VM:
        name = post.get("name", "").strip()
        if not name:
            return "Name is required."
        params = {
            **common,
            "name": name,
            "ostype": post.get("ostype", "l26").strip() or "l26",
            "sockets": post.get("sockets", "1").strip() or "1",
            "iso": post.get("iso", "").strip(),
        }
        if not bridge:
            params["bridge"] = ""
        _data, err = create_vm(node, params)
    else:
        hostname = post.get("hostname", "").strip()
        if not hostname:
            return "Hostname is required."
        ostemplate = post.get("ostemplate", "").strip()
        if not ostemplate:
            return "Select an OS template."
        password = post.get("password", "")
        ssh_keys = post.get("ssh_keys", "").strip()
        if not password and not ssh_keys:
            return "Set a root password or an SSH public key for the container."
        params = {
            **common,
            "hostname": hostname,
            "ostemplate": ostemplate,
            "swap": post.get("swap", "0").strip() or "0",
            "password": password,
            "ssh_keys": ssh_keys,
            "ip": post.get("ip", "dhcp").strip() or "dhcp",
        }
        _data, err = create_ct(node, params)

    if err:
        if "403" in err:
            return proxmox_permission_hint("VM.Allocate + Datastore.AllocateSpace (+ SDN.Use for the NIC)")
        return f"Creation failed: {err}"

    record_audit_event(
        request,
        action="guest.create",
        object_type="guest",
        object_id=f"{object_type}:{vmid}",
        details={"node": node, "vmid": vmid, "target_type": object_type, "name": post.get("name") or post.get("hostname") or ""},
        system_username="system",
    )
    return None


def _backup_archive_type(volid: str) -> str:
    name = str(volid).rsplit("/", 1)[-1]
    if "vzdump-qemu-" in name:
        return ProxmoxInventory.ObjectType.VM
    if "vzdump-lxc-" in name:
        return ProxmoxInventory.ObjectType.CT
    return ""


def _backup_archive_vmid(volid: str) -> int | None:
    match = re.search(r"(?:^|[:/])vzdump-(?:qemu|lxc)-(\d+)-", str(volid))
    return int(match.group(1)) if match else None


def _restore_options() -> tuple[list[dict], list[dict], dict[str, dict[str, list[str]]], str]:
    """Discover restoreable archives and compatible target storages live.

    Archive visibility is deliberately evaluated per node. A local backup on
    pve3 must not be presented as restorable on pve99 just because the storage
    IDs happen to share a name.
    """
    archives: list[dict] = []
    nodes: list[dict] = []
    storage_options: dict[str, dict[str, list[str]]] = {}
    nextid = ""
    seen_nodes: set[str] = set()
    seen_archives: set[tuple[str, str, str]] = set()
    for client in common.configured_clients():
        endpoint = str(getattr(client, "endpoint", ""))
        try:
            client_nodes = client.node_names(fallback="")
            if not nextid:
                nextid = str(client.get("cluster/nextid"))
        except ProxmoxAPIError:
            continue
        for node in client_nodes:
            node_key = f"{endpoint}|{node}"
            if node_key not in seen_nodes:
                seen_nodes.add(node_key)
                nodes.append({"key": node_key, "label": node, "node": node, "endpoint": endpoint})
            try:
                storages = client.get(f"nodes/{quote(node, safe='')}/storage")
            except ProxmoxAPIError:
                continue
            node_types = storage_options.setdefault(node_key, {"vm": [], "ct": []})
            for storage in storages if isinstance(storages, list) else []:
                storage_id = str(storage.get("storage") or "")
                if not storage_id or not storage.get("active", 1):
                    continue
                if _storage_supports_content(storage, "images") and storage_id not in node_types["vm"]:
                    node_types["vm"].append(storage_id)
                if _storage_supports_content(storage, "rootdir") and storage_id not in node_types["ct"]:
                    node_types["ct"].append(storage_id)
                if not _storage_supports_content(storage, "backup"):
                    continue
                try:
                    entries = client.get(
                        f"nodes/{quote(node, safe='')}/storage/{quote(storage_id, safe='')}/content?content=backup"
                    )
                except ProxmoxAPIError:
                    continue
                for entry in entries if isinstance(entries, list) else []:
                    volid = str(entry.get("volid") or "")
                    object_type = _backup_archive_type(volid)
                    # Shared backup storage exposes the same archive through
                    # every cluster node. It is still one archive in the UI.
                    key = (endpoint, storage_id, volid)
                    if not object_type or key in seen_archives:
                        continue
                    seen_archives.add(key)
                    ctime = datetime.fromtimestamp(int(entry["ctime"]), dt_timezone.utc) if entry.get("ctime") else None
                    archive_key = "|".join((endpoint, node, storage_id, volid))
                    archives.append(
                        {
                            "key": archive_key,
                            "endpoint": endpoint,
                            "node": node,
                            "storage": storage_id,
                            "volid": volid,
                            "name": volid.rsplit("/", 1)[-1],
                            "source_vmid": _backup_archive_vmid(volid),
                            "object_type": object_type,
                            "type_label": "VM" if object_type == "vm" else "CT",
                            "ctime": ctime,
                            "size": entry.get("size"),
                            "notes": entry.get("notes", ""),
                        }
                    )
    archives.sort(key=lambda item: item["ctime"] or datetime.min.replace(tzinfo=dt_timezone.utc), reverse=True)
    duplicate_names = {item["node"] for item in nodes if sum(other["node"] == item["node"] for other in nodes) > 1}
    for item in nodes:
        if item["node"] in duplicate_names:
            item["label"] = f"{item['node']} · {item['endpoint']}"
    nodes.sort(key=lambda item: (item["node"].casefold(), item["endpoint"]))
    return archives, nodes, storage_options, nextid


def _restore_archive_from_key(key: str, archives: list[dict]) -> dict | None:
    exact = next((archive for archive in archives if archive["key"] == key), None)
    if exact is not None:
        return exact
    endpoint_parts = key.split("|", 3)
    if len(endpoint_parts) == 4:
        endpoint, _node, storage, volid = endpoint_parts
        return next(
            (
                archive
                for archive in archives
                if archive["endpoint"] == endpoint and archive["storage"] == storage and archive["volid"] == volid
            ),
            None,
        )
    # Archive links from the guest Backup tab intentionally omit the endpoint
    # URL. Resolve them from fresh discovery rather than trusting query data.
    parts = key.split("|", 2)
    if len(parts) == 3:
        node, storage, volid = parts
        matches = [
            archive
            for archive in archives
            if archive["node"] == node and archive["storage"] == storage and archive["volid"] == volid
        ]
        return matches[0] if len(matches) == 1 else None
    return None


def _restore_client(endpoint: str):
    for client in common.configured_clients():
        if str(getattr(client, "endpoint", "")) == endpoint:
            return client
    return None


def _queue_guest_backup_restore(request, archives: list[dict]) -> str:
    archive = _restore_archive_from_key(request.POST.get("archive_key", ""), archives)
    if archive is None:
        return "Select a backup archive that is still available."
    target_node_key = request.POST.get("node", "").strip()
    target_storage = request.POST.get("storage", "").strip()
    vmid_text = request.POST.get("vmid", "").strip()
    overwrite = request.POST.get("overwrite") in {"1", "on", "true"}
    start_after = request.POST.get("start_after") in {"1", "on", "true"}
    if "|" not in target_node_key or not target_storage:
        return "Choose a target node and target storage."
    target_endpoint, target_node = target_node_key.rsplit("|", 1)
    if not vmid_text.isdigit() or int(vmid_text) <= 0:
        return "VMID must be a positive whole number."
    vmid = int(vmid_text)
    if overwrite and request.POST.get("overwrite_confirm", "").strip() != vmid_text:
        return f"Enter {vmid} to confirm replacement of the existing guest."

    client = _restore_client(str(archive.get("key", "")).split("|", 1)[0])
    if client is None:
        return "The Proxmox endpoint that exposes this archive is unavailable."
    if target_endpoint != str(getattr(client, "endpoint", "")):
        return "The target node must belong to the Proxmox endpoint that exposes the backup archive."
    try:
        target_storages = client.get(f"nodes/{quote(target_node, safe='')}/storage")
        target_match = next(
            (item for item in (target_storages if isinstance(target_storages, list) else []) if str(item.get("storage") or "") == target_storage),
            None,
        )
        content_type = "images" if archive["object_type"] == ProxmoxInventory.ObjectType.VM else "rootdir"
        if not target_match or not target_match.get("active", 1) or not _storage_supports_content(target_match, content_type):
            return f"Storage '{target_storage}' cannot hold {archive['type_label']} disks on {target_node}."
        archive_entries = client.get(
            f"nodes/{quote(target_node, safe='')}/storage/{quote(str(archive['storage']), safe='')}/content?content=backup"
        )
        if not any(str(entry.get("volid") or "") == archive["volid"] for entry in archive_entries if isinstance(entry, dict)):
            return f"Archive {archive['volid']} is not accessible from {target_node}."
    except ProxmoxAPIError as exc:
        return public_exception_message(
            exc,
            operation="backup_restore_preflight",
            fallback="Restore preflight could not be completed against Proxmox.",
        )

    live_guests = [guest for guest in common.fetch_live_guest_inventory(use_cache=False) if guest.vmid == vmid]
    existing = next((guest for guest in live_guests if guest.object_type == archive["object_type"] and guest.node == target_node), None)
    if live_guests and not overwrite:
        return f"VMID {vmid} is already in use. Enable overwrite only when replacing the existing {archive['type_label']}."
    if overwrite and existing is None:
        return f"No existing {archive['type_label']} with VMID {vmid} exists on {target_node} to overwrite."
    if existing:
        try:
            existing_config = client.guest_config(node=target_node, object_type=archive["object_type"], vmid=vmid)
            existing_current = client.guest_current(node=target_node, object_type=archive["object_type"], vmid=vmid)
        except ProxmoxAPIError as exc:
            return public_exception_message(
                exc,
                operation="backup_restore_overwrite_preflight",
                fallback="Could not inspect the existing guest before overwrite.",
            )
        lock = (existing_config or {}).get("lock") or (existing_current or {}).get("lock")
        if lock:
            return f"The existing guest is locked ({lock})."
        if (existing_config or {}).get("protection") in {1, "1", True}:
            return "The existing guest is protected. Disable protection before overwriting it."

    existing_status = str((existing_current or {}).get("status") or "").lower() if existing else ""
    if existing and not existing_status:
        return "Could not confirm the existing guest's current power state. Restore was not queued."

    target_name = getattr(existing, "name", "") or f"Restored {archive['type_label']} {vmid}"
    detail = SimpleNamespace(
        object_type=archive["object_type"], vmid=vmid, node=target_node, name=target_name, config={}, current={}
    )
    audit_details = {
        "archive": archive["volid"],
        "archive_storage": archive["storage"],
        "source_node": archive["node"],
        "target_storage": target_storage,
        "overwrite": overwrite,
        "start_after": start_after,
        "stage": "queued",
        "proxmox_endpoint": getattr(client, "endpoint", ""),
    }
    event = _audit_guest(request, detail, "guest.backup.restore", audit_details, outcome="running")
    task_id = common.enqueue_bulk_task(
        "core.tasks.restore_guest_backup_task",
        event.id,
        getattr(client, "endpoint", ""),
        target_node,
        archive["object_type"],
        vmid,
        archive["volid"],
        target_storage,
        overwrite,
        bool(existing and existing_status != "stopped"),
        start_after,
        settings.BACKUP_TASK_TIMEOUT_SECONDS,
    )
    event.details = {**event.details, "worker_task_id": task_id}
    event.save(update_fields=["details"])
    return ""

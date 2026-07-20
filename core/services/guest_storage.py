from __future__ import annotations

import re
from dataclasses import dataclass

from django.conf import settings
from django.utils.http import urlencode

from core.models import ClusterStorageMount, StorageMount
from core.services.classification import extract_disk_references
from core.services.datastore_nav import datastore_url

DISK_BUS_RE = re.compile(r"^(scsi|virtio|sata|ide|efidisk|tpmstate|rootfs|mp)\d*$")
NIC_RE = re.compile(r"^net\d+$")
NIC_MODELS = {
    "virtio",
    "e1000",
    "e1000e",
    "rtl8139",
    "vmxnet3",
    "ne2k_pci",
    "pcnet",
    "i82551",
    "i82557b",
    "i82559er",
}


def _mounted_storage_refs(cluster_key: str, node: str) -> dict[str, str]:
    bindings = (
        ClusterStorageMount.objects.select_related("cluster_storage", "mount")
        .filter(
            cluster_storage__cluster__key=cluster_key,
            cluster_storage__present=True,
            mount__enabled=True,
        )
        .order_by("cluster_storage__storage_id")
    )
    result = {
        binding.cluster_storage.storage_id: binding.mount.mount_ref
        for binding in bindings
        if binding.node is None or binding.node == node
    }
    if not result and settings.PVE_TEST_NETWORK_DISABLED:
        for mount in StorageMount.objects.filter(enabled=True):
            if StorageMount.objects.filter(storage_id=mount.storage_id, enabled=True).count() == 1:
                result[mount.storage_id] = mount.mount_ref
    return result


def _storage_link(
    storage_id: str,
    node: str,
    vmid: int,
    mounted_refs: dict[str, str],
    *,
    cluster_key: str,
) -> tuple[bool, str, str]:
    # The guest knows its cluster, node and storage id, which is the datastore
    # page's own key: no mount ref to resolve, and the link is right even when the
    # same mount is bound in more than one cluster.
    if node and storage_id in mounted_refs:
        return True, datastore_url("core:api_storage_files", cluster_key, storage_id, node), "Browse files"
    if node:
        base = datastore_url("core:storage_api_inventory", cluster_key, storage_id, node)
        return False, f"{base}?{urlencode({'vmid': vmid})}", "Storage inventory"
    return False, "", ""


def guest_disks(
    config: dict,
    node: str,
    vmid: int,
    *,
    cluster_key: str,
) -> tuple[list[dict], list[dict]]:
    """Return (disks, cdroms) parsed from the guest config, each disk carrying a
    storage link (mounted browser vs read-only API inventory)."""
    mounted_refs = _mounted_storage_refs(cluster_key, node)
    disks: list[dict] = []
    cdroms: list[dict] = []
    for key in sorted(config or {}):
        if not DISK_BUS_RE.match(key):
            continue
        value = config.get(key)
        if not isinstance(value, str) or not value:
            continue
        parts = value.split(",")
        head = parts[0]
        params = dict(token.split("=", 1) for token in parts[1:] if "=" in token)
        if "media=cdrom" in value or head == "none":
            cdroms.append({"label": key, "value": "Empty" if head == "none" else head})
            continue
        storage_id, sep, volume = head.partition(":")
        if not sep or not storage_id:
            continue
        fmt = "qcow2" if volume.endswith(".qcow2") else "raw" if volume.endswith(".raw") else params.get("format", "")
        mounted, url, link_label = _storage_link(
            storage_id,
            node,
            vmid,
            mounted_refs,
            cluster_key=cluster_key,
        )
        if mounted and url and "/" in volume:
            # Land in the folder that holds the disk, e.g. images/<vmid>/.
            disk_dir = volume.split("/", 1)[0]
            url = f"{url}?{urlencode({'path': f'images/{disk_dir}'})}"
            link_label = "Browse disk folder"
        disks.append(
            {
                "label": key,
                "volid": head,
                "storage_id": storage_id,
                "size": params.get("size", ""),
                "format": fmt,
                "thin": "discard" in params,
                "ssd": params.get("ssd") == "1",
                "mounted": mounted,
                "url": url,
                "link_label": link_label,
            }
        )
    return disks, cdroms


def guest_networks(config: dict) -> list[dict]:
    nets: list[dict] = []
    for key in sorted(config or {}):
        if not NIC_RE.match(key):
            continue
        value = config.get(key)
        if not isinstance(value, str) or not value:
            continue
        entry = {"label": key, "model": "", "mac": "", "bridge": "", "vlan": "", "firewall": False, "rate": ""}
        for token in value.split(","):
            if "=" not in token:
                continue
            name, val = token.split("=", 1)
            if name in NIC_MODELS:
                entry["model"] = name
                entry["mac"] = val
            elif name == "bridge":
                entry["bridge"] = val
            elif name == "tag":
                entry["vlan"] = val
            elif name == "firewall":
                entry["firewall"] = val == "1"
            elif name == "rate":
                entry["rate"] = val
        nets.append(entry)
    return nets


@dataclass(frozen=True)
class GuestVolumeLink:
    """A guest disk normalized into a link, routed to either the mounted
    Module 1 file browser or the read-only Proxmox API storage inventory."""

    volid: str
    storage_id: str
    volume: str
    mounted: bool
    url: str
    link_label: str


def guest_volume_links(
    config: dict,
    node: str,
    vmid: int,
    *,
    cluster_key: str,
) -> list[GuestVolumeLink]:
    mounted_refs = _mounted_storage_refs(cluster_key, node)
    links: list[GuestVolumeLink] = []
    seen: set[str] = set()
    for volid in extract_disk_references(config or {}):
        if volid in seen:
            continue
        seen.add(volid)
        storage_id, _, volume = volid.partition(":")
        if not storage_id:
            continue
        if storage_id in mounted_refs:
            links.append(
                GuestVolumeLink(
                    volid=volid,
                    storage_id=storage_id,
                    volume=volume,
                    mounted=True,
                    url=datastore_url("core:api_storage_files", cluster_key, storage_id, node),
                    link_label="Browse files",
                )
            )
        elif node:
            base = datastore_url("core:storage_api_inventory", cluster_key, storage_id, node)
            links.append(
                GuestVolumeLink(
                    volid=volid,
                    storage_id=storage_id,
                    volume=volume,
                    mounted=False,
                    url=f"{base}?{urlencode({'vmid': vmid})}",
                    link_label="Storage inventory",
                )
            )
        else:
            links.append(
                GuestVolumeLink(
                    volid=volid,
                    storage_id=storage_id,
                    volume=volume,
                    mounted=False,
                    url="",
                    link_label="",
                )
            )
    return links

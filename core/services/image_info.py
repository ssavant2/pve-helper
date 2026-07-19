from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from django.conf import settings

IMAGE_INFO_CATEGORIES = {"vm_disk", "base_image"}


def probe_qemu_image_info(*, path: str, entry_type: str, content_category: str) -> dict[str, Any]:
    if not settings.STORAGE_IMAGE_INFO_ENABLED:
        return {}
    if entry_type != "file" or content_category not in IMAGE_INFO_CATEGORIES:
        return {}

    qemu_img = shutil.which("qemu-img")
    if not qemu_img:
        return {"error": "qemu-img is not installed"}

    try:
        result = subprocess.run(
            [qemu_img, "info", "--output=json", path],
            check=False,
            capture_output=True,
            text=True,
            timeout=settings.STORAGE_IMAGE_INFO_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": exc.__class__.__name__}

    if result.returncode != 0:
        return {"error": (result.stderr or "qemu-img info failed").strip()[:240]}

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": "Invalid qemu-img JSON output"}

    info: dict[str, Any] = {
        "format": payload.get("format") or "",
    }
    virtual_size = payload.get("virtual-size")
    disk_size = payload.get("actual-size")
    if isinstance(virtual_size, int):
        info["virtual_size_bytes"] = virtual_size
    if isinstance(disk_size, int):
        info["disk_size_bytes"] = disk_size
    if payload.get("dirty-flag") is not None:
        info["dirty"] = bool(payload.get("dirty-flag"))
    if info.get("format") == "qcow2":
        info.update(_probe_qcow2_allocation(qemu_img=qemu_img, path=path))
    return {key: value for key, value in info.items() if value not in {"", None}}


def _probe_qcow2_allocation(*, qemu_img: str, path: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [qemu_img, "check", "--output=json", path],
            check=False,
            capture_output=True,
            text=True,
            timeout=settings.STORAGE_IMAGE_INFO_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"qcow2_allocation_error": exc.__class__.__name__}

    if result.returncode != 0:
        return {"qcow2_allocation_error": (result.stderr or "qemu-img check failed").strip()[:240]}

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"qcow2_allocation_error": "Invalid qemu-img check JSON output"}

    total_clusters = payload.get("total-clusters")
    allocated_clusters = payload.get("allocated-clusters", 0)
    if not isinstance(total_clusters, int) or total_clusters <= 0:
        return {"qcow2_allocation_error": "qemu-img check did not report total clusters"}
    if not isinstance(allocated_clusters, int) or allocated_clusters < 0:
        return {"qcow2_allocation_error": "qemu-img check did not report valid allocated clusters"}

    allocation_percent = min(100.0, allocated_clusters * 100.0 / total_clusters)
    return {
        "qcow2_allocated_clusters": allocated_clusters,
        "qcow2_total_clusters": total_clusters,
        "qcow2_allocation_percent": allocation_percent,
    }

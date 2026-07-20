from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

IMAGE_INFO_CATEGORIES = {"vm_disk", "base_image"}

# qemu-img reports why it failed as free-form English on stderr; the exit code is 1
# for everything and there is no machine-readable field to read instead. Matching
# text is therefore the only way to tell "the datastore is full" from "the image is
# broken" — two failures with completely different answers for the operator. Each
# needle is one literal qemu-img or strerror phrase rather than a guess at a family
# of them, and the sentences are worded for any qemu-img call, because the same
# cause reaches the operator from `info`, `check` and `convert` alike.
_QEMU_IMG_FAILURES: tuple[tuple[str, str], ...] = (
    ("no space left on device", "The datastore is out of free space."),
    ("disk quota exceeded", "The datastore's quota is exhausted."),
    ("permission denied", "pve-helper is not permitted to access this file."),
    ("read-only file system", "The datastore is mounted read-only."),
    ("input/output error", "The datastore reported an I/O error."),
    ("image is corrupt", "The image is corrupt."),
    ("is not in qcow2 format", "The image is not in qcow2 format."),
)


def qemu_img_failure_cause(stderr: str) -> str | None:
    """One operator-actionable sentence for a qemu-img failure, or None.

    None is a real answer and callers must keep it that way: naming the wrong
    cause sends the operator after the wrong problem, which is worse than saying
    the output is in the log. The raw text never leaves this boundary — it is
    unstructured external data carrying host paths, and it also ends up persisted
    in scan evidence, where the project's own rule puts stable messages only.
    """
    lowered = stderr.lower()
    return next((cause for needle, cause in _QEMU_IMG_FAILURES if needle in lowered), None)


def _probe_failure(*, subject: str, command: str, stderr: str, path: str) -> str:
    logger.warning("qemu-img %s failed: path=%s stderr=%s", command, path, stderr.strip())
    cause = qemu_img_failure_cause(stderr)
    return f"{subject} {cause}" if cause else f"{subject} The qemu-img output is in the application log."


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
        return {
            "error": _probe_failure(
                subject="Image details are unavailable.",
                command="info",
                stderr=result.stderr or "",
                path=path,
            )
        }

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
        return {
            "qcow2_allocation_error": _probe_failure(
                subject="The qcow2 map could not be read.",
                command="check",
                stderr=result.stderr or "",
                path=path,
            )
        }

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

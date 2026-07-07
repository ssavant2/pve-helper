from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Any

from core.models import FileInventory


@dataclass(frozen=True)
class DerivedVolume:
    storage_id: str
    relative_path: str
    volid: str
    content_category: str


def categorize_proxmox_path(relative_path: str) -> str:
    path = PurePosixPath(relative_path)
    parts = path.parts
    if not parts:
        return "unknown"
    if parts[0].startswith(".pve-helper"):
        return "app_internal"
    if parts[:1] == (".trash",):
        return "trash"
    if parts == ("images",):
        return "vm_images"
    if len(parts) == 2 and parts[0] == "images":
        return "vm_image_directory"
    if len(parts) >= 3 and parts[0] == "images" and parts[2].startswith("base-"):
        return "base_image"
    if len(parts) >= 3 and parts[0] == "images" and parts[2].startswith("vm-"):
        return "vm_disk"
    if parts[:1] == ("dump",):
        return "backup"
    if parts == ("template",):
        return "template_directory"
    if parts[:2] == ("template", "iso"):
        return "iso"
    if parts[:2] == ("template", "cache"):
        return "ct_template"
    if parts[:1] == ("snippets",):
        return "snippet"
    if parts[:1] == ("private",):
        return "ct_private"
    if parts == ("import",):
        return "import_directory"
    if parts[:1] == ("import",):
        return "import_content"
    return "unknown"


def derive_volid(storage_id: str, relative_path: str) -> DerivedVolume | None:
    category = categorize_proxmox_path(relative_path)
    if category in {"vm_disk", "base_image"}:
        path = PurePosixPath(relative_path)
        return DerivedVolume(
            storage_id=storage_id,
            relative_path=relative_path,
            volid=f"{storage_id}:{path.parts[1]}/{path.name}",
            content_category=category,
        )
    return None


def extract_vmid_from_image_path(relative_path: str) -> int | None:
    path = PurePosixPath(relative_path)
    parts = path.parts
    if len(parts) < 3 or parts[0] != "images":
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


DISK_CONFIG_KEYS = re.compile(
    r"^(?:"
    r"ide|sata|scsi|virtio|efidisk|tpmstate|unused|"
    r"rootfs|mp"
    r")\d*$"
)


def extract_disk_references(config: dict[str, Any]) -> list[str]:
    references: list[str] = []

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                visit(child_value, str(child_key))
            return
        if isinstance(value, list):
            for child_value in value:
                visit(child_value, key)
            return
        if not isinstance(value, str):
            return
        if not DISK_CONFIG_KEYS.match(key):
            return

        volid = parse_config_value_volid(value)
        if volid:
            references.append(volid)

    visit(config)
    return sorted(set(references))


def parse_config_value_volid(value: str) -> str:
    first_value = value.split(",", 1)[0].strip()
    if not first_value or first_value == "none":
        return ""
    if first_value.startswith(("http://", "https://")):
        return ""
    if ":" not in first_value:
        return ""
    return first_value


# Disk-image extensions used to tell a stray disk image apart from any other
# loose file that happens to sit directly in the ``images/`` directory.
DISK_IMAGE_EXTENSIONS = {".qcow2", ".raw", ".vmdk", ".img", ".qed", ".vdi", ".vhd", ".vhdx"}

# Expected file extensions for content categories that have a clear file type,
# so a stray file (e.g. a .txt in template/iso) is not treated as that content.
CONTENT_EXTENSIONS = {
    "iso": (".iso", ".img"),
    "ct_template": (".tar.gz", ".tar.xz", ".tar.zst", ".tar.bz2", ".tar.lzo", ".tgz", ".tar"),
}
CONTENT_LABELS = {
    "iso": "ISO",
    "ct_template": "container template",
}


@dataclass(frozen=True)
class ClassificationResult:
    classification: str
    reason: str
    matched_object: dict[str, Any]
    evidence: dict[str, Any]


def classify_entry(
    *,
    relative_path: str,
    entry_type: str,
    content_category: str,
    derived_volid: str,
    referenced_volids: set[str],
    template_vmids: set[int],
    gate_ok: bool,
    missing_consumers: list[str],
) -> ClassificationResult:
    evidence = {
        "derived_volid": derived_volid,
        "content_category": content_category,
        "gate_ok": gate_ok,
        "missing_consumers": missing_consumers,
    }

    if content_category == "app_internal" or relative_path.startswith(".pve-helper"):
        return ClassificationResult(
            classification=FileInventory.Classification.INFRASTRUCTURE,
            reason="App-managed internal directory (upload staging / working files).",
            matched_object={},
            evidence=evidence,
        )

    if content_category == "trash" or relative_path.startswith(".trash/"):
        if entry_type != FileInventory.EntryType.FILE:
            return ClassificationResult(
                classification=FileInventory.Classification.INFRASTRUCTURE,
                reason="Trash directory structure.",
                matched_object={},
                evidence=evidence,
            )
        return ClassificationResult(
            classification=FileInventory.Classification.TRASH,
            reason="File is already under the storage trash path.",
            matched_object={},
            evidence=evidence,
        )

    if derived_volid and derived_volid in referenced_volids:
        return ClassificationResult(
            classification=FileInventory.Classification.REFERENCED,
            reason="Exact Proxmox volid is referenced by current VM/CT configuration.",
            matched_object={"volid": derived_volid},
            evidence=evidence,
        )

    if content_category == "base_image":
        vmid = extract_vmid_from_image_path(relative_path)
        evidence["vmid"] = vmid
        if vmid in template_vmids:
            return ClassificationResult(
                classification=FileInventory.Classification.REFERENCED,
                reason="Base image belongs to an inventoried Proxmox template.",
                matched_object={"vmid": vmid, "object_type": "template"},
                evidence=evidence,
            )
        return ClassificationResult(
            classification=FileInventory.Classification.UNKNOWN,
            reason="Base images are never marked orphan in V1; backing-chain analysis is deferred.",
            matched_object={},
            evidence=evidence,
        )

    KNOWN_DIRECTORY_CATEGORIES = {
        "vm_images", "vm_image_directory", "backup", "iso",
        "ct_template", "ct_private", "snippet", "template_directory",
        "import_directory",
    }

    if entry_type != FileInventory.EntryType.FILE:
        if content_category in KNOWN_DIRECTORY_CATEGORIES:
            return ClassificationResult(
                classification=FileInventory.Classification.INFRASTRUCTURE,
                reason="Standard Proxmox storage directory structure.",
                matched_object={},
                evidence=evidence,
            )
        return ClassificationResult(
            classification=FileInventory.Classification.UNKNOWN,
            reason="Directory with unrecognized content category.",
            matched_object={},
            evidence=evidence,
        )

    # A true orphan is specifically a Proxmox VM disk volume (vm-<id>-disk-N)
    # with no matching VM/CT reference. Nothing else is called an orphan.
    if content_category == "vm_disk":
        if not gate_ok:
            return ClassificationResult(
                classification=FileInventory.Classification.CLASSIFICATION_BLOCKED,
                reason="Not all expected storage consumers were inventoried in this scan-run.",
                matched_object={},
                evidence=evidence,
            )
        return ClassificationResult(
            classification=FileInventory.Classification.LIKELY_ORPHAN,
            reason="VM disk file has no exact volid reference in the same scan-run.",
            matched_object={},
            evidence=evidence,
        )

    KNOWN_CONTENT_CATEGORIES = {
        "backup", "iso", "ct_template", "ct_private", "snippet", "import_content",
    }
    if content_category in KNOWN_CONTENT_CATEGORIES:
        # Being in the right folder isn't enough: a stray file (e.g. a .txt in
        # template/iso) is not Proxmox content. For categories with a clear file
        # type, require a matching extension; otherwise flag it as misplaced.
        allowed = CONTENT_EXTENSIONS.get(content_category)
        if allowed and entry_type == FileInventory.EntryType.FILE:
            name = PurePosixPath(relative_path).name.lower()
            if not any(name.endswith(ext) for ext in allowed):
                label = CONTENT_LABELS.get(content_category, content_category)
                return ClassificationResult(
                    classification=FileInventory.Classification.UNKNOWN,
                    reason=(
                        f"File is in the {label} directory but is not a recognised "
                        f"{label} file — it is probably misplaced."
                    ),
                    matched_object={},
                    evidence=evidence,
                )
        return ClassificationResult(
            classification=FileInventory.Classification.PROXMOX_CONTENT,
            reason="File belongs to a recognized Proxmox content type.",
            matched_object={},
            evidence=evidence,
        )

    # A recognised disk image that is not a proper vm-<id>-disk volume is not an
    # orphan — it's a real disk image sitting in the wrong place.
    if PurePosixPath(relative_path).suffix.lower() in DISK_IMAGE_EXTENSIONS:
        return ClassificationResult(
            classification=FileInventory.Classification.UNKNOWN,
            reason=(
                "Disk image that is not a referenced Proxmox volume — it is probably in the "
                "wrong folder. A VM disk belongs in images/<vmid>/vm-<vmid>-disk-N.<format>; "
                "to bring in a loose image, put it in the storage's import/ directory or use "
                "Import as VM."
            ),
            matched_object={},
            evidence=evidence,
        )

    return ClassificationResult(
        classification=FileInventory.Classification.UNKNOWN,
        reason="File type is not used by Proxmox and probably does not belong in this storage.",
        matched_object={},
        evidence=evidence,
    )

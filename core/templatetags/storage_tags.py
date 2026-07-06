"""Small helpers for the storage file browser."""

from __future__ import annotations

from django import template

register = template.Library()

# Disk-image extensions the "Import as VM" flow can stage and import
# (kept in sync with core.services.vm_register._stage_source). ".img" is
# accepted too — its real format is detected with qemu-img at import time.
IMPORTABLE_IMAGE_EXTS = {"qcow2", "raw", "vmdk", "img"}


@register.filter
def is_disk_image(name: str) -> bool:
    """True when a filename looks like an importable VM disk image.

    Gates the file browser's "Import as VM" action by extension rather than by
    the scan's path-based categorisation, so an image uploaded to any folder is
    importable — not only files already living under ``images/<vmid>/``.
    """
    if not name or "." not in name:
        return False
    return name.rsplit(".", 1)[-1].lower() in IMPORTABLE_IMAGE_EXTS

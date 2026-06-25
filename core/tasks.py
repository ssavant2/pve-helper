from __future__ import annotations

from django.utils import timezone

from .models import ScanRun


def run_scan(scan_run_id: int) -> None:
    """Phase 0 scan task placeholder.

    The worker path is intentionally wired now, while the real Proxmox/NFS
    inventory implementation lands in the next phases.
    """

    scan = ScanRun.objects.get(pk=scan_run_id)
    scan.status = ScanRun.Status.RUNNING
    scan.started_at = timezone.now()
    scan.progress_message = "Scan worker is wired; inventory implementation is pending."
    scan.save(update_fields=["status", "started_at", "progress_message", "updated_at"])

    scan.status = ScanRun.Status.COMPLETED
    scan.finished_at = timezone.now()
    scan.summary_counts = {"phase": "skeleton", "files": 0, "proxmox_objects": 0}
    scan.progress_message = "Skeleton scan completed."
    scan.save(update_fields=["status", "finished_at", "summary_counts", "progress_message", "updated_at"])

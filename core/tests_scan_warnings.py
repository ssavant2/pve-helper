from django.test import TestCase

from core.models import AuditEvent, ScanRun
from core.services.recent_tasks import _scan_task
from core.services.scan_warnings import scan_warning_summary
from core.views.common import _audit_detail_label


class ScanWarningSummaryTests(TestCase):
    def test_summarizes_proxmox_node_actions_without_rendering_provider_text(self):
        provider_text = "595: No route to host at https://pve3.internal:8006/api2/json"
        details = {
            "proxmox": {
                "pve3": [
                    {"path": "nodes/pve3/qemu", "error": provider_text, "action": "qemu.list"},
                    {"path": "nodes/pve3/lxc", "error": provider_text, "action": "lxc.list"},
                ]
            }
        }

        summary = scan_warning_summary(details)

        self.assertEqual(summary, "pve3: QEMU inventory and LXC inventory could not be read")
        self.assertNotIn(provider_text, summary)
        self.assertNotIn("pve3.internal", summary)

    def test_summarizes_storage_path_errors(self):
        details = {
            "storage": {
                "nfs-vm": {
                    "errors": [
                        {"path": "images/100", "error": "Permission denied"},
                        {"path": "images/200", "error": "Input/output error"},
                        {"path": "images/300", "error": "Stale file handle"},
                    ]
                }
            }
        }

        summary = scan_warning_summary(details)

        self.assertEqual(
            summary,
            "nfs-vm: storage content could not be read at images/100 and images/200 and 1 more path(s)",
        )
        self.assertNotIn("Permission denied", summary)
        self.assertNotIn("Input/output error", summary)

    def test_limits_number_of_warning_groups(self):
        details = {
            "proxmox": {
                "pve1": [{"error": "unreachable"}],
                "pve2": [{"error": "unreachable"}],
                "pve3": [{"error": "unreachable"}],
                "pve4": [{"error": "unreachable"}],
            }
        }

        summary = scan_warning_summary(details)

        self.assertIn("pve1", summary)
        self.assertIn("pve3", summary)
        self.assertNotIn("pve4", summary)
        self.assertIn("and 1 more warning(s)", summary)

    def test_audit_scan_detail_uses_the_structured_error_payload(self):
        event = AuditEvent(
            action="scan.completed",
            outcome="warning",
            details={
                "warnings": 1,
                "error_details": {"proxmox": {"pve3": [{"action": "qemu.list", "error": "595: No route to host"}]}},
            },
        )

        self.assertEqual(
            _audit_detail_label(event),
            "pve3: QEMU inventory could not be read",
        )

    def test_recent_scan_task_replaces_the_warning_count_with_the_cause(self):
        scan = ScanRun.objects.create(
            status=ScanRun.Status.COMPLETED,
            progress_message="Scan completed with 1 warning(s).",
            summary_counts={"files": 52, "classifications": {"referenced": 18}},
            error_details={"proxmox": {"pve3": [{"action": "lxc.list", "error": "595: No route to host"}]}},
        )

        task = _scan_task(scan, "system")

        self.assertEqual(task["status"], "Completed with warnings")
        self.assertEqual(
            task["details"],
            "52 files, 18 referenced, pve3: LXC inventory could not be read",
        )

    def test_unknown_legacy_failure_does_not_render_stored_prose_or_error_type(self):
        details = {
            "error": "RuntimeError",
            "message": "request failed at https://pve3.internal:8006/api2/json",
        }

        summary = scan_warning_summary(details)

        self.assertEqual(summary, "The storage scan did not complete.")
        self.assertNotIn("RuntimeError", summary)
        self.assertNotIn("pve3.internal", summary)

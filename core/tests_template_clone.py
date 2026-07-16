from unittest.mock import patch

from django.test import TestCase

from core.models import AuditEvent
from core.services.proxmox import ProxmoxTaskResult
from core.template_clone_tasks import clone_guest_to_template_task


class TemplateCloneTaskTests(TestCase):
    def test_clone_to_template_waits_for_clone_then_converts_result(self):
        event = AuditEvent.objects.create(
            action="guest.template.clone",
            object_type="guest",
            object_id="vm:505",
            outcome="running",
            details={"node": "pve3", "vmid": 505, "new_vmid": 508, "stage": "clone"},
        )

        class FakeClient:
            def __init__(self, _endpoint):
                self.posts = []

            def wait_for_task(self, *, node, upid, timeout_seconds):
                return ProxmoxTaskResult(node=node, upid=upid, status="stopped", exitstatus="OK", raw={})

            def post(self, path, data):
                self.posts.append((path, data))
                return "UPID:pve3:template:508"

        fake = FakeClient("unused")
        with patch("core.template_clone_tasks.ProxmoxClient", return_value=fake):
            clone_guest_to_template_task(
                event.id,
                "https://pve3.invalid:8006",
                "pve3",
                508,
                "UPID:pve3:clone:508",
                3600,
            )

        event.refresh_from_db()
        self.assertEqual(event.outcome, "success")
        self.assertEqual(fake.posts, [("nodes/pve3/qemu/508/template", {})])
        self.assertEqual(event.details["completed_stages"], ["clone", "template"])

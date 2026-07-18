from unittest.mock import patch

from django.test import TestCase

from core.models import AuditEvent, ProxmoxCluster, ProxmoxEndpoint
from core.services.refs import GuestRef
from core.services.proxmox import ProxmoxTaskResult
from core.template_clone_tasks import clone_guest_to_template_task


class TemplateCloneTaskTests(TestCase):
    def test_clone_to_template_waits_for_clone_then_converts_result(self):
        cluster = ProxmoxCluster.objects.create(
            key="default", display_name="Default cluster", enabled=True
        )
        ProxmoxEndpoint.objects.create(
            cluster=cluster,
            name="pve3",
            url="https://pve3.invalid:8006",
        )
        event = AuditEvent.objects.create(
            cluster=cluster,
            action="guest.template.clone",
            object_type="guest",
            object_id="vm:505",
            outcome="running",
            details={
                "guest_ref": GuestRef(cluster.key, "vm", 505, "pve3").serialize(),
                "node": "pve3",
                "vmid": 505,
                "new_vmid": 508,
                "stage": "clone",
                "proxmox_task_node": "pve3",
                "proxmox_task_upid": "UPID:pve3:clone:508",
            },
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
        with patch(
            "core.template_clone_tasks.client_for_audit_event",
            return_value=(fake, GuestRef(cluster.key, "vm", 505, "pve3"), cluster),
        ):
            clone_guest_to_template_task(event.id)

        event.refresh_from_db()
        self.assertEqual(event.outcome, "success")
        self.assertEqual(fake.posts, [("nodes/pve3/qemu/508/template", {})])
        self.assertEqual(event.details["completed_stages"], ["clone", "template"])

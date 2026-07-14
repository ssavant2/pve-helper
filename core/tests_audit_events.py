from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase

from core.services.audit_events import audit_module_key, record_audit_event


class AuditEventServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="operator")
        self.factory = RequestFactory()

    def test_request_event_normalizes_actor_ip_module_and_detail_fields(self):
        request = self.factory.post("/storage/action/", REMOTE_ADDR="192.0.2.10")
        request.user = self.user

        event = record_audit_event(
            request,
            action="file.inflated",
            object_type="file",
            object_id="nfs-vm:images/100/disk.qcow2",
            details={
                "storage_id": "nfs-vm",
                "path": "images/100/disk.qcow2",
                "target_preallocation": "metadata",
            },
        )

        self.assertEqual(event.user, self.user)
        self.assertEqual(event.username, "operator")
        self.assertEqual(event.source_ip, "192.0.2.10")
        self.assertEqual(event.module, "storage")
        self.assertEqual(event.storage_id, "nfs-vm")
        self.assertEqual(event.path, "images/100/disk.qcow2")
        self.assertEqual(event.target_preallocation, "metadata")

    def test_worker_event_uses_explicit_actor_and_persists_filter_module(self):
        event = record_audit_event(
            username="system",
            action="tag.membership.removed",
            object_type="guest",
            object_id="vm:500@pve1",
        )

        self.assertIsNone(event.user)
        self.assertEqual(event.username, "system")
        self.assertIsNone(event.source_ip)
        self.assertEqual(event.module, "vms")

    def test_worker_user_supplies_username_when_not_explicit(self):
        event = record_audit_event(
            user=self.user,
            action="scheduled_action.run_completed",
            object_type="scheduled_action_run",
        )

        self.assertEqual(event.user, self.user)
        self.assertEqual(event.username, "operator")
        self.assertEqual(event.module, "vms")

    def test_unauthenticated_request_uses_system_username(self):
        request = self.factory.post("/internal/")
        request.user = AnonymousUser()

        event = record_audit_event(
            request,
            action="audit.retention.schedule.updated",
            system_username="system",
        )

        self.assertIsNone(event.user)
        self.assertEqual(event.username, "system")
        self.assertEqual(event.module, "system")

    def test_signal_actor_is_preserved_when_request_is_already_anonymous(self):
        request = self.factory.post("/logout/", REMOTE_ADDR="192.0.2.20")
        request.user = AnonymousUser()

        event = record_audit_event(
            request,
            user=self.user,
            action="auth.logout",
            object_type="user",
            object_id="operator",
        )

        self.assertEqual(event.user, self.user)
        self.assertEqual(event.username, "operator")
        self.assertEqual(event.source_ip, "192.0.2.20")
        self.assertEqual(event.module, "auth")

    def test_module_classification_is_shared_for_all_supported_modules(self):
        self.assertEqual(audit_module_key("auth.login", "user"), "auth")
        self.assertEqual(audit_module_key("network.firewall.updated"), "network")
        self.assertEqual(audit_module_key("guest.power.start", "guest"), "vms")
        self.assertEqual(audit_module_key("cluster.updated", "cluster"), "clusters")
        self.assertEqual(audit_module_key("file.inflated", "file"), "storage")
        self.assertEqual(audit_module_key("tag.deleted", "tag"), "system")

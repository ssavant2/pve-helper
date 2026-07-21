from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase
from django.urls import reverse

from core.models import DETAIL_DERIVED_AUDIT_FIELDS, AuditEvent
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

    def test_worker_tag_event_is_visible_in_the_vms_audit_filter(self):
        self.client.force_login(self.user)
        worker_event = record_audit_event(
            username="system",
            action="tag.membership.removed",
            object_type="guest",
            object_id="vm:500@pve1",
        )
        system_event = record_audit_event(
            username="system",
            action="tag.deleted",
            object_type="tag",
            object_id="prod",
        )

        response = self.client.get(reverse("core:audit_log"), {"filter": "vms"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["audit_total"], 1)
        self.assertEqual([event.pk for event in response.context["events"]], [worker_event.pk])
        self.assertNotIn(system_event, response.context["events"])

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


class DerivedAuditColumnTests(TestCase):
    """`storage_id`/`path`/`target_preallocation` are projections of `details`.

    They are what the audit filters and `core_audit_store_path_pre_idx` read, so
    they have to follow `details` however the row is written — and roughly twenty
    call sites in `tasks.py`, `ovf_import_tasks.py`, `template_clone_tasks.py`
    and the guest views finish an event with
    `save(update_fields=["outcome", "details"])`. Django writes only the listed
    columns, so before this the recomputation in `save()` was discarded on
    exactly the saves that change the source of the projection.
    """

    def _event(self, **details) -> AuditEvent:
        return AuditEvent.objects.create(action="file.moved", object_type="file", details=details)

    def test_finishing_an_event_persists_the_recomputed_columns(self):
        event = self._event(storage_id="nfs-vm", path="images/100/disk.qcow2")

        event.details = {**event.details, "path": "images/101/disk.qcow2", "finished_at": "2026-07-21T12:00:00Z"}
        event.outcome = "failed"
        event.save(update_fields=["outcome", "details"])

        stored = AuditEvent.objects.get(pk=event.pk)
        self.assertEqual(stored.path, "images/101/disk.qcow2", "The derived column did not follow `details`.")
        self.assertEqual(stored.storage_id, "nfs-vm")
        self.assertEqual(stored.outcome, "failed")

    def test_a_key_removed_from_details_clears_its_column(self):
        """Otherwise a row keeps answering a filter for a fact it no longer states."""
        event = self._event(storage_id="nfs-vm", target_preallocation="metadata")

        event.details = {"storage_id": "nfs-vm"}
        event.save(update_fields=["details"])

        self.assertEqual(AuditEvent.objects.get(pk=event.pk).target_preallocation, "")

    def test_an_update_that_does_not_touch_details_writes_only_what_it_names(self):
        """The fix widens `update_fields`; it must not widen it unconditionally."""
        event = self._event(storage_id="nfs-vm")
        AuditEvent.objects.filter(pk=event.pk).update(path="written-by-another-writer")

        event.outcome = "cancelled"
        event.save(update_fields=["outcome"])

        stored = AuditEvent.objects.get(pk=event.pk)
        self.assertEqual(stored.outcome, "cancelled")
        self.assertEqual(stored.path, "written-by-another-writer", "A save that did not name `details` overwrote it.")

    def test_every_derived_field_is_a_real_column(self):
        for field in DETAIL_DERIVED_AUDIT_FIELDS:
            AuditEvent._meta.get_field(field)

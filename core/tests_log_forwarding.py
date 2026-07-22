from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django_q.models import Schedule

from core.models import AuditEvent, LogForwardingDelivery
from core.services.audit_events import record_audit_event
from core.services.log_forwarding import (
    LOG_FORWARDER_SCHEDULE_NAME,
    _rfc5424_message,
    configuration,
    deliver_pending_log_events,
    update_configuration,
)


class LogForwardingServiceTests(TestCase):
    def enable(self, *, transport="tls"):
        return update_configuration(enabled=True, host="siem.example.test", port=6514, transport=transport)

    def test_disabled_forwarder_does_not_queue_audit_events(self):
        record_audit_event(action="test.disabled", outcome="success")

        self.assertFalse(LogForwardingDelivery.objects.exists())

    def test_event_create_and_update_create_ordered_safe_snapshots(self):
        self.enable()
        event = record_audit_event(
            action="scan.completed",
            object_type="scan_run",
            object_id="42",
            outcome="running",
            details={
                "storage_id": "local-nfs",
                "path": "dump/example.vma.zst",
                "error": "requests.ConnectionError https://token@internal.invalid",
            },
        )
        event.outcome = "warning"
        event.details["error"] = "RuntimeError: provider secret"
        event.save(update_fields=["outcome", "details"])

        deliveries = list(LogForwardingDelivery.objects.filter(audit_event_id=event.id).order_by("sequence"))
        self.assertEqual([item.sequence for item in deliveries], [1, 2])
        self.assertEqual(deliveries[0].payload["outcome"], "running")
        self.assertEqual(deliveries[1].payload["outcome"], "warning")
        serialized = str(deliveries[1].payload)
        self.assertNotIn("internal.invalid", serialized)
        self.assertNotIn("provider secret", serialized)
        self.assertEqual(deliveries[1].payload["storage_id"], "local-nfs")

    def test_rfc5424_uses_octet_framing_and_stable_event_identity(self):
        config = self.enable(transport="tcp")
        event = record_audit_event(action="guest.power.stop", outcome="failed")
        delivery = LogForwardingDelivery.objects.get(audit_event_id=event.id)

        framed = _rfc5424_message(config, delivery)
        length, message = framed.split(b" ", 1)

        self.assertEqual(int(length), len(message))
        self.assertTrue(message.startswith(b"<131>1 "))
        self.assertIn(b" pve-helper - guest.power.stop - ", message)
        self.assertIn(f'"event_id":"audit-{event.id}"'.encode(), message)
        self.assertIn(b'"sequence":1', message)

    @patch("core.services.log_forwarding._send")
    def test_worker_marks_delivery_sent(self, send):
        config = self.enable()
        event = record_audit_event(action="test.success", outcome="success")

        self.assertEqual(deliver_pending_log_events(), 1)

        delivery = LogForwardingDelivery.objects.get(audit_event_id=event.id)
        config.refresh_from_db()
        self.assertEqual(delivery.status, LogForwardingDelivery.Status.SENT)
        self.assertIsNotNone(delivery.delivered_at)
        self.assertIsNotNone(config.last_success_at)
        send.assert_called_once()

    @patch("core.services.log_forwarding._send", side_effect=OSError("secret raw socket diagnostic"))
    def test_worker_retries_without_persisting_raw_exception(self, send):
        config = self.enable()
        event = record_audit_event(action="test.failure", outcome="success")

        self.assertEqual(deliver_pending_log_events(), 0)

        delivery = LogForwardingDelivery.objects.get(audit_event_id=event.id)
        config.refresh_from_db()
        self.assertEqual(delivery.status, LogForwardingDelivery.Status.PENDING)
        self.assertEqual(delivery.last_error_code, "syslog_connection_failed")
        self.assertEqual(config.last_error_code, "syslog_connection_failed")
        self.assertNotIn("secret raw", delivery.last_error_code)
        self.assertGreater(delivery.next_attempt_at, delivery.created_at)

    def test_outbox_survives_audit_retention_delete(self):
        self.enable()
        event = record_audit_event(action="test.retention", outcome="success")
        delivery_id = LogForwardingDelivery.objects.get(audit_event_id=event.id).id

        event.delete()

        self.assertTrue(LogForwardingDelivery.objects.filter(pk=delivery_id).exists())

    def test_disabling_removes_internal_schedule(self):
        self.enable()
        self.assertTrue(Schedule.objects.filter(name=LOG_FORWARDER_SCHEDULE_NAME).exists())

        update_configuration(enabled=False, host="siem.example.test", port=6514, transport="tls")

        self.assertFalse(Schedule.objects.filter(name=LOG_FORWARDER_SCHEDULE_NAME).exists())


class LogForwardingViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="operator", password="password")
        self.client.force_login(self.user)

    def test_settings_page_and_audit_shortcut_are_reachable(self):
        settings_response = self.client.get(reverse("core:settings_log_forwarder"))
        audit_response = self.client.get(reverse("core:audit_log"))

        self.assertContains(settings_response, "Log forwarder")
        self.assertContains(settings_response, "RFC 5424 destination")
        self.assertContains(settings_response, reverse("core:settings_storage"))
        self.assertContains(audit_response, reverse("core:settings_log_forwarder"))

    def test_save_enables_forwarding_and_audits_change(self):
        response = self.client.post(
            reverse("core:settings_log_forwarder"),
            {"enabled": "on", "host": "siem.example.test", "port": "6514", "transport": "tls"},
        )

        self.assertRedirects(response, reverse("core:settings_log_forwarder"))
        config = configuration()
        self.assertTrue(config.enabled)
        self.assertEqual(config.host, "siem.example.test")
        event = AuditEvent.objects.get(action="log_forwarder.configuration.updated")
        self.assertTrue(LogForwardingDelivery.objects.filter(audit_event_id=event.id).exists())
        follow_up = self.client.get(response.url)
        self.assertNotContains(follow_up, "Log forwarding configuration saved.")
        self.assertNotContains(follow_up, 'class="messages"')

        audit = self.client.get(reverse("core:audit_log"))
        self.assertContains(audit, "Update log forwarding configuration")
        self.assertContains(audit, "Log forwarder")
        self.assertContains(audit, "Enabled · TCP with TLS to siem.example.test:6514")
        self.assertNotContains(audit, "log_forwarder.configuration.updated")

    def test_invalid_destination_is_rejected_without_raw_error(self):
        response = self.client.post(
            reverse("core:settings_log_forwarder"),
            {"enabled": "on", "host": "bad host", "port": "99999", "transport": "tls"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "without whitespace")
        self.assertFalse(configuration().enabled)

    def test_status_endpoint_reports_live_pending_and_paused_state(self):
        update_configuration(enabled=True, host="siem.example.test", port=6514, transport="tls")
        record_audit_event(action="test.pending", outcome="success")

        active = self.client.get(reverse("core:settings_log_forwarder_status"))

        self.assertEqual(active.status_code, 200)
        self.assertEqual(active.json()["pending"], 1)
        self.assertEqual(active.json()["pending_label"], "1")
        self.assertFalse(active.json()["paused"])

        update_configuration(enabled=False, host="siem.example.test", port=6514, transport="tls")
        paused = self.client.get(reverse("core:settings_log_forwarder_status"))

        self.assertEqual(paused.json()["pending_label"], "1 (paused)")
        self.assertTrue(paused.json()["paused"])

    @patch("core.views.log_forwarding.async_task")
    def test_test_button_creates_real_audit_delivery(self, async_task):
        update_configuration(enabled=True, host="siem.example.test", port=6514, transport="tls")

        response = self.client.post(reverse("core:settings_log_forwarder_test"))

        self.assertRedirects(response, reverse("core:settings_log_forwarder") + "?test=queued")
        event = AuditEvent.objects.get(action="log_forwarder.test_requested")
        self.assertTrue(LogForwardingDelivery.objects.filter(audit_event_id=event.id).exists())
        async_task.assert_called_once()

        follow_up = self.client.get(response.url)
        self.assertContains(follow_up, "Test event queued for delivery.")
        self.assertContains(follow_up, "log-forwarder-test-feedback")
        self.assertNotContains(follow_up, 'class="messages"')

        audit = self.client.get(reverse("core:audit_log"))
        self.assertContains(audit, "Send log forwarding test event")
        self.assertContains(audit, "Test event · TCP with TLS to siem.example.test:6514")
        self.assertNotContains(audit, "log_forwarder.test_requested")

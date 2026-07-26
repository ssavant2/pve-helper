from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, ProxmoxCluster, ScheduledAction, ScheduledActionRun, ScheduledActionSettings
from core.services.scheduled_actions import prune_scheduled_action_runs


class ScheduledActionSettingsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="scheduler-admin", password="unused")
        self.client.force_login(self.user)
        self.url = reverse("core:settings_scheduled_tasks")

    def test_page_uses_default_and_alphabetical_settings_tabs(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["config"].run_history_retention_days, 90)
        self.assertContains(response, 'name="run_history_retention_days"')
        self.assertContains(response, 'min="1"')
        self.assertContains(response, 'max="999"')
        content = response.content.decode()
        settings_tabs = content.split('aria-label="PVE-helper settings areas"', 1)[1].split("</nav>", 1)[0]
        self.assertLess(settings_tabs.index("Certificates"), settings_tabs.index("Log forwarder"))
        self.assertLess(settings_tabs.index("Log forwarder"), settings_tabs.index("Scheduled Tasks"))
        self.assertLess(settings_tabs.index("Scheduled Tasks"), settings_tabs.index("Storage access"))

    def test_save_persists_retention_and_records_audit_event(self):
        response = self.client.post(self.url, {"run_history_retention_days": "180"})

        self.assertRedirects(response, self.url)
        self.assertEqual(ScheduledActionSettings.objects.get(pk=1).run_history_retention_days, 180)
        event = AuditEvent.objects.get(action="scheduled_action.run_retention.updated")
        self.assertEqual(event.details["retention_days"], 180)
        audit_response = self.client.get(reverse("core:audit_log"))
        self.assertContains(audit_response, "Update scheduled task run retention")

    def test_save_rejects_values_outside_one_to_999(self):
        for value in ("0", "1000", "not-a-number"):
            with self.subTest(value=value):
                response = self.client.post(self.url, {"run_history_retention_days": value})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Run history retention must be between 1 and 999 days.")
                self.assertEqual(ScheduledActionSettings.objects.get(pk=1).run_history_retention_days, 90)

    def test_pruner_uses_saved_retention(self):
        ScheduledActionSettings.objects.create(pk=1, run_history_retention_days=30)
        cluster = ProxmoxCluster.objects.create(key="retention", display_name="Retention", enabled=True)
        action = ScheduledAction.objects.create(
            cluster=cluster,
            name="Retention task",
            action_type=ScheduledAction.ActionType.START,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=500,
            schedule_type=ScheduledAction.ScheduleType.ONCE,
        )
        now = timezone.now()
        run = ScheduledActionRun.objects.create(
            scheduled_action=action,
            planned_for=now - timedelta(days=31),
            occurrence_key="old-run",
            status=ScheduledActionRun.Status.COMPLETED,
            outcome=ScheduledActionRun.Outcome.SUCCESS,
            finished_at=now - timedelta(days=31),
        )

        self.assertEqual(prune_scheduled_action_runs(now=now), 1)
        self.assertFalse(ScheduledActionRun.objects.filter(pk=run.pk).exists())

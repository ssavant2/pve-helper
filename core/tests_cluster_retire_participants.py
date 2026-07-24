"""R2 owner transitions for schedule, console and Audit retirement participants."""

from unittest.mock import patch

from django.db.models.query import QuerySet
from django.test import TestCase
from django.utils import timezone

from core.models import (
    AuditEvent,
    ConsoleSession,
    ProxmoxCluster,
    ScheduledAction,
    ScheduledActionRun,
)
from core.services.audit_events import (
    AuditRetirementBlocked,
    cluster_retirement_audit_preflight,
    finalize_cluster_retirement_audit_operations,
)
from core.services.cluster_lifecycle_registry import (
    CODE_FORCE_RETIRED_UNRESOLVABLE,
    CODE_RETIRED_BEFORE_START,
)
from core.services.console_sessions import (
    ConsoleRetirementBlocked,
    cluster_retirement_console_preflight,
    finalize_cluster_retirement_consoles,
)
from core.services.scheduled_actions import (
    ScheduledActionRetirementBlocked,
    cluster_retirement_scheduled_actions_preflight,
    finalize_cluster_retirement_scheduled_actions,
)


class ClusterRetirementParticipantTests(TestCase):
    """Exercise queued-vs-active behavior against the canonical PostgreSQL DB."""

    def setUp(self):
        self.cluster = ProxmoxCluster.objects.create(key="retiring", display_name="Retiring")
        self.other_cluster = ProxmoxCluster.objects.create(key="managed", display_name="Managed")

    def _action(self, cluster, name):
        return ScheduledAction.objects.create(
            cluster=cluster,
            name=name,
            action_type=ScheduledAction.ActionType.SHUTDOWN,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=100,
            next_run_at=timezone.now(),
        )

    def _run(self, action, status, suffix):
        return ScheduledActionRun.objects.create(
            scheduled_action=action,
            planned_for=timezone.now(),
            occurrence_key=f"run-{suffix}",
            status=status,
        )

    def _console(self, cluster, status, suffix, *, secrets=True):
        return ConsoleSession.objects.create(
            token_hash=f"token-{cluster.key}-{suffix}",
            cluster=cluster,
            target_type=ConsoleSession.TargetType.VM,
            target_vmid=100,
            target_node="pve1",
            expires_at=timezone.now() + timezone.timedelta(minutes=5),
            status=status,
            proxmox_endpoint="https://pve1.example.test:8006" if secrets else "",
            proxmox_ticket="ticket" if secrets else "",
            proxmox_password="password" if secrets else "",
        )

    def _audit(self, cluster, action, outcome, suffix):
        return AuditEvent.objects.create(
            cluster=cluster,
            cluster_key_snapshot=cluster.key,
            action=action,
            object_type="guest",
            object_id=f"guest-{suffix}",
            outcome=outcome,
            details={"original": suffix},
        )

    def test_verified_finalizers_cancel_not_started_and_preserve_history_and_other_cluster(self):
        queued_action = self._action(self.cluster, "Queued")
        queued_run = self._run(queued_action, ScheduledActionRun.Status.QUEUED, "queued")
        terminal_action = self._action(self.cluster, "Terminal")
        terminal_run = self._run(terminal_action, ScheduledActionRun.Status.COMPLETED, "terminal")
        other_action = self._action(self.other_cluster, "Other")
        other_run = self._run(other_action, ScheduledActionRun.Status.QUEUED, "other")

        pending_console = self._console(self.cluster, ConsoleSession.Status.PENDING, "pending")
        terminal_console = self._console(self.cluster, ConsoleSession.Status.FAILED, "terminal")
        other_console = self._console(self.other_cluster, ConsoleSession.Status.PENDING, "other")

        queued_audit = self._audit(self.cluster, "guest.power.start", "queued", "queued")
        terminal_audit = self._audit(self.cluster, "guest.power.start", "success", "terminal")
        config_audit = self._audit(self.cluster, "cluster.disabled", "queued", "config")
        other_audit = self._audit(self.other_cluster, "guest.power.start", "queued", "other")

        schedule_result = finalize_cluster_retirement_scheduled_actions(
            self.cluster,
            mode=ProxmoxCluster.RetirementMode.VERIFIED,
        )
        console_result = finalize_cluster_retirement_consoles(
            self.cluster,
            mode=ProxmoxCluster.RetirementMode.VERIFIED,
        )
        audit_result = finalize_cluster_retirement_audit_operations(
            self.cluster,
            mode=ProxmoxCluster.RetirementMode.VERIFIED,
        )

        self.assertEqual(schedule_result.schedules_deleted, 2)
        self.assertEqual(schedule_result.not_started_runs_cancelled, 1)
        self.assertEqual(schedule_result.active_runs_abandoned, 0)
        queued_action.refresh_from_db()
        terminal_action.refresh_from_db()
        queued_run.refresh_from_db()
        terminal_run.refresh_from_db()
        other_action.refresh_from_db()
        other_run.refresh_from_db()
        self.assertIsNotNone(queued_action.deleted_at)
        self.assertFalse(queued_action.enabled)
        self.assertIsNone(queued_action.next_run_at)
        self.assertIsNotNone(terminal_action.deleted_at)
        self.assertEqual(queued_run.status, ScheduledActionRun.Status.CANCELLED)
        self.assertEqual(queued_run.error, CODE_RETIRED_BEFORE_START)
        self.assertEqual(terminal_run.status, ScheduledActionRun.Status.COMPLETED)
        self.assertIsNone(other_action.deleted_at)
        self.assertEqual(other_run.status, ScheduledActionRun.Status.QUEUED)

        self.assertEqual(console_result.pending_closed, 1)
        self.assertEqual(console_result.active_closed, 0)
        self.assertEqual(console_result.sessions_sanitized, 2)
        pending_console.refresh_from_db()
        terminal_console.refresh_from_db()
        other_console.refresh_from_db()
        self.assertEqual(pending_console.status, ConsoleSession.Status.CLOSED)
        self.assertEqual(pending_console.close_reason, CODE_RETIRED_BEFORE_START)
        self.assertEqual(pending_console.proxmox_ticket, "")
        self.assertEqual(terminal_console.status, ConsoleSession.Status.FAILED)
        self.assertEqual(terminal_console.proxmox_ticket, "")
        self.assertNotEqual(other_console.proxmox_ticket, "")

        self.assertEqual(audit_result.queued_cancelled, 1)
        self.assertEqual(audit_result.running_abandoned, 0)
        queued_audit.refresh_from_db()
        terminal_audit.refresh_from_db()
        config_audit.refresh_from_db()
        other_audit.refresh_from_db()
        self.assertEqual(queued_audit.outcome, "cancelled")
        self.assertEqual(queued_audit.details["retirement_code"], CODE_RETIRED_BEFORE_START)
        self.assertEqual(queued_audit.details["original"], "queued")
        self.assertEqual(terminal_audit.outcome, "success")
        self.assertEqual(config_audit.outcome, "queued")
        self.assertEqual(other_audit.outcome, "queued")

    def test_verified_preflight_and_finalizers_block_active_participants_without_mutation(self):
        action = self._action(self.cluster, "Active")
        run = self._run(action, ScheduledActionRun.Status.POLLING, "active")
        console = self._console(self.cluster, ConsoleSession.Status.CONNECTED, "active")
        event = self._audit(self.cluster, "guest.power.stop", "running", "active")

        schedule_preflight = cluster_retirement_scheduled_actions_preflight(
            self.cluster,
            mode=ProxmoxCluster.RetirementMode.VERIFIED,
        )
        console_preflight = cluster_retirement_console_preflight(
            self.cluster,
            mode=ProxmoxCluster.RetirementMode.VERIFIED,
        )
        audit_preflight = cluster_retirement_audit_preflight(
            self.cluster,
            mode=ProxmoxCluster.RetirementMode.VERIFIED,
        )
        self.assertFalse(schedule_preflight.gate_clear)
        self.assertEqual(schedule_preflight.active_run_count, 1)
        self.assertFalse(console_preflight.gate_clear)
        self.assertEqual(console_preflight.active_count, 1)
        self.assertFalse(audit_preflight.gate_clear)
        self.assertEqual(audit_preflight.running_count, 1)

        with self.assertRaises(ScheduledActionRetirementBlocked):
            finalize_cluster_retirement_scheduled_actions(
                self.cluster,
                mode=ProxmoxCluster.RetirementMode.VERIFIED,
            )
        with self.assertRaises(ConsoleRetirementBlocked):
            finalize_cluster_retirement_consoles(
                self.cluster,
                mode=ProxmoxCluster.RetirementMode.VERIFIED,
            )
        with self.assertRaises(AuditRetirementBlocked):
            finalize_cluster_retirement_audit_operations(
                self.cluster,
                mode=ProxmoxCluster.RetirementMode.VERIFIED,
            )

        action.refresh_from_db()
        run.refresh_from_db()
        console.refresh_from_db()
        event.refresh_from_db()
        self.assertIsNone(action.deleted_at)
        self.assertEqual(run.status, ScheduledActionRun.Status.POLLING)
        self.assertEqual(console.status, ConsoleSession.Status.CONNECTED)
        self.assertEqual(console.proxmox_ticket, "ticket")
        self.assertEqual(event.outcome, "running")

    def test_forced_finalizers_abandon_known_active_participants(self):
        action = self._action(self.cluster, "Forced")
        queued_run = self._run(action, ScheduledActionRun.Status.PREFLIGHT, "queued")
        active_run = self._run(action, ScheduledActionRun.Status.SUBMITTED, "active")
        pending_console = self._console(self.cluster, ConsoleSession.Status.PENDING, "pending")
        active_console = self._console(self.cluster, ConsoleSession.Status.CONNECTING, "active")
        queued_audit = self._audit(self.cluster, "tag.bulk_operation", "queued", "queued")
        active_audit = self._audit(self.cluster, "storage.catalog.refresh", "running", "active")

        schedule_result = finalize_cluster_retirement_scheduled_actions(
            self.cluster,
            mode=ProxmoxCluster.RetirementMode.FORCED,
        )
        console_result = finalize_cluster_retirement_consoles(
            self.cluster,
            mode=ProxmoxCluster.RetirementMode.FORCED,
        )
        audit_result = finalize_cluster_retirement_audit_operations(
            self.cluster,
            mode=ProxmoxCluster.RetirementMode.FORCED,
        )

        self.assertEqual(schedule_result.not_started_runs_cancelled, 1)
        self.assertEqual(schedule_result.active_runs_abandoned, 1)
        queued_run.refresh_from_db()
        active_run.refresh_from_db()
        self.assertEqual(queued_run.error, CODE_FORCE_RETIRED_UNRESOLVABLE)
        self.assertEqual(active_run.error, CODE_FORCE_RETIRED_UNRESOLVABLE)
        self.assertEqual(active_run.status, ScheduledActionRun.Status.CANCELLED)

        self.assertEqual(console_result.pending_closed, 1)
        self.assertEqual(console_result.active_closed, 1)
        pending_console.refresh_from_db()
        active_console.refresh_from_db()
        self.assertEqual(pending_console.close_reason, CODE_RETIRED_BEFORE_START)
        self.assertEqual(active_console.close_reason, CODE_FORCE_RETIRED_UNRESOLVABLE)
        self.assertEqual(active_console.status, ConsoleSession.Status.CLOSED)

        self.assertEqual(audit_result.queued_cancelled, 1)
        self.assertEqual(audit_result.running_abandoned, 1)
        queued_audit.refresh_from_db()
        active_audit.refresh_from_db()
        self.assertEqual(queued_audit.details["retirement_code"], CODE_FORCE_RETIRED_UNRESOLVABLE)
        self.assertEqual(active_audit.details["retirement_code"], CODE_FORCE_RETIRED_UNRESOLVABLE)
        self.assertEqual(active_audit.outcome, "cancelled")

    def test_unknown_participant_types_and_states_block_both_modes(self):
        action = self._action(self.cluster, "Unknown")
        run = self._run(action, "future_state", "unknown")
        console = self._console(self.cluster, "future_state", "unknown")
        event = self._audit(self.cluster, "provider.future_operation", "queued", "unknown-action")
        unknown_outcome = self._audit(self.cluster, "guest.power.start", "future_state", "unknown-outcome")

        with self.assertRaises(ScheduledActionRetirementBlocked) as schedule_error:
            finalize_cluster_retirement_scheduled_actions(
                self.cluster,
                mode=ProxmoxCluster.RetirementMode.FORCED,
            )
        with self.assertRaises(ConsoleRetirementBlocked) as console_error:
            finalize_cluster_retirement_consoles(
                self.cluster,
                mode=ProxmoxCluster.RetirementMode.FORCED,
            )
        with self.assertRaises(AuditRetirementBlocked) as audit_error:
            finalize_cluster_retirement_audit_operations(
                self.cluster,
                mode=ProxmoxCluster.RetirementMode.FORCED,
            )

        self.assertEqual(schedule_error.exception.preflight.unknown_run_count, 1)
        self.assertEqual(console_error.exception.preflight.unknown_count, 1)
        self.assertEqual(audit_error.exception.preflight.unknown_count, 2)
        action.refresh_from_db()
        run.refresh_from_db()
        console.refresh_from_db()
        event.refresh_from_db()
        unknown_outcome.refresh_from_db()
        self.assertIsNone(action.deleted_at)
        self.assertEqual(run.status, "future_state")
        self.assertEqual(console.status, "future_state")
        self.assertEqual(event.outcome, "queued")
        self.assertEqual(unknown_outcome.outcome, "future_state")

    def test_audit_preflight_is_bounded_but_keeps_exact_counts(self):
        AuditEvent.objects.bulk_create(
            [
                AuditEvent(
                    cluster=self.cluster,
                    cluster_key_snapshot=self.cluster.key,
                    action="guest.power.start",
                    object_type="guest",
                    object_id=f"guest-{index}",
                    outcome="queued",
                )
                for index in range(101)
            ]
        )

        with self.assertNumQueries(2):
            preflight = cluster_retirement_audit_preflight(
                self.cluster,
                mode=ProxmoxCluster.RetirementMode.VERIFIED,
            )

        self.assertEqual(preflight.queued_count, 101)
        self.assertEqual(preflight.participant_count, 101)
        self.assertEqual(len(preflight.participants), 100)
        self.assertEqual(preflight.participants_omitted, 1)
        self.assertTrue(preflight.gate_clear)

    def test_mid_finalizer_failures_roll_back_prior_schedule_and_console_updates(self):
        action = self._action(self.cluster, "Rollback")
        run = self._run(action, ScheduledActionRun.Status.QUEUED, "rollback")
        pending = self._console(self.cluster, ConsoleSession.Status.PENDING, "rollback")
        terminal = self._console(self.cluster, ConsoleSession.Status.CLOSED, "terminal-rollback")
        queryset_update = QuerySet.update

        def fail_schedule_run_update(queryset, **updates):
            if queryset.model is ScheduledActionRun:
                raise RuntimeError("injected schedule run failure")
            return queryset_update(queryset, **updates)

        with (
            patch.object(QuerySet, "update", new=fail_schedule_run_update),
            self.assertRaisesRegex(RuntimeError, "injected schedule run failure"),
        ):
            finalize_cluster_retirement_scheduled_actions(
                self.cluster,
                mode=ProxmoxCluster.RetirementMode.VERIFIED,
            )

        action.refresh_from_db()
        run.refresh_from_db()
        self.assertIsNone(action.deleted_at)
        self.assertEqual(run.status, ScheduledActionRun.Status.QUEUED)

        console_updates = 0

        def fail_second_console_update(queryset, **updates):
            nonlocal console_updates
            if queryset.model is ConsoleSession:
                console_updates += 1
                if console_updates == 2:
                    raise RuntimeError("injected console sanitize failure")
            return queryset_update(queryset, **updates)

        with (
            patch.object(QuerySet, "update", new=fail_second_console_update),
            self.assertRaisesRegex(RuntimeError, "injected console sanitize failure"),
        ):
            finalize_cluster_retirement_consoles(
                self.cluster,
                mode=ProxmoxCluster.RetirementMode.VERIFIED,
            )

        pending.refresh_from_db()
        terminal.refresh_from_db()
        self.assertEqual(pending.status, ConsoleSession.Status.PENDING)
        self.assertEqual(pending.proxmox_ticket, "ticket")
        self.assertEqual(terminal.proxmox_ticket, "ticket")

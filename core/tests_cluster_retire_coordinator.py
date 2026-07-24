"""R3 atomic cluster-retirement coordinator."""

import threading
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from core.models import (
    AuditEvent,
    ClusterCredential,
    ClusterStorage,
    ClusterTransportTrust,
    ConsoleSession,
    CurrentGuestInventory,
    CurrentGuestInventoryState,
    LogForwarderConfiguration,
    LogForwardingDelivery,
    ProxmoxCluster,
    ProxmoxEndpoint,
    ProxmoxInventory,
    ScanRun,
    ScheduledAction,
    ScheduledActionRun,
    StorageCatalogState,
)
from core.services.cluster_lifecycle_lock import acquire_operable_cluster
from core.services.cluster_lifecycle_registry import (
    CODE_FORCE_RETIRED_UNRESOLVABLE,
    CODE_RETIRED_BEFORE_START,
)
from core.services.cluster_retirement import (
    ERROR_CODE_RETIREMENT_ACTIVE_SCAN,
    ERROR_CODE_RETIREMENT_CONFIRMATION,
    ClusterRetirementActiveScan,
    ClusterRetirementConfirmationRequired,
    ClusterRetirementFailed,
    cluster_retirement_preflight,
    retire_cluster,
)
from core.tests_cluster_retire_preflight import CA_UUID, IdentityClient


class ClusterRetirementCoordinatorTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(username="retirement-operator")
        self.cluster = ProxmoxCluster.objects.create(
            key="retiring",
            display_name="Retiring",
            enabled=False,
            discovered_ca_uuid=CA_UUID,
            discovered_ca_fingerprint="AA:11",
        )
        self.endpoint = ProxmoxEndpoint.objects.create(
            cluster=self.cluster,
            name="pve1",
            url="https://pve1.retiring.test:8006/",
        )
        ClusterCredential.objects.create(
            cluster=self.cluster,
            token_id="retire@pve!helper",
            token_secret_sealed="sealed-test-secret",
            encryption_key_id="test",
        )
        ClusterTransportTrust.objects.create(
            cluster=self.cluster,
            mode=ClusterTransportTrust.Mode.PUBLIC,
        )

    def _verified_confirmation(self):
        with patch(
            "core.services.cluster_retirement.client_for_endpoint",
            return_value=IdentityClient(),
        ):
            return cluster_retirement_preflight(
                self.cluster,
                mode=ProxmoxCluster.RetirementMode.VERIFIED,
                endpoint_id=self.endpoint.pk,
            ).confirmation

    def _forced_confirmation(self):
        return cluster_retirement_preflight(
            self.cluster,
            mode=ProxmoxCluster.RetirementMode.FORCED,
        ).confirmation

    def _schedule_run(self, status):
        action = ScheduledAction.objects.create(
            cluster=self.cluster,
            name="Retirement fixture",
            action_type=ScheduledAction.ActionType.SHUTDOWN,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=100,
            next_run_at=timezone.now(),
        )
        run = ScheduledActionRun.objects.create(
            scheduled_action=action,
            planned_for=timezone.now(),
            occurrence_key=f"retirement-{status}",
            status=status,
        )
        return action, run

    def _console(self, status):
        return ConsoleSession.objects.create(
            token_hash=f"retirement-console-{status}",
            cluster=self.cluster,
            target_type=ConsoleSession.TargetType.VM,
            target_vmid=100,
            target_node="pve1",
            expires_at=timezone.now() + timezone.timedelta(minutes=5),
            status=status,
            proxmox_endpoint=self.endpoint.normalized_url,
            proxmox_ticket="ticket",
            proxmox_password="password",
        )

    def _provider_audit(self, outcome):
        return AuditEvent.objects.create(
            cluster=self.cluster,
            cluster_key_snapshot=self.cluster.key,
            action="storage.catalog.refresh",
            object_type="cluster",
            object_id=self.cluster.key,
            outcome=outcome,
        )

    def test_verified_retirement_commits_cleanup_audit_outbox_and_on_commit_invalidation(self):
        current = CurrentGuestInventory.objects.create(
            cluster=self.cluster,
            source_endpoint=self.endpoint,
            node="pve1",
            object_type=CurrentGuestInventory.ObjectType.VM,
            vmid=100,
            name="retiring-vm",
            status="stopped",
            observed_at=timezone.now(),
        )
        CurrentGuestInventoryState.objects.create(cluster=self.cluster, complete=True)
        definition = ClusterStorage.objects.create(
            cluster=self.cluster,
            storage_id="local",
            storage_type="dir",
            present=True,
        )
        StorageCatalogState.objects.create(cluster=self.cluster, metadata_complete=True)
        action, run = self._schedule_run(ScheduledActionRun.Status.QUEUED)
        console = self._console(ConsoleSession.Status.PENDING)
        provider_event = self._provider_audit("queued")
        completed_scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        historical_guest = ProxmoxInventory.objects.create(
            scan_run=completed_scan,
            cluster=self.cluster,
            node="pve1",
            object_type=ProxmoxInventory.ObjectType.VM,
            vmid=100,
            name="retiring-vm",
        )
        LogForwarderConfiguration.objects.create(
            pk=1,
            enabled=True,
            host="syslog.example.test",
            port=6514,
        )
        confirmation = self._verified_confirmation()

        with (
            patch(
                "core.services.cluster_retirement.client_for_endpoint",
                side_effect=AssertionError("final retirement must not contact Proxmox"),
            ),
            patch("core.services.cluster_retirement.invalidate_cluster_cache") as invalidate,
            patch("core.services.cluster_retirement.reset_trust_pools") as reset_pools,
            self.captureOnCommitCallbacks(execute=True),
        ):
            result = retire_cluster(
                self.cluster,
                confirmation=confirmation,
                actor=self.actor,
            )

        self.cluster.refresh_from_db()
        action.refresh_from_db()
        run.refresh_from_db()
        console.refresh_from_db()
        provider_event.refresh_from_db()
        definition.refresh_from_db()
        self.assertFalse(self.cluster.enabled)
        self.assertIsNotNone(self.cluster.retired_at)
        self.assertEqual(self.cluster.retirement_mode, ProxmoxCluster.RetirementMode.VERIFIED)
        self.assertEqual(self.cluster.retired_ca_uuid, CA_UUID)
        self.assertEqual(self.cluster.discovered_ca_uuid, "")
        self.assertEqual(self.cluster.lifecycle_generation, 2)
        self.assertFalse(ProxmoxEndpoint.objects.filter(cluster=self.cluster).exists())
        self.assertFalse(ClusterCredential.objects.filter(cluster=self.cluster).exists())
        self.assertFalse(ClusterTransportTrust.objects.filter(cluster=self.cluster).exists())
        self.assertFalse(CurrentGuestInventory.objects.filter(pk=current.pk).exists())
        self.assertFalse(CurrentGuestInventoryState.objects.filter(cluster=self.cluster).exists())
        self.assertIsNotNone(action.deleted_at)
        self.assertEqual(run.status, ScheduledActionRun.Status.CANCELLED)
        self.assertEqual(run.error, CODE_RETIRED_BEFORE_START)
        self.assertEqual(console.status, ConsoleSession.Status.CLOSED)
        self.assertEqual(console.proxmox_ticket, "")
        self.assertEqual(provider_event.outcome, "cancelled")
        self.assertEqual(provider_event.details["retirement_code"], CODE_RETIRED_BEFORE_START)
        self.assertIsNotNone(definition.unmanaged_at)
        self.assertTrue(ProxmoxInventory.objects.filter(pk=historical_guest.pk).exists())
        self.assertEqual(result.cluster_pk, self.cluster.pk)
        invalidate.assert_called_once()
        reset_pools.assert_called_once_with()

        event = AuditEvent.objects.get(pk=result.audit_event_id)
        self.assertEqual(event.action, "cluster.retired")
        self.assertEqual(event.outcome, "success")
        self.assertEqual(event.cluster_id, self.cluster.pk)
        self.assertEqual(event.details["identity_verification"], "matched")
        self.assertEqual(event.details["credential_token_id"], "retire@pve!helper")
        self.assertEqual(event.details["endpoint_count"], 1)
        self.assertNotIn("sealed-test-secret", str(event.details))
        delivery = LogForwardingDelivery.objects.get(audit_event_id=event.pk)
        self.assertEqual(delivery.payload["action"], "cluster.retired")
        self.assertEqual(delivery.payload["cluster"], self.cluster.key)
        self.assertNotIn("details", delivery.payload)
        self.assertNotIn("endpoints", delivery.payload)
        replacement = ProxmoxCluster.objects.create(
            key="replacement",
            display_name="Retiring",
            enabled=False,
            discovered_ca_uuid=CA_UUID,
        )
        replacement_endpoint = ProxmoxEndpoint.objects.create(
            cluster=replacement,
            name="pve1",
            url="https://pve1.retiring.test:8006/",
        )
        self.assertEqual(replacement_endpoint.normalized_url, "https://pve1.retiring.test:8006")

    def test_forced_retirement_disables_enabled_cluster_and_abandons_classified_work(self):
        self.cluster.enabled = True
        self.cluster.save(update_fields=["enabled", "updated_at"])
        action, run = self._schedule_run(ScheduledActionRun.Status.SUBMITTED)
        console = self._console(ConsoleSession.Status.CONNECTING)
        provider_event = self._provider_audit("running")
        confirmation = self._forced_confirmation()

        with (
            patch(
                "core.services.cluster_retirement.client_for_endpoint",
                side_effect=AssertionError("forced retirement must not contact Proxmox"),
            ),
            patch(
                "core.services.cluster_retirement.discover_cluster_identity",
                side_effect=AssertionError("forced retirement must not inspect provider identity"),
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            result = retire_cluster(
                self.cluster,
                confirmation=confirmation,
                actor=self.actor,
                typed_cluster_key=self.cluster.key,
                reason="The decommissioned site cannot return.",
                permanent_unavailability_asserted=True,
            )

        self.cluster.refresh_from_db()
        action.refresh_from_db()
        run.refresh_from_db()
        console.refresh_from_db()
        provider_event.refresh_from_db()
        self.assertFalse(self.cluster.enabled)
        self.assertEqual(self.cluster.retirement_mode, ProxmoxCluster.RetirementMode.FORCED)
        self.assertEqual(self.cluster.retirement_reason, "The decommissioned site cannot return.")
        self.assertIsNotNone(action.deleted_at)
        self.assertEqual(run.status, ScheduledActionRun.Status.CANCELLED)
        self.assertEqual(run.error, CODE_FORCE_RETIRED_UNRESOLVABLE)
        self.assertEqual(console.status, ConsoleSession.Status.CLOSED)
        self.assertEqual(console.close_reason, CODE_FORCE_RETIRED_UNRESOLVABLE)
        self.assertEqual(provider_event.outcome, "cancelled")
        self.assertEqual(provider_event.details["retirement_code"], CODE_FORCE_RETIRED_UNRESOLVABLE)
        event = AuditEvent.objects.get(pk=result.audit_event_id)
        self.assertEqual(event.action, "cluster.force_retired")
        self.assertEqual(event.details["identity_verification"], "skipped")
        self.assertEqual(event.details["cleanup"]["scheduled_runs_abandoned"], 1)
        self.assertEqual(event.details["cleanup"]["consoles_abandoned"], 1)
        self.assertEqual(event.details["cleanup"]["audit_operations_abandoned"], 1)

    def test_forced_retirement_requires_exact_key_reason_and_permanent_unavailability_assertion(self):
        self.cluster.enabled = True
        self.cluster.save(update_fields=["enabled", "updated_at"])
        confirmation = self._forced_confirmation()

        attempts = (
            {"typed_cluster_key": "wrong", "reason": "Decommissioned", "permanent_unavailability_asserted": True},
            {"typed_cluster_key": self.cluster.key, "reason": "", "permanent_unavailability_asserted": True},
            {
                "typed_cluster_key": self.cluster.key,
                "reason": "Decommissioned",
                "permanent_unavailability_asserted": False,
            },
        )
        for kwargs in attempts:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ClusterRetirementConfirmationRequired) as raised:
                    retire_cluster(
                        self.cluster,
                        confirmation=confirmation,
                        actor=self.actor,
                        **kwargs,
                    )
                self.assertEqual(raised.exception.error_code, ERROR_CODE_RETIREMENT_CONFIRMATION)
                self.cluster.refresh_from_db()
                self.assertTrue(self.cluster.enabled)
                self.assertIsNone(self.cluster.retired_at)
                self.assertTrue(ProxmoxEndpoint.objects.filter(cluster=self.cluster).exists())

        refusals = AuditEvent.objects.filter(action="cluster.retirement_refused").order_by("pk")
        self.assertEqual(refusals.count(), 3)
        self.assertEqual(
            {event.details["reason_code"] for event in refusals},
            {ERROR_CODE_RETIREMENT_CONFIRMATION},
        )

    def test_active_scan_created_after_preflight_blocks_before_mutation_and_records_refusal(self):
        confirmation = self._verified_confirmation()
        scan = ScanRun.objects.create(status=ScanRun.Status.RUNNING)

        with self.assertRaises(ClusterRetirementActiveScan) as raised:
            retire_cluster(
                self.cluster,
                confirmation=confirmation,
                actor=self.actor,
            )

        self.assertEqual(raised.exception.error_code, ERROR_CODE_RETIREMENT_ACTIVE_SCAN)
        self.cluster.refresh_from_db()
        scan.refresh_from_db()
        self.assertIsNone(self.cluster.retired_at)
        self.assertEqual(scan.status, ScanRun.Status.RUNNING)
        refusal = AuditEvent.objects.get(action="cluster.retirement_refused")
        self.assertEqual(refusal.details["reason_code"], "active_scan")

    def test_mid_coordinator_failure_rolls_back_all_cleanup_and_success_outbox(self):
        action, run = self._schedule_run(ScheduledActionRun.Status.QUEUED)
        LogForwarderConfiguration.objects.create(
            pk=1,
            enabled=True,
            host="syslog.example.test",
            port=6514,
        )
        confirmation = self._verified_confirmation()

        with (
            patch(
                "core.services.cluster_retirement.finalize_cluster_retirement_storage",
                side_effect=RuntimeError("sensitive injected failure"),
            ),
            self.assertRaises(ClusterRetirementFailed) as raised,
        ):
            retire_cluster(
                self.cluster,
                confirmation=confirmation,
                actor=self.actor,
            )

        self.assertNotIn("sensitive", str(raised.exception))
        self.cluster.refresh_from_db()
        action.refresh_from_db()
        run.refresh_from_db()
        self.assertIsNone(self.cluster.retired_at)
        self.assertIsNone(action.deleted_at)
        self.assertEqual(run.status, ScheduledActionRun.Status.QUEUED)
        self.assertTrue(ProxmoxEndpoint.objects.filter(cluster=self.cluster).exists())
        self.assertTrue(ClusterCredential.objects.filter(cluster=self.cluster).exists())
        self.assertFalse(AuditEvent.objects.filter(action="cluster.retired").exists())
        self.assertFalse(AuditEvent.objects.filter(action="cluster.force_retired").exists())
        refusal = AuditEvent.objects.get(action="cluster.retirement_refused")
        self.assertEqual(refusal.details["reason_code"], "cluster_retirement_failed")
        delivery = LogForwardingDelivery.objects.get()
        self.assertEqual(delivery.audit_event_id, refusal.pk)
        self.assertEqual(delivery.payload["action"], "cluster.retirement_refused")


class ClusterRetirementCoordinatorConcurrencyTests(TransactionTestCase):
    databases = {"default"}

    def test_provider_acquisition_and_retirement_serialize_on_the_lifecycle_lock(self):
        if connection.vendor != "postgresql":
            self.skipTest("advisory-lock serialisation only holds on PostgreSQL")
        actor = get_user_model().objects.create_user(username="concurrent-retirement-operator")
        cluster = ProxmoxCluster.objects.create(
            key="concurrent-retirement",
            display_name="Concurrent retirement",
            enabled=True,
            discovered_ca_uuid=CA_UUID,
            discovered_ca_fingerprint="AA:11",
        )
        ProxmoxEndpoint.objects.create(
            cluster=cluster,
            name="pve1",
            url="https://pve1.concurrent-retirement.test:8006/",
        )
        ClusterCredential.objects.create(
            cluster=cluster,
            token_id="retire@pve!helper",
            token_secret_sealed="sealed-test-secret",
            encryption_key_id="test",
        )
        ClusterTransportTrust.objects.create(
            cluster=cluster,
            mode=ClusterTransportTrust.Mode.PUBLIC,
        )
        confirmation = cluster_retirement_preflight(
            cluster,
            mode=ProxmoxCluster.RetirementMode.FORCED,
        ).confirmation
        acquired = threading.Event()
        release = threading.Event()
        retirement_result: dict[str, object] = {}

        def holder():
            try:
                with transaction.atomic():
                    acquire_operable_cluster(cluster)
                    acquired.set()
                    release.wait(timeout=10)
            finally:
                connection.close()

        def retire():
            try:
                retirement_result["result"] = retire_cluster(
                    cluster,
                    confirmation=confirmation,
                    actor=actor,
                    typed_cluster_key=cluster.key,
                    reason="The decommissioned site cannot return.",
                    permanent_unavailability_asserted=True,
                )
            except Exception as exc:
                retirement_result["error"] = exc
            finally:
                connection.close()

        holder_thread = threading.Thread(target=holder)
        holder_thread.start()
        self.assertTrue(acquired.wait(timeout=10))

        retirement_thread = threading.Thread(target=retire)
        retirement_thread.start()
        retirement_thread.join(timeout=1)
        self.assertTrue(
            retirement_thread.is_alive(),
            "retirement must block behind a provider acquisition holding the lifecycle lock",
        )

        release.set()
        holder_thread.join(timeout=10)
        retirement_thread.join(timeout=10)
        self.assertFalse(retirement_thread.is_alive())
        self.assertNotIn("error", retirement_result)
        cluster.refresh_from_db()
        self.assertIsNotNone(cluster.retired_at)

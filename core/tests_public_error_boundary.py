"""The public exception boundary, on the paths that persist and render failures.

Review 10 found the contract documented but unenforced outside the request path:
workers and the console gateway wrote `str(exc)` straight into `AuditEvent`,
`ConsoleSession` and `ScanRun`, and Recent Tasks then made a product decision by
substring-matching the text it had stored. The source invariants in
`tests_source_invariants.PublicErrorBoundaryInvariantTests` keep new call sites
from reintroducing that; these tests pin what the boundary actually produces.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from core.models import AuditEvent, ProxmoxCluster, ProxmoxEndpoint
from core.services.durable_guest_operations import DurableGuestOperationError
from core.services.proxmox import ProxmoxAPIError, ProxmoxTaskTimeout
from core.services.public_errors import (
    ERROR_CODE_DOMAIN,
    ERROR_CODE_POWERDOWN_FAILED,
    ERROR_CODE_PROVIDER,
    ERROR_CODE_TASK_FAILED,
    ERROR_CODE_TASK_TIMEOUT,
    ERROR_CODE_UNEXPECTED,
    PublicMessageError,
    proxmox_task_failure,
    public_failure,
)
from core.services.recent_tasks import recent_task_page
from core.services.storage_actions import StorageActionError
from core.services.task_failures import record_event_exception

SECRET = "https://pve1.internal:8006/api2/json/nodes/pve1/qemu/107/status/shutdown"


class PublicFailureTests(TestCase):
    def test_a_domain_error_keeps_the_message_its_raise_site_wrote(self):
        failure = public_failure(
            StorageActionError("Target file already exists."),
            operation="test",
            fallback="should not be used",
        )
        self.assertEqual(failure.message, "Target file already exists.")
        self.assertEqual(failure.code, ERROR_CODE_DOMAIN)

    def test_a_provider_error_is_replaced_by_caller_owned_text(self):
        failure = public_failure(
            ProxmoxAPIError(f"500 Internal Server Error from {SECRET}"),
            operation="test",
            fallback="The Proxmox request failed.",
            code=ERROR_CODE_PROVIDER,
        )
        self.assertEqual(failure.message, "The Proxmox request failed.")
        self.assertEqual(failure.code, ERROR_CODE_PROVIDER)
        self.assertNotIn("pve1.internal", failure.message)

    def test_an_unexpected_exception_defaults_to_the_unexpected_code(self):
        failure = public_failure(RuntimeError(SECRET), operation="test", fallback="It failed.")
        self.assertEqual(failure.code, ERROR_CODE_UNEXPECTED)
        self.assertNotIn("pve1.internal", failure.message)

    def test_the_marker_classifies_the_exception_type(self):
        self.assertIsInstance(StorageActionError("x"), PublicMessageError)
        self.assertIsInstance(DurableGuestOperationError("x"), PublicMessageError)
        self.assertNotIsInstance(ProxmoxAPIError("x"), PublicMessageError)

    def test_a_completed_proxmox_task_is_classified_by_code_not_by_prose(self):
        self.assertEqual(proxmox_task_failure("VM quit/powerdown failed - got timeout").code, ERROR_CODE_TASK_TIMEOUT)
        self.assertEqual(proxmox_task_failure("powerdown failed").code, ERROR_CODE_POWERDOWN_FAILED)
        self.assertEqual(proxmox_task_failure("", "stopped").code, ERROR_CODE_TASK_FAILED)
        self.assertEqual(proxmox_task_failure().code, ERROR_CODE_TASK_FAILED)
        # Whatever the provider said, it is not what the operator is shown.
        self.assertNotIn("powerdown", proxmox_task_failure("powerdown failed").message.lower()[:20])


class RecordEventFailureTests(TestCase):
    def _event(self, **details):
        return AuditEvent.objects.create(
            action="guest.power.shutdown",
            outcome="running",
            object_type="guest",
            object_id="vm:107",
            details=details,
        )

    def test_it_writes_prose_a_code_and_a_finish_time_in_one_step(self):
        event = self._event(vmid=107, target_type="vm")

        record_event_exception(
            event,
            ProxmoxAPIError(SECRET),
            operation="test",
            fallback="The Proxmox request failed.",
            code=ERROR_CODE_PROVIDER,
        )

        event.refresh_from_db()
        self.assertEqual(event.outcome, "failed")
        self.assertEqual(event.details["error"], "The Proxmox request failed.")
        self.assertEqual(event.details["error_code"], ERROR_CODE_PROVIDER)
        self.assertIn("finished_at", event.details)
        # The keys the payload already carried are not dropped on the way.
        self.assertEqual(event.details["vmid"], 107)

    def test_no_persisted_field_repeats_the_exception_text(self):
        event = self._event()

        record_event_exception(
            event,
            ProxmoxTaskTimeout(f"timed out waiting for {SECRET}"),
            operation="test",
            fallback="The Proxmox task did not finish before its timeout.",
            code=ERROR_CODE_TASK_TIMEOUT,
        )

        event.refresh_from_db()
        self.assertNotIn("pve1.internal", str(event.details))


class ForceStopOfferTests(TestCase):
    """Recent Tasks decides on `error_code`, with a tail for pre-code rows."""

    def setUp(self):
        self.cluster = ProxmoxCluster.objects.create(key="alpha", display_name="Alpha", enabled=True)
        ProxmoxEndpoint.objects.create(cluster=self.cluster, name="pve1", url="https://pve1:8006/api2/json")

    def _shutdown(self, **details):
        payload = {"target_type": "vm", "vmid": 107, "node": "pve1", "name": "web"}
        payload.update(details)
        return AuditEvent.objects.create(
            action="guest.power.shutdown",
            outcome="failed",
            object_type="guest",
            object_id="vm:107",
            cluster=self.cluster,
            cluster_key_snapshot="alpha",
            timestamp=timezone.now(),
            details=payload,
        )

    def _question_kinds(self):
        page = recent_task_page(page=0, limit=25)
        return [(row.get("question") or {}).get("kind") for row in page.tasks]

    def test_the_offer_survives_prose_that_no_longer_says_timeout(self):
        self._shutdown(
            error="The guest did not shut down. It may have no ACPI handler or no running QEMU guest agent.",
            error_code=ERROR_CODE_POWERDOWN_FAILED,
        )
        self.assertIn("force_stop", self._question_kinds())

    def test_a_timeout_code_also_offers_it(self):
        self._shutdown(error="The Proxmox task did not finish before its timeout.", error_code=ERROR_CODE_TASK_TIMEOUT)
        self.assertIn("force_stop", self._question_kinds())

    def test_prose_alone_cannot_conjure_the_offer_once_a_code_exists(self):
        # A different failure whose public text happens to contain the word: the
        # code is authoritative, so this is not a force-stop question.
        self._shutdown(error="The request timeout was rejected by Proxmox.", error_code=ERROR_CODE_PROVIDER)
        self.assertNotIn("force_stop", self._question_kinds())

    def test_rows_written_before_codes_existed_still_get_the_offer(self):
        self._shutdown(error="VM quit/powerdown failed - got timeout")
        self.assertIn("force_stop", self._question_kinds())


class WorkerFailurePayloadTests(TestCase):
    def setUp(self):
        self.cluster = ProxmoxCluster.objects.create(key="alpha", display_name="Alpha", enabled=True)
        ProxmoxEndpoint.objects.create(cluster=self.cluster, name="pve1", url="https://pve1:8006/api2/json")

    def test_a_poll_that_times_out_stores_public_text_and_a_timeout_code(self):
        from core import tasks

        event = AuditEvent.objects.create(
            action="guest.power.shutdown",
            outcome="running",
            object_type="guest",
            object_id="vm:107",
            cluster=self.cluster,
            cluster_key_snapshot="alpha",
            details={
                "guest_ref": "alpha/vm:107@pve1",
                "target_type": "vm",
                "vmid": 107,
                "proxmox_task_node": "pve1",
                "proxmox_task_upid": "UPID:pve1:0000:shutdown",
            },
        )

        class _Client:
            endpoint = "https://pve1:8006/api2/json"

            def wait_for_task(self, **_kwargs):
                raise ProxmoxTaskTimeout(f"timed out waiting for {SECRET}")

        with (
            patch.object(
                tasks,
                "_durable_or_legacy_guest_operation",
                return_value=(_Client(), _Ref(), self.cluster),
            ),
            patch.object(tasks, "clear_live_guest_caches"),
        ):
            tasks.poll_guest_audit_task(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.outcome, "failed")
        self.assertEqual(event.details["error_code"], ERROR_CODE_TASK_TIMEOUT)
        self.assertNotIn("pve1.internal", str(event.details))
        # And the failure is still recognised as one force-stop can answer.
        self.assertIn("force_stop", [(row.get("question") or {}).get("kind") for row in recent_task_page().tasks])

    def test_an_unresolvable_target_keeps_its_domain_message(self):
        from core import tasks

        event = AuditEvent.objects.create(
            action="guest.power.shutdown",
            outcome="running",
            object_type="guest",
            object_id="vm:107",
            details={},
        )

        tasks.poll_guest_audit_task(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.outcome, "failed")
        self.assertEqual(event.details["error"], "Audit event has no cluster-qualified guest target.")
        self.assertEqual(event.details["error_code"], ERROR_CODE_DOMAIN)


class _Ref:
    object_type = "vm"
    vmid = 107
    node = "pve1"
    cluster_key = "alpha"

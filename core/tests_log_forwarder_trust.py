"""Trust-on-first-use for the syslog destination, exercised against real TLS.

These tests stand up an actual TLS listener on loopback with certificates minted
in-process, rather than mocking `ssl`. The defect being fixed was entirely about
what `ssl` does with a trust store — a mocked handshake would have passed just as
happily against the broken code, which is the one outcome a regression test for
this must not allow.
"""

from __future__ import annotations

import socket
import ssl
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django_q.models import Schedule

from core.models import AuditEvent, LogForwarderTransportTrust
from core.services.log_forwarder_certificate_watch import (
    CERTIFICATE_QUESTION_ACTION,
    CERTIFICATE_WATCH_SCHEDULE_NAME,
    CONDITION_CHANGED,
    CONDITION_EXPIRING,
    check_destination_certificate,
)
from core.services.log_forwarder_trust import (
    LogForwarderTrustError,
    ambient_ca_bundle,
    approve_destination,
    delivery_context,
    inspect_destination,
    trust,
    trust_applies_to,
)
from core.services.log_forwarding import configuration, deliver_pending_log_events, update_configuration

HOST = "localhost"


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def issue_ca(common_name: str = "pve-helper test CA"):
    key = _key()
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(_name(common_name))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, certificate


def issue_leaf(ca_key, ca_certificate, *, days_valid: int = 365, common_name: str = HOST):
    key = _key()
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(ca_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=days_valid))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(common_name)]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return key, certificate


def issue_self_signed(*, days_valid: int = 365, common_name: str = HOST):
    key = _key()
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(_name(common_name))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=days_valid))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(common_name)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, certificate


class TlsCollector:
    """A throwaway syslog-shaped TLS listener, one connection at a time.

    Serves the chain it is given so the tests can exercise the real difference
    between a bare leaf and a leaf plus issuer — which is exactly what decides
    whether CA mode is offered at all.
    """

    def __init__(self, key, chain):
        self.received: list[bytes] = []
        self._directory = tempfile.TemporaryDirectory()
        directory = Path(self._directory.name)
        certificate_path = directory / "chain.pem"
        key_path = directory / "key.pem"
        certificate_path.write_bytes(b"".join(c.public_bytes(serialization.Encoding.PEM) for c in chain))
        key_path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(certificate_path, key_path)
        self._socket = socket.create_server((HOST, 0))
        self._socket.settimeout(5)
        self.port = self._socket.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                connection, _address = self._socket.accept()
            except (TimeoutError, OSError):
                continue
            try:
                with self._context.wrap_socket(connection, server_side=True) as tls:
                    tls.settimeout(2)
                    data = tls.recv(65535)
                    if data:
                        self.received.append(data)
            except (OSError, ssl.SSLError):
                # A probe that only reads the certificate and hangs up is the
                # normal case here, not a failure.
                pass

    def close(self):
        self._stop.set()
        try:
            self._socket.close()
        except OSError:
            pass
        self._thread.join(timeout=5)
        self._directory.cleanup()


def approve_test_destination(host: str, port: int, *, mode: str = "insecure") -> LogForwarderTransportTrust:
    """Stamp an approval without a live destination, for tests about other things."""
    record = trust()
    record.mode = mode
    record.host = host
    record.port = port
    record.approved_at = timezone.now()
    record.save()
    return record


class InspectionTests(TestCase):
    def test_an_internal_ca_certificate_is_inspectable_and_reports_its_chain(self):
        ca_key, ca_certificate = issue_ca()
        leaf_key, leaf = issue_leaf(ca_key, ca_certificate)
        collector = TlsCollector(leaf_key, [leaf, ca_certificate])
        self.addCleanup(collector.close)

        inspection = inspect_destination(HOST, collector.port)

        self.assertEqual(inspection.sha256_fingerprint, leaf.fingerprint(hashes.SHA256()).hex())
        self.assertFalse(inspection.self_signed)
        self.assertTrue(inspection.ca_available)
        # The whole point of the finding: an internal CA is not in the system store.
        self.assertFalse(inspection.system_trusted)
        self.assertTrue(inspection.verification_error)

    def test_a_bare_leaf_offers_no_issuer_to_trust(self):
        ca_key, ca_certificate = issue_ca()
        leaf_key, leaf = issue_leaf(ca_key, ca_certificate)
        collector = TlsCollector(leaf_key, [leaf])
        self.addCleanup(collector.close)

        inspection = inspect_destination(HOST, collector.port)

        self.assertFalse(inspection.ca_available)
        with self.assertRaises(LogForwarderTrustError):
            approve_destination(mode="ca", host=HOST, port=collector.port, inspection=inspection)

    def test_a_self_signed_certificate_is_its_own_issuer(self):
        key, certificate = issue_self_signed()
        collector = TlsCollector(key, [certificate])
        self.addCleanup(collector.close)

        inspection = inspect_destination(HOST, collector.port)

        self.assertTrue(inspection.self_signed)
        self.assertTrue(inspection.ca_available)

    def test_an_unreachable_destination_is_a_public_error_not_a_traceback(self):
        with self.assertRaises(LogForwarderTrustError) as raised:
            inspect_destination(HOST, 1)
        self.assertIn(
            "could not be reached",
            raised.exception.public_message.lower().replace("could not reach", "could not be reached"),
        )

    def test_the_mounted_ca_bundle_is_read_where_ssl_would_not_read_it(self):
        _ca_key, ca_certificate = issue_ca()
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "ca-bundle.pem"
            bundle.write_bytes(ca_certificate.public_bytes(serialization.Encoding.PEM))
            with patch.dict("os.environ", {"REQUESTS_CA_BUNDLE": str(bundle)}, clear=False):
                self.assertIn("BEGIN CERTIFICATE", ambient_ca_bundle())


class TrustModeTests(TestCase):
    def test_ca_mode_accepts_a_renewal_from_the_same_issuer(self):
        """The behaviour the "always trust this host" answer is actually for."""
        ca_key, ca_certificate = issue_ca()
        first_key, first = issue_leaf(ca_key, ca_certificate)
        collector = TlsCollector(first_key, [first, ca_certificate])
        self.addCleanup(collector.close)
        inspection = inspect_destination(HOST, collector.port)
        record = approve_destination(mode="ca", host=HOST, port=collector.port, inspection=inspection)
        collector.close()

        renewed_key, renewed = issue_leaf(ca_key, ca_certificate)
        renewed_collector = TlsCollector(renewed_key, [renewed, ca_certificate])
        self.addCleanup(renewed_collector.close)

        # A different certificate, same issuer: it must verify without asking again.
        self.assertNotEqual(renewed.fingerprint(hashes.SHA256()).hex(), inspection.sha256_fingerprint)
        with socket.create_connection((HOST, renewed_collector.port), timeout=5) as connection:
            with delivery_context(record).wrap_socket(connection, server_hostname=HOST) as tls:
                self.assertTrue(tls.getpeercert())

    def test_ca_mode_rejects_a_certificate_from_a_different_issuer(self):
        ca_key, ca_certificate = issue_ca()
        leaf_key, leaf = issue_leaf(ca_key, ca_certificate)
        collector = TlsCollector(leaf_key, [leaf, ca_certificate])
        self.addCleanup(collector.close)
        inspection = inspect_destination(HOST, collector.port)
        record = approve_destination(mode="ca", host=HOST, port=collector.port, inspection=inspection)
        collector.close()

        other_key, other_ca = issue_ca("someone else")
        impostor_key, impostor = issue_leaf(other_key, other_ca)
        impostor_collector = TlsCollector(impostor_key, [impostor, other_ca])
        self.addCleanup(impostor_collector.close)

        with self.assertRaises(ssl.SSLError):
            with socket.create_connection((HOST, impostor_collector.port), timeout=5) as connection:
                delivery_context(record).wrap_socket(connection, server_hostname=HOST)

    def test_pinned_mode_reports_a_renewal_from_the_same_issuer_as_a_change(self):
        """`load_verify_locations` alone would silently accept it; pinning must not."""
        key, certificate = issue_self_signed()
        collector = TlsCollector(key, [certificate])
        self.addCleanup(collector.close)
        inspection = inspect_destination(HOST, collector.port)
        record = approve_destination(mode="pinned", host=HOST, port=collector.port, inspection=inspection)
        collector.close()

        renewed_key, renewed = issue_self_signed()
        renewed_collector = TlsCollector(renewed_key, [renewed])
        self.addCleanup(renewed_collector.close)

        with self.assertRaises((LogForwarderTrustError, ssl.SSLError)):
            with socket.create_connection((HOST, renewed_collector.port), timeout=5) as connection:
                with delivery_context(record).wrap_socket(connection, server_hostname=HOST) as tls:
                    from core.services.log_forwarder_trust import assert_pinned_match

                    assert_pinned_match(record, tls.getpeercert(binary_form=True))

    def test_an_unapproved_destination_refuses_rather_than_borrowing_ambient_trust(self):
        record = trust()
        with self.assertRaises(LogForwarderTrustError):
            delivery_context(record)

    def test_repointing_the_forwarder_drops_the_previous_approval(self):
        key, certificate = issue_self_signed()
        collector = TlsCollector(key, [certificate])
        self.addCleanup(collector.close)
        inspection = inspect_destination(HOST, collector.port)
        approve_destination(mode="pinned", host=HOST, port=collector.port, inspection=inspection)

        update_configuration(enabled=True, host="other.example.test", port=6514, transport="tls")

        record = trust()
        self.assertEqual(record.mode, LogForwarderTransportTrust.Mode.UNSET)
        self.assertFalse(trust_applies_to(record, "other.example.test", 6514))


class DeliveryTests(TestCase):
    def test_tls_delivery_succeeds_against_an_internal_ca_once_approved(self):
        """The finding in one test: this was impossible before, at any setting."""
        ca_key, ca_certificate = issue_ca()
        leaf_key, leaf = issue_leaf(ca_key, ca_certificate)
        collector = TlsCollector(leaf_key, [leaf, ca_certificate])
        self.addCleanup(collector.close)
        update_configuration(enabled=True, host=HOST, port=collector.port, transport="tls")
        approve_destination(
            mode="ca",
            host=HOST,
            port=collector.port,
            inspection=inspect_destination(HOST, collector.port),
        )

        from core.services.audit_events import record_audit_event

        record_audit_event(action="test.delivery", outcome="success")
        delivered = deliver_pending_log_events()

        self.assertEqual(delivered, 1)
        self.assertTrue(collector.received)
        self.assertEqual(configuration().last_error_code, "")

    def test_an_unapproved_tls_destination_names_the_trust_failure(self):
        ca_key, ca_certificate = issue_ca()
        leaf_key, leaf = issue_leaf(ca_key, ca_certificate)
        collector = TlsCollector(leaf_key, [leaf, ca_certificate])
        self.addCleanup(collector.close)
        update_configuration(enabled=True, host=HOST, port=collector.port, transport="tls")

        from core.services.audit_events import record_audit_event

        record_audit_event(action="test.delivery", outcome="success")
        delivered = deliver_pending_log_events()

        self.assertEqual(delivered, 0)
        # Not `syslog_connection_failed`: the collector answered fine.
        self.assertEqual(configuration().last_error_code, "syslog_tls_not_approved")

    def test_a_verification_failure_is_named_apart_from_an_unreachable_collector(self):
        ca_key, ca_certificate = issue_ca()
        leaf_key, leaf = issue_leaf(ca_key, ca_certificate)
        collector = TlsCollector(leaf_key, [leaf, ca_certificate])
        self.addCleanup(collector.close)
        update_configuration(enabled=True, host=HOST, port=collector.port, transport="tls")
        approve_destination(
            mode="ca",
            host=HOST,
            port=collector.port,
            inspection=inspect_destination(HOST, collector.port),
        )
        collector.close()

        other_key, other_ca = issue_ca("someone else")
        impostor_key, impostor = issue_leaf(other_key, other_ca)
        impostor = TlsCollector(impostor_key, [impostor, other_ca])
        self.addCleanup(impostor.close)
        config = configuration()
        config.port = impostor.port
        config.save(update_fields=["port"])
        approve_test = trust()
        approve_test.port = impostor.port
        approve_test.save(update_fields=["port"])

        from core.services.audit_events import record_audit_event

        record_audit_event(action="test.delivery", outcome="success")
        deliver_pending_log_events()

        self.assertEqual(configuration().last_error_code, "syslog_tls_verification_failed")


class CertificateWatchTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("operator", password="x" * 14)
        self.client.force_login(self.user)

    def test_a_healthy_certificate_files_no_question(self):
        ca_key, ca_certificate = issue_ca()
        leaf_key, leaf = issue_leaf(ca_key, ca_certificate)
        collector = TlsCollector(leaf_key, [leaf, ca_certificate])
        self.addCleanup(collector.close)
        update_configuration(enabled=True, host=HOST, port=collector.port, transport="tls")
        approve_destination(
            mode="ca", host=HOST, port=collector.port, inspection=inspect_destination(HOST, collector.port)
        )

        result = check_destination_certificate()

        self.assertEqual(result["condition"], "")
        self.assertFalse(AuditEvent.objects.filter(action=CERTIFICATE_QUESTION_ACTION).exists())
        self.assertIsNotNone(trust().last_checked_at)

    def test_a_certificate_within_the_warning_window_raises_one_answerable_question(self):
        key, certificate = issue_self_signed(days_valid=3)
        collector = TlsCollector(key, [certificate])
        self.addCleanup(collector.close)
        update_configuration(enabled=True, host=HOST, port=collector.port, transport="tls")
        approve_destination(
            mode="pinned", host=HOST, port=collector.port, inspection=inspect_destination(HOST, collector.port)
        )

        check_destination_certificate()

        event = AuditEvent.objects.get(action=CERTIFICATE_QUESTION_ACTION)
        self.assertEqual(event.details["condition"], CONDITION_EXPIRING)
        self.assertTrue(event.details["question"])
        self.assertIn("Expires in", event.details["detail"])

        # The finding is one question, not one per daily run.
        check_destination_certificate()
        self.assertEqual(AuditEvent.objects.filter(action=CERTIFICATE_QUESTION_ACTION).count(), 1)

    def test_the_question_appears_in_recent_tasks_and_can_be_answered(self):
        key, certificate = issue_self_signed(days_valid=3)
        collector = TlsCollector(key, [certificate])
        self.addCleanup(collector.close)
        update_configuration(enabled=True, host=HOST, port=collector.port, transport="tls")
        approve_destination(
            mode="pinned", host=HOST, port=collector.port, inspection=inspect_destination(HOST, collector.port)
        )
        check_destination_certificate()
        event = AuditEvent.objects.get(action=CERTIFICATE_QUESTION_ACTION)

        page = self.client.get(reverse("core:recent_tasks"))
        payload = page.json()
        row = next(task for task in payload["tasks"] if task["id"] == f"log_forwarder:{event.id}")
        self.assertEqual(row["question"]["kind"], "log_forwarder_certificate")
        self.assertGreaterEqual(payload["questions_pending"], 1)

        answered = self.client.post(
            reverse("core:dismiss_task_question"),
            {"task_id": f"log_forwarder:{event.id}", "answer": "acknowledged"},
        )

        self.assertEqual(answered.status_code, 200)
        event.refresh_from_db()
        self.assertTrue(event.details["question_dismissed"])
        self.assertTrue(
            AuditEvent.objects.filter(
                action="log_forwarder.certificate.answered", details__answer="acknowledged"
            ).exists()
        )

    def test_a_replaced_certificate_is_reported_as_a_change_under_pinning(self):
        key, certificate = issue_self_signed()
        collector = TlsCollector(key, [certificate])
        self.addCleanup(collector.close)
        port = collector.port
        update_configuration(enabled=True, host=HOST, port=port, transport="tls")
        approve_destination(mode="pinned", host=HOST, port=port, inspection=inspect_destination(HOST, port))
        collector.close()

        # Same port, different key: what a re-keyed collector looks like.
        replacement_key, replacement = issue_self_signed()
        replacement_collector = TlsCollector(replacement_key, [replacement])
        self.addCleanup(replacement_collector.close)
        config = configuration()
        config.port = replacement_collector.port
        config.save(update_fields=["port"])
        record = trust()
        record.port = replacement_collector.port
        record.save(update_fields=["port"])

        check_destination_certificate()

        event = AuditEvent.objects.get(action=CERTIFICATE_QUESTION_ACTION)
        self.assertEqual(event.details["condition"], CONDITION_CHANGED)

    def test_a_resolved_finding_stops_pulsing_without_a_second_click(self):
        key, certificate = issue_self_signed(days_valid=3)
        collector = TlsCollector(key, [certificate])
        self.addCleanup(collector.close)
        update_configuration(enabled=True, host=HOST, port=collector.port, transport="tls")
        approve_destination(
            mode="pinned", host=HOST, port=collector.port, inspection=inspect_destination(HOST, collector.port)
        )
        check_destination_certificate()
        collector.close()

        healthy_key, healthy = issue_self_signed(days_valid=365)
        healthy_collector = TlsCollector(healthy_key, [healthy])
        self.addCleanup(healthy_collector.close)
        config = configuration()
        config.port = healthy_collector.port
        config.save(update_fields=["port"])
        approve_destination(
            mode="pinned",
            host=HOST,
            port=healthy_collector.port,
            inspection=inspect_destination(HOST, healthy_collector.port),
        )

        check_destination_certificate()

        event = AuditEvent.objects.get(action=CERTIFICATE_QUESTION_ACTION)
        self.assertTrue(event.details["question_dismissed"])

    def test_the_daily_schedule_follows_tls_being_enabled(self):
        update_configuration(enabled=True, host="siem.example.test", port=6514, transport="tls")
        schedule = Schedule.objects.get(name=CERTIFICATE_WATCH_SCHEDULE_NAME)
        self.assertEqual(schedule.schedule_type, Schedule.DAILY)

        update_configuration(enabled=True, host="siem.example.test", port=6514, transport="tcp")
        self.assertFalse(Schedule.objects.filter(name=CERTIFICATE_WATCH_SCHEDULE_NAME).exists())


class ApprovalEndpointTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("operator", password="x" * 14)
        self.client.force_login(self.user)

    def test_inspect_returns_what_the_modal_needs_to_ask_the_question(self):
        ca_key, ca_certificate = issue_ca()
        leaf_key, leaf = issue_leaf(ca_key, ca_certificate)
        collector = TlsCollector(leaf_key, [leaf, ca_certificate])
        self.addCleanup(collector.close)

        response = self.client.post(
            reverse("core:settings_log_forwarder_inspect"), {"host": HOST, "port": collector.port}
        )

        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["certificate"]["sha256_fingerprint"], leaf.fingerprint(hashes.SHA256()).hex())
        self.assertTrue(payload["certificate"]["ca_available"])
        self.assertFalse(payload["certificate"]["system_trusted"])
        self.assertIsNone(payload["current"])

    def test_approving_records_the_decision_and_who_made_it(self):
        key, certificate = issue_self_signed()
        collector = TlsCollector(key, [certificate])
        self.addCleanup(collector.close)

        response = self.client.post(
            reverse("core:settings_log_forwarder_approve"),
            {
                "host": HOST,
                "port": collector.port,
                "mode": "pinned",
                "fingerprint": certificate.fingerprint(hashes.SHA256()).hex(),
            },
        )

        self.assertTrue(response.json()["ok"])
        record = trust()
        self.assertEqual(record.mode, LogForwarderTransportTrust.Mode.PINNED)
        self.assertEqual(record.approved_by, "operator")
        event = AuditEvent.objects.get(action="log_forwarder.certificate.approved")
        self.assertEqual(event.details["mode"], "pinned")

    def test_approval_is_refused_when_the_shown_certificate_is_no_longer_served(self):
        """The operator approved what they were shown, not whatever answers next."""
        key, certificate = issue_self_signed()
        collector = TlsCollector(key, [certificate])
        self.addCleanup(collector.close)

        response = self.client.post(
            reverse("core:settings_log_forwarder_approve"),
            {"host": HOST, "port": collector.port, "mode": "pinned", "fingerprint": "00" * 32},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("different certificate", response.json()["error"])
        self.assertEqual(trust().mode, LogForwarderTransportTrust.Mode.UNSET)

    def test_approving_answers_an_open_question(self):
        key, certificate = issue_self_signed(days_valid=3)
        collector = TlsCollector(key, [certificate])
        self.addCleanup(collector.close)
        update_configuration(enabled=True, host=HOST, port=collector.port, transport="tls")
        approve_destination(
            mode="pinned", host=HOST, port=collector.port, inspection=inspect_destination(HOST, collector.port)
        )
        check_destination_certificate()
        question = AuditEvent.objects.get(action=CERTIFICATE_QUESTION_ACTION)

        self.client.post(
            reverse("core:settings_log_forwarder_approve"),
            {
                "host": HOST,
                "port": collector.port,
                "mode": "pinned",
                "fingerprint": certificate.fingerprint(hashes.SHA256()).hex(),
            },
        )

        question.refresh_from_db()
        self.assertTrue(question.details["question_dismissed"])
        self.assertTrue(
            AuditEvent.objects.filter(action="log_forwarder.certificate.answered", details__answer="approved").exists()
        )

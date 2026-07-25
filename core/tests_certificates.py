"""Certificate import, publication and expiry watching, against real key material.

Every certificate here is minted in-process by `cryptography` and every format is
produced by the same library that produced the real thing: a DER encoding is a real
DER encoding, a PKCS#12 blob is a real PKCS#12 blob, an encrypted key is really
encrypted. Fixtures pasted from a text file would have made the parser's job easier
than it is in production, which is precisely the property a test of a parser must
not have.
"""

from __future__ import annotations

import base64
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, CertificateSettings, ManagedCertificate
from core.services import certificate_store
from core.services.certificate_expiry_watch import (
    CONDITION_EXPIRED,
    CONDITION_EXPIRING,
    EXPIRY_QUESTION_ACTION,
    check_stored_certificates,
    open_expiry_questions,
)
from core.services.certificates import (
    CertificateError,
    certificate_in_use,
    delete_certificate,
    expiry_warning_days,
    import_certificate,
    parse_material,
    private_key_pem,
    select_https_certificate,
    settings_record,
    update_expiry_policy,
)

TEST_KEYRING = f"k1:{base64.b64encode(b'C' * 32).decode()}"

SERVER = ManagedCertificate.Usage.SERVER
AUTHORITY = ManagedCertificate.Usage.AUTHORITY


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def issue_ca(common_name: str = "pve-helper test root"):
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


def issue_intermediate(root_key, root_certificate, common_name: str = "pve-helper test issuing CA"):
    key = _key()
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(root_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1825))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(root_key, hashes.SHA256())
    )
    return key, certificate


def issue_leaf(issuer_key, issuer_certificate, *, days_valid: int = 365, common_name: str = "pve-helper.example.net"):
    key = _key()
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(issuer_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now + timedelta(days=days_valid))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(common_name)]), critical=False)
        .sign(issuer_key, hashes.SHA256())
    )
    return key, certificate


def pem(certificate) -> bytes:
    return certificate.public_bytes(serialization.Encoding.PEM)


def der(certificate) -> bytes:
    return certificate.public_bytes(serialization.Encoding.DER)


def key_pem(key, password: bytes | None = None) -> bytes:
    encryption = serialization.BestAvailableEncryption(password) if password else serialization.NoEncryption()
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption)


def pkcs12_blob(key, leaf, chain, password: bytes | None) -> bytes:
    return pkcs12.serialize_key_and_certificates(
        b"pve-helper",
        key,
        leaf,
        chain,
        serialization.BestAvailableEncryption(password) if password else serialization.NoEncryption(),
    )


@override_settings(PVE_HELPER_ENCRYPTION_KEYS=TEST_KEYRING, PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID="k1")
class ParsingTests(TestCase):
    """Every shape an operator's CA might hand them has to land in one place."""

    def test_reads_a_pem_leaf_with_a_separate_key(self):
        ca_key, ca = issue_ca()
        leaf_key, leaf = issue_leaf(ca_key, ca)

        material = parse_material([pem(leaf), key_pem(leaf_key)])

        self.assertEqual(material.leaf.subject, leaf.subject)
        self.assertIsNotNone(material.private_key)

    def test_reads_a_der_certificate(self):
        _key_, ca = issue_ca()

        material = parse_material([der(ca)])

        self.assertEqual(material.leaf.subject, ca.subject)

    def test_reads_a_fullchain_file_and_orders_the_chain(self):
        root_key, root = issue_ca()
        intermediate_key, intermediate = issue_intermediate(root_key, root)
        leaf_key, leaf = issue_leaf(intermediate_key, intermediate)

        # Deliberately out of order: a hand-assembled bundle is under no obligation
        # to be leaf-first, and picking the first block would silently serve a CA.
        material = parse_material([pem(root) + pem(leaf) + pem(intermediate), key_pem(leaf_key)])

        self.assertEqual(material.leaf.subject, leaf.subject)
        self.assertEqual(
            [certificate.subject for certificate in material.chain],
            [intermediate.subject, root.subject],
        )

    def test_reads_a_password_protected_pkcs12(self):
        ca_key, ca = issue_ca()
        leaf_key, leaf = issue_leaf(ca_key, ca)
        blob = pkcs12_blob(leaf_key, leaf, [ca], b"hunter2")

        material = parse_material([blob], password="hunter2")

        self.assertEqual(material.leaf.subject, leaf.subject)
        self.assertIsNotNone(material.private_key)
        self.assertEqual([certificate.subject for certificate in material.chain], [ca.subject])

    def test_reads_a_pkcs12_with_no_password(self):
        ca_key, ca = issue_ca()
        leaf_key, leaf = issue_leaf(ca_key, ca)

        material = parse_material([pkcs12_blob(leaf_key, leaf, [ca], None)])

        self.assertEqual(material.leaf.subject, leaf.subject)

    def test_reads_an_encrypted_private_key(self):
        ca_key, ca = issue_ca()
        leaf_key, leaf = issue_leaf(ca_key, ca)

        material = parse_material([pem(leaf), key_pem(leaf_key, b"s3cret")], password="s3cret")

        self.assertIsNotNone(material.private_key)

    def test_an_encrypted_key_without_a_password_says_so(self):
        ca_key, ca = issue_ca()
        leaf_key, leaf = issue_leaf(ca_key, ca)

        with self.assertRaises(CertificateError) as caught:
            parse_material([pem(leaf), key_pem(leaf_key, b"s3cret")])

        self.assertIn("password", caught.exception.public_message.lower())

    def test_a_wrong_pkcs12_password_says_so(self):
        ca_key, ca = issue_ca()
        leaf_key, leaf = issue_leaf(ca_key, ca)
        blob = pkcs12_blob(leaf_key, leaf, [ca], b"right")

        with self.assertRaises(CertificateError) as caught:
            parse_material([blob], password="wrong")

        self.assertIn("password", caught.exception.public_message.lower())

    def test_material_with_no_certificate_is_refused(self):
        with self.assertRaises(CertificateError):
            parse_material([b"not a certificate at all"])


@override_settings(PVE_HELPER_ENCRYPTION_KEYS=TEST_KEYRING, PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID="k1")
class ImportTests(TestCase):
    def test_a_server_certificate_stores_a_sealed_key(self):
        ca_key, ca = issue_ca()
        leaf_key, leaf = issue_leaf(ca_key, ca)

        record = import_certificate(
            usage=SERVER,
            label="Web",
            blobs=[pem(leaf), pem(ca), key_pem(leaf_key)],
            uploaded_by="alice",
        )

        self.assertEqual(record.subject_alt_names, ["pve-helper.example.net"])
        self.assertIn(ca.subject.rfc4514_string(), record.issuer)
        # Sealed, not stored: the plaintext PEM must not be recoverable from the row.
        self.assertNotIn("PRIVATE KEY", record.private_key_sealed)
        self.assertTrue(record.private_key_sealed.startswith("v1:k1:"))
        self.assertIn("PRIVATE KEY", private_key_pem(record))

    def test_a_server_certificate_without_a_key_is_refused(self):
        ca_key, ca = issue_ca()
        _leaf_key, leaf = issue_leaf(ca_key, ca)

        with self.assertRaises(CertificateError) as caught:
            import_certificate(usage=SERVER, label="Web", blobs=[pem(leaf)])

        self.assertIn("private key", caught.exception.public_message.lower())

    def test_a_key_from_a_different_certificate_is_refused(self):
        ca_key, ca = issue_ca()
        _leaf_key, leaf = issue_leaf(ca_key, ca)
        other_key, _other = issue_leaf(ca_key, ca, common_name="other.example.net")

        with self.assertRaises(CertificateError) as caught:
            import_certificate(usage=SERVER, label="Web", blobs=[pem(leaf), key_pem(other_key)])

        self.assertIn("does not belong", caught.exception.public_message)

    def test_an_authority_carrying_a_private_key_is_refused(self):
        ca_key, ca = issue_ca()

        with self.assertRaises(CertificateError) as caught:
            import_certificate(usage=AUTHORITY, label="Root", blobs=[pem(ca), key_pem(ca_key)])

        self.assertIn("without the key", caught.exception.public_message)

    def test_the_same_certificate_twice_is_one_record(self):
        _ca_key, ca = issue_ca()
        import_certificate(usage=AUTHORITY, label="Root", blobs=[pem(ca)])

        with self.assertRaises(CertificateError) as caught:
            import_certificate(usage=AUTHORITY, label="Root again", blobs=[pem(ca)])

        self.assertIn("already stored", caught.exception.public_message)
        self.assertEqual(ManagedCertificate.objects.filter(usage=AUTHORITY).count(), 1)

    def test_an_authority_records_that_it_is_one(self):
        _ca_key, ca = issue_ca()

        record = import_certificate(usage=AUTHORITY, label="Root", blobs=[der(ca)])

        self.assertTrue(record.is_certificate_authority)


@override_settings(PVE_HELPER_ENCRYPTION_KEYS=TEST_KEYRING, PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID="k1")
class SelectionTests(TestCase):
    def setUp(self):
        self.ca_key, self.ca = issue_ca()
        leaf_key, leaf = issue_leaf(self.ca_key, self.ca)
        self.record = import_certificate(usage=SERVER, label="Web", blobs=[pem(leaf), key_pem(leaf_key)])

    def test_selecting_a_certificate_enables_https(self):
        config = select_https_certificate(self.record, enabled=True)

        self.assertTrue(config.https_enabled)
        self.assertEqual(config.active_certificate_id, self.record.pk)
        self.assertTrue(certificate_in_use(self.record))

    def test_an_expired_certificate_cannot_be_selected(self):
        expired_key, expired = issue_leaf(self.ca_key, self.ca, days_valid=-1, common_name="old.example.net")
        record = import_certificate(usage=SERVER, label="Old", blobs=[pem(expired), key_pem(expired_key)])

        with self.assertRaises(CertificateError) as caught:
            select_https_certificate(record, enabled=True)

        self.assertIn("expired", caught.exception.public_message)

    def test_an_authority_cannot_terminate_https(self):
        authority = import_certificate(usage=AUTHORITY, label="Root", blobs=[pem(self.ca)])

        with self.assertRaises(CertificateError):
            select_https_certificate(authority, enabled=True)

    def test_the_serving_certificate_cannot_be_deleted(self):
        select_https_certificate(self.record, enabled=True)

        with self.assertRaises(CertificateError) as caught:
            delete_certificate(self.record)

        self.assertIn("serving HTTPS", caught.exception.public_message)
        self.assertTrue(ManagedCertificate.objects.filter(pk=self.record.pk).exists())

    def test_disabling_https_frees_the_certificate(self):
        select_https_certificate(self.record, enabled=True)
        select_https_certificate(None, enabled=False)

        delete_certificate(self.record)

        self.assertFalse(ManagedCertificate.objects.filter(pk=self.record.pk).exists())

    def test_the_warning_window_is_bounded(self):
        for days in (0, 100):
            with self.subTest(days=days), self.assertRaises(CertificateError):
                update_expiry_policy(enabled=True, days=days)

        update_expiry_policy(enabled=True, days=30)

        self.assertEqual(expiry_warning_days(), 30)


@override_settings(PVE_HELPER_ENCRYPTION_KEYS=TEST_KEYRING, PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID="k1")
class PublicationTests(TestCase):
    """What nginx and OpenSSL actually read off the shared volume."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.directory = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)
        self.base = self.directory / "base-bundle.pem"
        self.base.write_text("# deployment base bundle\n", encoding="utf-8")
        self.ca_key, self.ca = issue_ca()
        leaf_key, leaf = issue_leaf(self.ca_key, self.ca)
        self.leaf = leaf
        self.record = import_certificate(usage=SERVER, label="Web", blobs=[pem(leaf), pem(self.ca), key_pem(leaf_key)])

    def _override(self):
        return override_settings(
            PVE_HELPER_CERTIFICATE_STATE_DIR=str(self.directory / "state"),
            PVE_HELPER_BASE_CA_BUNDLE=str(self.base),
        )

    def test_publishing_writes_the_chain_and_a_private_key_only_root_can_read(self):
        with self._override():
            select_https_certificate(self.record, enabled=True)
            certificate_store.publish()

        state = self.directory / "state"
        served = (state / "server.crt").read_text(encoding="utf-8")

        self.assertEqual(served.count("BEGIN CERTIFICATE"), 2)
        self.assertTrue(served.startswith(pem(self.leaf).decode("ascii")))
        self.assertIn("PRIVATE KEY", (state / "server.key").read_text(encoding="utf-8"))
        self.assertEqual((state / "server.key").stat().st_mode & 0o777, 0o600)

    def test_disabling_https_removes_the_key_from_disk(self):
        with self._override():
            select_https_certificate(self.record, enabled=True)
            certificate_store.publish()
            self.assertTrue((self.directory / "state" / "server.key").exists())

            select_https_certificate(None, enabled=False)
            certificate_store.publish()

        # An unused private key left readable in a volume for the life of the
        # installation is the whole reason this is removed rather than ignored.
        self.assertFalse((self.directory / "state" / "server.key").exists())
        self.assertFalse((self.directory / "state" / "server.crt").exists())

    def test_the_bundle_extends_the_base_rather_than_replacing_it(self):
        import_certificate(usage=AUTHORITY, label="Root", blobs=[pem(self.ca)])

        with self._override():
            certificate_store.publish()
            bundle = (self.directory / "state" / "ca-bundle.pem").read_text(encoding="utf-8")

        self.assertIn("# deployment base bundle", bundle)
        self.assertIn(pem(self.ca).decode("ascii"), bundle)

    def test_the_state_digest_moves_only_when_something_changed(self):
        with self._override():
            first = certificate_store.publish()
            unchanged = certificate_store.publish()
            import_certificate(usage=AUTHORITY, label="Root", blobs=[pem(self.ca)])
            after = certificate_store.publish()

        self.assertEqual(first, unchanged)
        self.assertNotEqual(first, after)
        self.assertEqual((self.directory / "state" / "state").read_text(encoding="utf-8").strip(), after)

    def test_publication_survives_a_missing_base_bundle(self):
        with override_settings(
            PVE_HELPER_CERTIFICATE_STATE_DIR=str(self.directory / "state"),
            PVE_HELPER_BASE_CA_BUNDLE=str(self.directory / "absent.pem"),
        ):
            import_certificate(usage=AUTHORITY, label="Root", blobs=[pem(self.ca)])
            certificate_store.publish()
            bundle = (self.directory / "state" / "ca-bundle.pem").read_text(encoding="utf-8")

        self.assertIn(pem(self.ca).decode("ascii"), bundle)


@override_settings(PVE_HELPER_ENCRYPTION_KEYS=TEST_KEYRING, PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID="k1")
class ExpiryWatchTests(TestCase):
    def setUp(self):
        self.ca_key, self.ca = issue_ca()

    def _stored(self, *, days_valid: int, label: str = "Web", common_name: str = "pve-helper.example.net"):
        leaf_key, leaf = issue_leaf(self.ca_key, self.ca, days_valid=days_valid, common_name=common_name)
        return import_certificate(usage=SERVER, label=label, blobs=[pem(leaf), key_pem(leaf_key)])

    def test_a_certificate_inside_the_window_raises_one_question(self):
        record = self._stored(days_valid=3)

        first = check_stored_certificates()
        second = check_stored_certificates()

        self.assertEqual(first["raised"], 1)
        # A daily job that refiled the same finding every run is how a pulsing
        # badge stops being believed.
        self.assertEqual(second["raised"], 0)
        question = AuditEvent.objects.get(action=EXPIRY_QUESTION_ACTION)
        self.assertEqual(question.details["condition"], CONDITION_EXPIRING)
        self.assertEqual(question.details["fingerprint"], record.sha256_fingerprint)
        self.assertEqual(question.outcome, "warning")

    def test_a_certificate_outside_the_window_raises_nothing(self):
        self._stored(days_valid=365)

        self.assertEqual(check_stored_certificates()["raised"], 0)
        self.assertEqual(open_expiry_questions().count(), 0)

    def test_an_expired_certificate_is_named_apart_from_an_expiring_one(self):
        self._stored(days_valid=-1)

        check_stored_certificates()

        self.assertEqual(AuditEvent.objects.get(action=EXPIRY_QUESTION_ACTION).details["condition"], CONDITION_EXPIRED)

    def test_replacing_the_certificate_answers_its_question(self):
        record = self._stored(days_valid=3)
        check_stored_certificates()
        self.assertEqual(open_expiry_questions().count(), 1)

        record.delete()
        result = check_stored_certificates()

        self.assertEqual(result["resolved"], 1)
        self.assertEqual(open_expiry_questions().count(), 0)

    def test_turning_warnings_off_closes_what_they_raised(self):
        self._stored(days_valid=3)
        check_stored_certificates()

        update_expiry_policy(enabled=False, days=7)
        result = check_stored_certificates()

        self.assertFalse(result["checked"])
        self.assertEqual(open_expiry_questions().count(), 0)

    def test_the_configured_window_is_what_decides(self):
        self._stored(days_valid=20)
        self.assertEqual(check_stored_certificates()["raised"], 0)

        update_expiry_policy(enabled=True, days=30)

        self.assertEqual(check_stored_certificates()["raised"], 1)


@override_settings(PVE_HELPER_ENCRYPTION_KEYS=TEST_KEYRING, PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID="k1")
class SettingsPageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("operator", password="operator-password")
        self.client.force_login(self.user)
        self.ca_key, self.ca = issue_ca()
        self.leaf_key, self.leaf = issue_leaf(self.ca_key, self.ca)
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.state = override_settings(
            PVE_HELPER_CERTIFICATE_STATE_DIR=str(Path(self._directory.name) / "state"),
            PVE_HELPER_BASE_CA_BUNDLE="",
        )
        self.state.enable()
        self.addCleanup(self.state.disable)
        self.url = reverse("core:settings_certificates")

    def test_the_tab_renders(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Trusted certificate authorities")
        self.assertContains(response, "Expiry warnings")

    def test_uploading_a_pkcs12_stores_it_and_audits_the_import(self):
        blob = pkcs12_blob(self.leaf_key, self.leaf, [self.ca], b"hunter2")

        response = self.client.post(
            self.url,
            {
                "action": "upload",
                "usage": "server",
                "label": "Web",
                "password": "hunter2",
                "certificate": SimpleUploadedFile("web.pfx", blob),
            },
        )

        self.assertRedirects(response, self.url)
        record = ManagedCertificate.objects.get(usage=SERVER)
        self.assertEqual(record.label, "Web")
        self.assertEqual(record.uploaded_by, "operator")
        self.assertTrue(AuditEvent.objects.filter(action="certificate.imported").exists())

    def test_a_bad_password_is_reported_without_storing_anything(self):
        blob = pkcs12_blob(self.leaf_key, self.leaf, [self.ca], b"right")

        response = self.client.post(
            self.url,
            {
                "action": "upload",
                "usage": "server",
                "label": "Web",
                "password": "wrong",
                "certificate": SimpleUploadedFile("web.pfx", blob),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "password")
        self.assertEqual(ManagedCertificate.objects.count(), 0)

    def test_enabling_https_publishes_the_certificate(self):
        record = import_certificate(usage=SERVER, label="Web", blobs=[pem(self.leaf), key_pem(self.leaf_key)])

        response = self.client.post(
            self.url,
            {"action": "select_https", "certificate_id": str(record.pk), "https_enabled": "on"},
        )

        self.assertRedirects(response, self.url)
        self.assertTrue(CertificateSettings.objects.get(pk=1).https_enabled)
        self.assertTrue((Path(self._directory.name) / "state" / "server.crt").exists())

    def test_deleting_the_serving_certificate_is_refused(self):
        record = import_certificate(usage=SERVER, label="Web", blobs=[pem(self.leaf), key_pem(self.leaf_key)])
        select_https_certificate(record, enabled=True)

        response = self.client.post(self.url, {"action": "delete", "certificate_id": str(record.pk)})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "serving HTTPS")
        self.assertTrue(ManagedCertificate.objects.filter(pk=record.pk).exists())

    def test_the_warning_policy_is_saved(self):
        response = self.client.post(
            self.url,
            {"action": "expiry_policy", "expiry_warning_enabled": "on", "expiry_warning_days": "21"},
        )

        self.assertRedirects(response, self.url)
        self.assertEqual(settings_record().expiry_warning_days, 21)

    def test_the_page_flags_a_certificate_inside_the_warning_window(self):
        soon_key, soon = issue_leaf(self.ca_key, self.ca, days_valid=2, common_name="soon.example.net")
        import_certificate(usage=SERVER, label="Soon", blobs=[pem(soon), key_pem(soon_key)])

        response = self.client.get(self.url)

        self.assertContains(response, "certificate-expiring")

    def test_answering_the_expiry_question_records_the_acknowledgement(self):
        record = import_certificate(usage=SERVER, label="Web", blobs=[pem(self.leaf), key_pem(self.leaf_key)])
        ManagedCertificate.objects.filter(pk=record.pk).update(not_after=timezone.now() + timedelta(days=1))
        check_stored_certificates()
        question = AuditEvent.objects.get(action=EXPIRY_QUESTION_ACTION)

        response = self.client.post(
            reverse("core:dismiss_task_question"),
            {"task_id": f"certificate:{question.id}", "answer": "acknowledged"},
        )

        self.assertEqual(response.status_code, 200)
        question.refresh_from_db()
        self.assertTrue(question.details["question_dismissed"])
        answer = AuditEvent.objects.get(action="certificate.expiry.answered")
        self.assertEqual(answer.details["answer"], "acknowledged")

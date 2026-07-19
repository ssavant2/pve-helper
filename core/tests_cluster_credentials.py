from __future__ import annotations

import base64

from django.test import SimpleTestCase, TestCase, override_settings

from core.models import ClusterCredential, ProxmoxCluster, ProxmoxEndpoint, RuntimeConfigurationState
from core.services.cluster_credentials import (
    ClusterCredentialError,
    complete_credential_cutover,
    credentials_needing_rotation,
    missing_encryption_key_ids,
    resolve_credential,
    rotate_credential,
    set_cluster_credential,
)
from core.services.secret_encryption import (
    EncryptionConfigurationError,
    MissingEncryptionKeyError,
    SecretDecryptionError,
    active_key_id,
    decrypt_secret,
    encrypt_secret,
    generate_key,
    key_id_of,
)

KEY_A = base64.b64encode(b"A" * 32).decode()
KEY_B = base64.b64encode(b"B" * 32).decode()
ONE_KEY = f"k1:{KEY_A}"
TWO_KEYS = f"k1:{KEY_A},k2:{KEY_B}"


@override_settings(PVE_HELPER_ENCRYPTION_KEYS=ONE_KEY, PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID="k1")
class SecretEncryptionTests(SimpleTestCase):
    def test_roundtrips_and_names_its_key(self):
        sealed = encrypt_secret("super-secret-token")

        self.assertTrue(sealed.startswith("v1:k1:"))
        self.assertEqual(key_id_of(sealed), "k1")
        self.assertEqual(decrypt_secret(sealed), "super-secret-token")

    def test_ciphertext_does_not_contain_the_plaintext(self):
        sealed = encrypt_secret("super-secret-token")

        self.assertNotIn("super-secret-token", sealed)

    def test_each_encryption_is_distinct(self):
        # A repeated nonce under one key would leak plaintext relationships.
        self.assertNotEqual(encrypt_secret("same"), encrypt_secret("same"))

    def test_tampered_ciphertext_is_rejected(self):
        sealed = encrypt_secret("super-secret-token")
        version, key_id, payload = sealed.split(":", 2)
        raw = bytearray(base64.b64decode(payload))
        raw[-1] ^= 0x01
        tampered = f"{version}:{key_id}:{base64.b64encode(bytes(raw)).decode()}"

        with self.assertRaises(SecretDecryptionError):
            decrypt_secret(tampered)

    def test_key_id_cannot_be_swapped_in_the_stored_string(self):
        # The key id is bound into the authentication tag, so relabelling a value to
        # point at another key must not authenticate.
        with override_settings(PVE_HELPER_ENCRYPTION_KEYS=TWO_KEYS, PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID="k1"):
            sealed = encrypt_secret("super-secret-token")
            relabelled = sealed.replace("v1:k1:", "v1:k2:", 1)

            with self.assertRaises(SecretDecryptionError):
                decrypt_secret(relabelled)

    def test_missing_key_is_named_rather_than_swallowed(self):
        sealed = encrypt_secret("super-secret-token")

        with override_settings(PVE_HELPER_ENCRYPTION_KEYS=f"k2:{KEY_B}", PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID="k2"):
            with self.assertRaises(MissingEncryptionKeyError) as ctx:
                decrypt_secret(sealed)

        self.assertIn("k1", str(ctx.exception))

    def test_unknown_format_version_is_refused(self):
        with self.assertRaises(SecretDecryptionError):
            decrypt_secret("v2:k1:AAAA")

    def test_empty_secret_is_refused(self):
        with self.assertRaises(ValueError):
            encrypt_secret("")

    def test_generated_keys_are_usable_and_distinct(self):
        first, second = generate_key(), generate_key()

        self.assertNotEqual(first, second)
        self.assertEqual(len(base64.b64decode(first)), 32)


class KeyringConfigurationTests(SimpleTestCase):
    def test_no_keyring_is_an_explicit_error(self):
        with override_settings(PVE_HELPER_ENCRYPTION_KEYS=""):
            with self.assertRaises(EncryptionConfigurationError):
                active_key_id()

    def test_single_key_needs_no_active_id(self):
        with override_settings(PVE_HELPER_ENCRYPTION_KEYS=ONE_KEY, PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID=""):
            self.assertEqual(active_key_id(), "k1")

    def test_several_keys_require_naming_the_active_one(self):
        # Otherwise which key seals new secrets would depend on dict ordering.
        with override_settings(PVE_HELPER_ENCRYPTION_KEYS=TWO_KEYS, PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID=""):
            with self.assertRaises(EncryptionConfigurationError):
                active_key_id()

    def test_active_key_must_exist_in_the_keyring(self):
        with override_settings(PVE_HELPER_ENCRYPTION_KEYS=ONE_KEY, PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID="nope"):
            with self.assertRaises(EncryptionConfigurationError):
                active_key_id()

    def test_malformed_keyring_entries_are_refused(self):
        for raw in [
            "k1",  # no key
            f"k1:{base64.b64encode(b'short').decode()}",  # wrong length
            "k1:not-base64!!",
            f"K1:{KEY_A}",  # uppercase id
            f"k1:{KEY_A},k1:{KEY_B}",  # duplicate id
        ]:
            with self.subTest(raw=raw), override_settings(PVE_HELPER_ENCRYPTION_KEYS=raw):
                with self.assertRaises(EncryptionConfigurationError):
                    active_key_id()


@override_settings(
    PVE_HELPER_ENCRYPTION_KEYS=ONE_KEY,
    PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID="k1",
    PVE_API_TOKEN_ID="legacy@pve!legacy",
    PVE_API_TOKEN_SECRET="legacy-secret",
)
class ClusterCredentialTests(TestCase):
    def setUp(self):
        super().setUp()
        self.cluster_a = ProxmoxCluster.objects.create(key="a", display_name="A", enabled=True)
        self.cluster_b = ProxmoxCluster.objects.create(key="b", display_name="B", enabled=False)

    def test_secret_is_never_stored_in_plaintext(self):
        set_cluster_credential(self.cluster_a, token_id="a@pve!t", token_secret="secret-a")

        row = ClusterCredential.objects.get(cluster=self.cluster_a)
        self.assertNotIn("secret-a", row.token_secret_sealed)
        self.assertEqual(row.encryption_key_id, "k1")
        self.assertEqual(row.token_id, "a@pve!t")

    def test_each_cluster_resolves_its_own_identity(self):
        set_cluster_credential(self.cluster_a, token_id="a@pve!t", token_secret="secret-a")
        set_cluster_credential(self.cluster_b, token_id="b@pve!t", token_secret="secret-b")

        first = resolve_credential(self.cluster_a)
        second = resolve_credential(self.cluster_b)

        self.assertEqual((first.token_id, first.token_secret), ("a@pve!t", "secret-a"))
        self.assertEqual((second.token_id, second.token_secret), ("b@pve!t", "secret-b"))

    def test_credential_repr_does_not_leak_the_secret(self):
        # A traceback or debugger transcript must not become a secret disclosure.
        credential = resolve_credential(self.cluster_a)

        self.assertNotIn(credential.token_secret, repr(credential))
        self.assertIn("redacted", repr(credential))

    def test_legacy_token_is_used_only_until_cutover(self):
        credential = resolve_credential(self.cluster_a)
        self.assertEqual(credential.token_id, "legacy@pve!legacy")

        set_cluster_credential(self.cluster_a, token_id="a@pve!t", token_secret="secret-a")
        RuntimeConfigurationState.objects.create(
            pk=RuntimeConfigurationState.SINGLETON_PK,
            bootstrap_completed=True,
            credential_cutover_completed_at="2026-07-17T00:00:00Z",
        )
        ClusterCredential.objects.filter(cluster=self.cluster_b).delete()

        # After cutover a cluster without a credential must fail rather than borrow
        # a global token that may belong to another cluster entirely.
        with self.assertRaises(ClusterCredentialError):
            resolve_credential(self.cluster_b)

    def test_identity_contract_v1_closes_fallback_without_a_separate_marker(self):
        RuntimeConfigurationState.objects.create(
            pk=RuntimeConfigurationState.SINGLETON_PK,
            bootstrap_completed=True,
            identity_contract_version=1,
        )
        ClusterCredential.objects.filter(cluster=self.cluster_b).delete()

        with self.assertRaises(ClusterCredentialError):
            resolve_credential(self.cluster_b)

    def test_rotation_reseals_without_changing_the_token(self):
        set_cluster_credential(self.cluster_a, token_id="a@pve!t", token_secret="secret-a")

        with override_settings(PVE_HELPER_ENCRYPTION_KEYS=TWO_KEYS, PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID="k2"):
            pending = credentials_needing_rotation()
            self.assertEqual([c.cluster.key for c in pending], ["a"])

            rotate_credential(pending[0])

            row = ClusterCredential.objects.get(cluster=self.cluster_a)
            self.assertEqual(row.encryption_key_id, "k2")
            self.assertEqual(resolve_credential(self.cluster_a).token_secret, "secret-a")
            self.assertEqual(credentials_needing_rotation(), [])

    def test_rotation_without_the_old_key_says_so(self):
        set_cluster_credential(self.cluster_a, token_id="a@pve!t", token_secret="secret-a")

        # k1 is gone: the secret is unreadable and must not be reported as rotated.
        with override_settings(PVE_HELPER_ENCRYPTION_KEYS=f"k2:{KEY_B}", PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID="k2"):
            with self.assertRaises(MissingEncryptionKeyError):
                rotate_credential(ClusterCredential.objects.get(cluster=self.cluster_a))

    def test_missing_key_ids_are_reported_for_startup(self):
        set_cluster_credential(self.cluster_a, token_id="a@pve!t", token_secret="secret-a")

        with override_settings(PVE_HELPER_ENCRYPTION_KEYS=f"k2:{KEY_B}", PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID="k2"):
            self.assertEqual(missing_encryption_key_ids(), ["k1"])

    def test_set_credential_requires_both_parts(self):
        for token_id, secret in [("", "s"), ("t", ""), ("  ", "s")]:
            with self.subTest(token_id=token_id):
                with self.assertRaises(ClusterCredentialError):
                    set_cluster_credential(self.cluster_a, token_id=token_id, token_secret=secret)


@override_settings(
    PVE_HELPER_ENCRYPTION_KEYS=ONE_KEY,
    PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID="k1",
    PVE_API_TOKEN_ID="legacy@pve!legacy",
    PVE_API_TOKEN_SECRET="legacy-secret",
)
class CredentialCutoverTests(TestCase):
    def setUp(self):
        super().setUp()
        self.cluster = ProxmoxCluster.objects.create(key="default", display_name="Default", enabled=True)
        self.state = RuntimeConfigurationState.objects.create(
            pk=RuntimeConfigurationState.SINGLETON_PK, bootstrap_completed=True
        )

    def test_cutover_seals_the_legacy_token_and_records_its_marker(self):
        changed, message = complete_credential_cutover()

        self.assertTrue(changed)
        row = ClusterCredential.objects.get(cluster=self.cluster)
        self.assertEqual(row.token_id, "legacy@pve!legacy")
        self.assertNotIn("legacy-secret", row.token_secret_sealed)
        self.assertEqual(resolve_credential(self.cluster).token_secret, "legacy-secret")
        self.state.refresh_from_db()
        self.assertIsNotNone(self.state.credential_cutover_completed_at)
        self.assertIn("default", message)

    def test_cutover_is_not_repeated(self):
        complete_credential_cutover()
        changed, message = complete_credential_cutover()

        self.assertFalse(changed)
        self.assertIn("already", message)

    def test_cutover_without_a_keyring_records_no_marker(self):
        # A half-done cutover would stop the legacy fallback while leaving nothing
        # able to decrypt, so it must leave the installation exactly as it was.
        with override_settings(PVE_HELPER_ENCRYPTION_KEYS=""):
            with self.assertRaises(EncryptionConfigurationError):
                complete_credential_cutover()

        self.state.refresh_from_db()
        self.assertIsNone(self.state.credential_cutover_completed_at)
        self.assertFalse(ClusterCredential.objects.exists())

    def test_legacy_settings_are_ignored_not_deleted_so_rollback_works(self):
        complete_credential_cutover()

        # The settings still hold the token; only runtime reads of them stopped.
        from django.conf import settings

        self.assertEqual(settings.PVE_API_TOKEN_SECRET, "legacy-secret")


class EndpointUrlIdentityTests(TestCase):
    """One transport belongs to one cluster: an endpoint answering for the wrong
    cluster would file its inventory under the wrong identity."""

    def test_normalization_treats_the_same_host_as_the_same_endpoint(self):
        from core.services.config import normalize_endpoint_url

        canonical = "https://pve201.example.net:8006"
        for variant in [
            "https://pve201.example.net:8006",
            "https://PVE201.example.net:8006/",
            "https://pve201.example.net:8006//",
            "pve201.example.net:8006",
        ]:
            with self.subTest(variant=variant):
                self.assertEqual(normalize_endpoint_url(variant), canonical)

    def test_default_port_and_explicit_port_match(self):
        from core.services.config import normalize_endpoint_url

        self.assertEqual(normalize_endpoint_url("https://p.example.net"), "https://p.example.net:8006")

    def test_two_clusters_cannot_claim_the_same_transport(self):
        from django.db import IntegrityError, transaction

        first = ProxmoxCluster.objects.create(key="a", display_name="A", enabled=True)
        second = ProxmoxCluster.objects.create(key="b", display_name="B", enabled=False)
        ProxmoxEndpoint.objects.create(name="a1", url="https://pve.example.net:8006", cluster=first, enabled=True)

        with self.assertRaises(IntegrityError), transaction.atomic():
            ProxmoxEndpoint.objects.create(name="b1", url="https://PVE.example.net:8006/", cluster=second, enabled=True)

    def test_editing_a_url_keeps_the_canonical_form_in_sync(self):
        cluster = ProxmoxCluster.objects.create(key="a", display_name="A", enabled=True)
        endpoint = ProxmoxEndpoint.objects.create(
            name="a1", url="https://old.example.net:8006", cluster=cluster, enabled=True
        )

        endpoint.url = "https://new.example.net:8006/"
        endpoint.save(update_fields=["url"])

        endpoint.refresh_from_db()
        self.assertEqual(endpoint.normalized_url, "https://new.example.net:8006")


class RecoveryCommandTests(SimpleTestCase):
    """The keyring check reports a missing key by failing the deployment. The
    commands that recover from that state must still run in it — otherwise the
    documented recovery procedure is impossible, because Django runs system checks
    before a command's own work."""

    def test_recovery_commands_do_not_run_the_blocking_checks(self):
        from core.management.commands.rotate_encryption_keys import Command as Rotate
        from core.management.commands.set_cluster_credential import Command as SetCredential

        self.assertEqual(SetCredential.requires_system_checks, [])
        self.assertEqual(Rotate.requires_system_checks, [])


@override_settings(PVE_HELPER_ENCRYPTION_KEYS=ONE_KEY, PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID="k1")
class CredentialInjectionTests(TestCase):
    def test_client_carries_its_own_clusters_identity(self):
        from core.services.cluster_resolver import client_for_endpoint

        cluster_a = ProxmoxCluster.objects.create(key="a", display_name="A", enabled=True)
        cluster_b = ProxmoxCluster.objects.create(key="b", display_name="B", enabled=False)
        set_cluster_credential(cluster_a, token_id="a@pve!t", token_secret="secret-a")
        set_cluster_credential(cluster_b, token_id="b@pve!t", token_secret="secret-b")
        endpoint_a = ProxmoxEndpoint.objects.create(
            name="a1", url="https://a1.example.com:8006", cluster=cluster_a, enabled=True
        )
        endpoint_b = ProxmoxEndpoint.objects.create(
            name="b1", url="https://b1.example.com:8006", cluster=cluster_b, enabled=True
        )

        header_a = client_for_endpoint(endpoint_a)._credential.authorization_header()
        header_b = client_for_endpoint(endpoint_b)._credential.authorization_header()

        self.assertEqual(header_a, "PVEAPIToken=a@pve!t=secret-a")
        self.assertEqual(header_b, "PVEAPIToken=b@pve!t=secret-b")
        self.assertNotEqual(header_a, header_b)

from __future__ import annotations

import ssl
from unittest.mock import MagicMock, patch

import httpx
from django.test import SimpleTestCase, TestCase, override_settings

from core.models import (
    ClusterTransportTrust,
    ProxmoxCluster,
    ProxmoxEndpoint,
    RuntimeConfigurationState,
)
from core.services.cluster_identity import (
    ClusterIdentityError,
    ClusterIdentityMismatch,
    ObservedClusterIdentity,
    discover_cluster_identity,
    reapprove_identity,
    verify_or_bind_identity,
)
from core.services.cluster_resolver import (
    ClusterQuarantinedError,
    client_for_endpoint,
    cluster_wide_read,
)
from core.services.cluster_trust import (
    TRUST_CA_PEM,
    TRUST_INSECURE,
    TRUST_PUBLIC,
    TRUST_PUBLIC_PLUS_CA,
    InspectedCertificate,
    TransportTrustError,
    TrustProfile,
    accepted_endpoint_certificate,
    approve_cluster_transport,
    complete_trust_cutover,
    legacy_trust_profile,
    resolve_trust_profile,
    ssl_context_for,
)
from core.services.proxmox import ProxmoxAPIError, ProxmoxClient, ProxmoxTlsTrustError
from core.services.public_errors import PROVIDER_FAILURE_MESSAGE, public_failure

# A syntactically valid self-signed CA is not needed for most tests; the ssl layer
# only parses it in build_verify, which those tests exercise separately.
FAKE_CA = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


class TrustProfilePoolingTests(SimpleTestCase):
    def test_public_and_ca_pem_key_different_pools(self):
        public = TrustProfile(mode=TRUST_PUBLIC)
        ca = TrustProfile(mode=TRUST_CA_PEM, ca_pem="CA-X")

        self.assertNotEqual(public.cache_key(), ca.cache_key())

    def test_different_ca_bundles_key_different_pools(self):
        # Two clusters with different CAs must never share a client, or one cluster's
        # trust decision would apply to the other's connections.
        self.assertNotEqual(
            TrustProfile(mode=TRUST_CA_PEM, ca_pem="CA-X").cache_key(),
            TrustProfile(mode=TRUST_CA_PEM, ca_pem="CA-Y").cache_key(),
        )

    def test_same_ca_bundle_shares_one_pool(self):
        self.assertEqual(
            TrustProfile(mode=TRUST_CA_PEM, ca_pem="CA-X").cache_key(),
            TrustProfile(mode=TRUST_CA_PEM, ca_pem="CA-X").cache_key(),
        )

    def test_public_builds_system_verification(self):
        self.assertIs(TrustProfile(mode=TRUST_PUBLIC).build_verify(), True)

    def test_insecure_builds_no_verification(self):
        self.assertIs(TrustProfile(mode=TRUST_INSECURE).build_verify(), False)

    def test_ca_pem_without_a_bundle_is_refused(self):
        with self.assertRaises(TransportTrustError):
            TrustProfile(mode=TRUST_CA_PEM, ca_pem="").build_verify()


class LegacyTrustProfileTests(SimpleTestCase):
    @override_settings(PVE_CA_BUNDLE="", PVE_VERIFY_TLS=True)
    def test_public_when_verifying_without_a_bundle(self):
        self.assertEqual(legacy_trust_profile(), TrustProfile(mode=TRUST_PUBLIC))

    @override_settings(PVE_CA_BUNDLE="", PVE_VERIFY_TLS=False)
    def test_insecure_when_verification_is_off(self):
        self.assertEqual(legacy_trust_profile(), TrustProfile(mode=TRUST_INSECURE))


@override_settings(PVE_CA_BUNDLE="", PVE_VERIFY_TLS=True)
class TrustResolutionTests(TestCase):
    def setUp(self):
        super().setUp()
        self.cluster = ProxmoxCluster.objects.create(key="a", display_name="A", enabled=True)

    def test_stored_trust_wins(self):
        ClusterTransportTrust.objects.create(
            cluster=self.cluster, mode=ClusterTransportTrust.Mode.CA_PEM, ca_pem="CA-X"
        )

        profile = resolve_trust_profile(self.cluster)

        self.assertEqual(profile, TrustProfile(mode=TRUST_CA_PEM, ca_pem="CA-X"))

    def test_legacy_fallback_before_cutover(self):
        self.assertEqual(resolve_trust_profile(self.cluster), TrustProfile(mode=TRUST_PUBLIC))

    def test_no_ambient_fallback_after_cutover(self):
        RuntimeConfigurationState.objects.create(
            pk=RuntimeConfigurationState.SINGLETON_PK,
            bootstrap_completed=True,
            trust_cutover_completed_at="2026-07-17T00:00:00Z",
        )

        # After the cutover, a cluster without stored trust must not borrow a global
        # CA decision that may not describe it at all.
        with self.assertRaises(TransportTrustError):
            resolve_trust_profile(self.cluster)

    def test_identity_contract_v1_closes_fallback_without_a_separate_marker(self):
        RuntimeConfigurationState.objects.create(
            pk=RuntimeConfigurationState.SINGLETON_PK,
            bootstrap_completed=True,
            identity_contract_version=1,
        )

        with self.assertRaises(TransportTrustError):
            resolve_trust_profile(self.cluster)


class ClusterIdentityTests(TestCase):
    def setUp(self):
        super().setUp()
        self.cluster = ProxmoxCluster.objects.create(key="a", display_name="A", enabled=True)

    def _client(self, subject, fingerprint="AA:BB"):
        class FakeClient:
            def get(self, path):
                assert "certificates/info" in path
                return [
                    {"filename": "pveproxy-ssl.pem", "subject": "CN=whatever"},
                    {"filename": "pve-root-ca.pem", "subject": subject, "fingerprint": fingerprint},
                ]

        return FakeClient()

    def test_discovers_uuid_from_the_ca_subject(self):
        subject = "OU=bc6169b4-c1fe-4c05-b4b5-e3cbf114db3e,O=PVE Cluster Manager CA"
        observed = discover_cluster_identity(self._client(subject), "pve1")

        self.assertEqual(observed.ca_uuid, "bc6169b4-c1fe-4c05-b4b5-e3cbf114db3e")
        self.assertEqual(observed.ca_fingerprint, "AA:BB")

    def test_missing_root_ca_is_an_error(self):
        class NoRootCa:
            def get(self, path):
                return [{"filename": "pveproxy-ssl.pem", "subject": "CN=x"}]

        with self.assertRaises(ClusterIdentityError):
            discover_cluster_identity(NoRootCa(), "pve1")

    def test_first_observation_binds_the_identity(self):
        observed = ObservedClusterIdentity(ca_uuid="uuid-a", ca_fingerprint="fp-a")

        result = verify_or_bind_identity(self.cluster, observed)

        self.cluster.refresh_from_db()
        self.assertEqual(result, "bound")
        self.assertEqual(self.cluster.discovered_ca_uuid, "uuid-a")
        self.assertFalse(self.cluster.ingestion_quarantined)

    def test_matching_identity_passes(self):
        self.cluster.discovered_ca_uuid = "uuid-a"
        self.cluster.discovered_ca_fingerprint = "fp-a"
        self.cluster.save()

        result = verify_or_bind_identity(self.cluster, ObservedClusterIdentity("uuid-a", "fp-a"))

        self.assertEqual(result, "match")

    def test_uuid_mismatch_quarantines_and_raises(self):
        self.cluster.discovered_ca_uuid = "uuid-a"
        self.cluster.save()

        with self.assertRaises(ClusterIdentityMismatch):
            verify_or_bind_identity(self.cluster, ObservedClusterIdentity("uuid-b", "fp-b"))

        self.cluster.refresh_from_db()
        self.assertTrue(self.cluster.ingestion_quarantined)
        self.assertIn("uuid-b", self.cluster.quarantine_reason)

    def test_fingerprint_renewal_under_same_uuid_is_accepted(self):
        # A CA renewal keeps the UUID and changes the fingerprint; that is legitimate.
        self.cluster.discovered_ca_uuid = "uuid-a"
        self.cluster.discovered_ca_fingerprint = "fp-old"
        self.cluster.save()

        verify_or_bind_identity(self.cluster, ObservedClusterIdentity("uuid-a", "fp-new"))

        self.cluster.refresh_from_db()
        self.assertEqual(self.cluster.discovered_ca_fingerprint, "fp-new")
        self.assertFalse(self.cluster.ingestion_quarantined)

    def test_reapproval_lifts_quarantine(self):
        self.cluster.discovered_ca_uuid = "uuid-a"
        self.cluster.ingestion_quarantined = True
        self.cluster.quarantine_reason = "mismatch"
        self.cluster.save()

        reapprove_identity(self.cluster, ObservedClusterIdentity("uuid-b", "fp-b"))

        self.cluster.refresh_from_db()
        self.assertEqual(self.cluster.discovered_ca_uuid, "uuid-b")
        self.assertFalse(self.cluster.ingestion_quarantined)


class IdentityDiscoveryFailoverTests(TestCase):
    """Identity discovery must fail over across the cluster's endpoints: the CA is
    the same from any member, so a single down node must not block verification."""

    def setUp(self):
        super().setUp()
        self.cluster = ProxmoxCluster.objects.create(key="a", display_name="A", enabled=True)
        self.ep1 = ProxmoxEndpoint.objects.create(name="a1", url="https://a1:8006", cluster=self.cluster, enabled=True)
        self.ep2 = ProxmoxEndpoint.objects.create(name="a2", url="https://a2:8006", cluster=self.cluster, enabled=True)

    def test_second_endpoint_answers_when_the_first_is_down(self):
        from core.services.proxmox import ProxmoxAPIError

        class Client:
            def __init__(self, name, ok):
                self.name = name
                self.ok = ok

            def discover_node_name(self, fallback):
                return self.name

            def get(self, path):
                if not self.ok:
                    raise ProxmoxAPIError("ConnectError")
                return [
                    {
                        "filename": "pve-root-ca.pem",
                        "subject": "OU=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa,O=x",
                        "fingerprint": "FP",
                    }
                ]

        from core.services.cluster_identity import observe_cluster_identity

        with patch(
            "core.services.cluster_identity.client_for_endpoint",
            side_effect=lambda ep: Client(ep.name, ok=(ep.name == "a2")),
        ):
            observed = observe_cluster_identity(self.cluster)

        self.assertEqual(observed.ca_uuid, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    def test_all_endpoints_down_raises(self):
        from core.services.cluster_identity import ClusterIdentityError, observe_cluster_identity
        from core.services.proxmox import ProxmoxAPIError

        class Down:
            def discover_node_name(self, fallback):
                return fallback

            def get(self, path):
                raise ProxmoxAPIError("ConnectError")

        with patch("core.services.cluster_identity.client_for_endpoint", return_value=Down()):
            with self.assertRaises(ClusterIdentityError):
                observe_cluster_identity(self.cluster)


class QuarantineBlocksAcquisitionTests(TestCase):
    def test_a_quarantined_cluster_refuses_reads(self):
        cluster = ProxmoxCluster.objects.create(
            key="a",
            display_name="A",
            enabled=True,
            ingestion_quarantined=True,
            quarantine_reason="CA mismatch",
        )
        ProxmoxEndpoint.objects.create(name="a1", url="https://a1:8006", cluster=cluster, enabled=True)

        with self.assertRaises(ClusterQuarantinedError):
            cluster_wide_read(cluster, operation="inventory", call=lambda c: c.get("x"))


@override_settings(PVE_CA_BUNDLE="", PVE_VERIFY_TLS=True)
class TrustCutoverTests(TestCase):
    def setUp(self):
        super().setUp()
        self.cluster = ProxmoxCluster.objects.create(key="default", display_name="D", enabled=True)
        self.state = RuntimeConfigurationState.objects.create(
            pk=RuntimeConfigurationState.SINGLETON_PK, bootstrap_completed=True
        )

    def test_cutover_seals_public_trust_and_records_marker(self):
        with patch("core.services.cluster_trust.reset_trust_pools"):
            changed, _message = complete_trust_cutover()

        self.assertTrue(changed)
        trust = ClusterTransportTrust.objects.get(cluster=self.cluster)
        self.assertEqual(trust.mode, ClusterTransportTrust.Mode.PUBLIC)
        self.state.refresh_from_db()
        self.assertIsNotNone(self.state.trust_cutover_completed_at)

    def test_cutover_is_not_repeated(self):
        with patch("core.services.cluster_trust.reset_trust_pools"):
            complete_trust_cutover()
            changed, message = complete_trust_cutover()

        self.assertFalse(changed)
        self.assertIn("already", message)

    def test_approving_transport_invalidates_pools(self):
        with patch("core.services.cluster_trust.reset_trust_pools") as reset:
            approve_cluster_transport(self.cluster, mode=ClusterTransportTrust.Mode.PUBLIC)

        reset.assert_called_once()


@override_settings(
    PVE_CA_BUNDLE="",
    PVE_VERIFY_TLS=True,
    PVE_API_TOKEN_ID="root@pam!test",
    PVE_API_TOKEN_SECRET="test-secret",
)
class TrustProfileInjectionTests(TestCase):
    def test_client_carries_the_clusters_trust_profile(self):
        cluster = ProxmoxCluster.objects.create(key="a", display_name="A", enabled=True)
        ClusterTransportTrust.objects.create(cluster=cluster, mode=ClusterTransportTrust.Mode.CA_PEM, ca_pem="CA-X")
        endpoint = ProxmoxEndpoint.objects.create(name="a1", url="https://a1:8006", cluster=cluster, enabled=True)

        client = client_for_endpoint(endpoint)

        self.assertEqual(client._trust_profile, TrustProfile(mode=TRUST_CA_PEM, ca_pem="CA-X"))


class TlsTrustFailureClassificationTests(SimpleTestCase):
    """A rejected chain is local configuration, and says so.

    The generic transport error is redacted at the boundary because it carries
    provider diagnostics. This one carries none and is the only transport failure
    the operator repairs, so it must survive `public_failure` intact — and name the
    trust profile that did the rejecting, since that is what decides the repair.
    """

    def _connect_error(self) -> httpx.ConnectError:
        verification = ssl.SSLCertVerificationError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate"
        )
        # httpx wraps httpcore wraps ssl, so the ssl error is never the direct cause.
        inner = httpx.ConnectError("connection failed")
        inner.__cause__ = verification
        outer = httpx.ConnectError("connection failed")
        outer.__cause__ = inner
        return outer

    def _get(self, profile: TrustProfile, exc: Exception):
        client = ProxmoxClient("https://node.example.test:8006", trust_profile=profile)
        failing = MagicMock()
        failing.request.side_effect = exc
        with patch.object(ProxmoxClient, "_http_client", return_value=failing):
            with self.assertRaises(ProxmoxAPIError) as caught:
                client.get("version")
        return caught.exception

    def test_a_verification_failure_becomes_a_public_trust_error(self):
        raised = self._get(TrustProfile(mode=TRUST_PUBLIC), self._connect_error())

        self.assertIsInstance(raised, ProxmoxTlsTrustError)
        self.assertIn("public CA store", str(raised))
        self.assertEqual(
            public_failure(raised, operation="test", fallback=PROVIDER_FAILURE_MESSAGE).message,
            str(raised),
        )

    def test_the_message_names_the_trust_profile_that_rejected_the_chain(self):
        raised = self._get(TrustProfile(mode=TRUST_CA_PEM, ca_pem=FAKE_CA), self._connect_error())

        self.assertIn("internal CA bundle", str(raised))

    def test_a_rejected_chain_proves_nothing_was_sent(self):
        raised = self._get(TrustProfile(mode=TRUST_PUBLIC), self._connect_error())

        self.assertFalse(raised.ambiguous)

    def test_an_ordinary_connect_failure_is_still_redacted(self):
        raised = self._get(TrustProfile(mode=TRUST_PUBLIC), httpx.ConnectError("connection refused"))

        self.assertNotIsInstance(raised, ProxmoxTlsTrustError)
        self.assertEqual(
            public_failure(raised, operation="test", fallback=PROVIDER_FAILURE_MESSAGE).message,
            PROVIDER_FAILURE_MESSAGE,
        )


class AcceptedCertificateProbeTests(SimpleTestCase):
    """The probe that turns "a sibling's certificate" into "a certificate we accept".

    Offering a working endpoint's certificate as the example to follow is only
    honest if the same trust profile has been asked about it, so the probe completes
    a verifying handshake rather than an inspection.
    """

    url = "https://pve201.example.test:8006"

    def _probe(self, profile, **patches):
        return accepted_endpoint_certificate(self.url, profile, **patches)

    def test_a_rejected_chain_yields_no_example_instead_of_raising(self):
        """It runs while composing another failure's page; a second exception here
        would replace a diagnosis with a stack trace."""

        with patch("socket.create_connection", side_effect=ssl.SSLCertVerificationError("verify failed")):
            certificate = self._probe(TrustProfile(mode=TRUST_PUBLIC))

        self.assertEqual(certificate.sha256_fingerprint, "")

    def test_an_unreachable_sibling_yields_no_example(self):
        with patch("socket.create_connection", side_effect=OSError("no route to host")):
            certificate = self._probe(TrustProfile(mode=TRUST_PUBLIC))

        self.assertEqual(certificate.sha256_fingerprint, "")

    def test_an_insecure_profile_is_no_evidence_about_any_certificate(self):
        """It accepts every chain, so "accepted here" says nothing about what another
        node must install."""

        with patch("socket.create_connection") as connect:
            certificate = self._probe(TrustProfile(mode=TRUST_INSECURE))

        self.assertEqual(certificate.sha256_fingerprint, "")
        connect.assert_not_called()

    def test_an_accepted_chain_returns_the_certificate_it_verified(self):
        peer = MagicMock()
        peer.__enter__ = lambda _self: peer
        peer.__exit__ = lambda *_args: False
        peer.getpeercert.return_value = b"der"
        context = MagicMock()
        context.wrap_socket.return_value = peer
        inspected = InspectedCertificate(subject="CN=pve201", issuer="CN=R11", sha256_fingerprint="ab01")

        with (
            patch("socket.create_connection"),
            patch("ssl.create_default_context", return_value=context),
            patch("core.services.cluster_trust._inspected", return_value=inspected),
        ):
            certificate = self._probe(TrustProfile(mode=TRUST_PUBLIC))

        self.assertEqual(certificate, inspected)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)


def _self_signed_ca(common_name: str = "Proxmox Virtual Environment", ou: str = "e4b043e3-0000-4000-8000-000000000000"):
    """A real, parseable self-signed CA.

    The additive mode's whole claim is about what an `SSLContext` ends up trusting,
    and `load_verify_locations` parses. `FAKE_CA` above is deliberately not a
    certificate, so these tests generate one instead of asserting on a mock.
    """
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, ou),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "PVE Cluster Manager CA"),
        ]
    )
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


class AdditiveTrustProfileTests(SimpleTestCase):
    """`public_ca_pem`: the public store *and* this cluster's CA.

    Chain acceptance cannot be asserted without a handshake harness this repo does
    not have, so the assertions are over what the context actually loaded — which is
    the decision the handshake would then apply.
    """

    def test_the_additive_context_carries_both_anchors(self):
        ca_pem = _self_signed_ca()
        profile = TrustProfile(mode=TRUST_PUBLIC_PLUS_CA, ca_pem=ca_pem)

        context = profile.build_verify()

        loaded = context.get_ca_certs()
        subjects = {tuple(part for rdn in cert["subject"] for part in rdn) for cert in loaded}
        self.assertIn(
            ("organizationName", "PVE Cluster Manager CA"), {pair for subject in subjects for pair in subject}
        )
        # More than the one CA we added: the system store is still there, which is
        # the whole difference from `ca_pem` and the reason pve201 keeps working.
        self.assertGreater(len(loaded), 1)

    def test_the_exclusive_mode_still_trusts_only_its_bundle(self):
        profile = TrustProfile(mode=TRUST_CA_PEM, ca_pem=_self_signed_ca())

        self.assertEqual(len(profile.build_verify().get_ca_certs()), 1)

    def test_two_clusters_in_the_additive_mode_key_different_pools(self):
        # Keying on the mode alone would run one cluster's traffic on the other's
        # anchor — the exact defect the pool exists to prevent.
        self.assertNotEqual(
            TrustProfile(mode=TRUST_PUBLIC_PLUS_CA, ca_pem="CA-X").cache_key(),
            TrustProfile(mode=TRUST_PUBLIC_PLUS_CA, ca_pem="CA-Y").cache_key(),
        )

    def test_the_additive_mode_never_shares_a_pool_with_the_exclusive_one(self):
        self.assertNotEqual(
            TrustProfile(mode=TRUST_PUBLIC_PLUS_CA, ca_pem="CA-X").cache_key(),
            TrustProfile(mode=TRUST_CA_PEM, ca_pem="CA-X").cache_key(),
        )

    def test_the_additive_mode_needs_a_bundle(self):
        with self.assertRaises(TransportTrustError):
            TrustProfile(mode=TRUST_PUBLIC_PLUS_CA).build_verify()

    def test_the_console_context_matches_the_http_decision(self):
        # `ssl_context_for` was a parallel mode table; a mode added to one and not
        # the other leaves the console silently on the default store.
        profile = TrustProfile(mode=TRUST_PUBLIC_PLUS_CA, ca_pem=_self_signed_ca())

        self.assertEqual(
            {cert["serialNumber"] for cert in ssl_context_for(profile).get_ca_certs()},
            {cert["serialNumber"] for cert in profile.build_verify().get_ca_certs()},
        )

    def test_each_call_builds_a_fresh_context(self):
        # `accepted_endpoint_certificate` mutates what it is handed, so a memoized
        # context would leak that mutation into every later connection.
        profile = TrustProfile(mode=TRUST_PUBLIC_PLUS_CA, ca_pem=_self_signed_ca())

        self.assertIsNot(profile.build_verify(), profile.build_verify())


class StoredAdditiveTrustTests(TestCase):
    def test_a_stored_additive_mode_resolves_with_its_bundle(self):
        cluster = ProxmoxCluster.objects.create(key="ca-additive", display_name="CA additive")
        ClusterTransportTrust.objects.create(
            cluster=cluster, mode=ClusterTransportTrust.Mode.PUBLIC_PLUS_CA, ca_pem="CA-X"
        )

        profile = resolve_trust_profile(cluster)

        self.assertEqual(profile.mode, TRUST_PUBLIC_PLUS_CA)
        self.assertEqual(profile.ca_pem, "CA-X")

    def test_an_unknown_stored_mode_falls_back_to_public(self):
        # The rollback story: an older build reading a newer mode loses the internal
        # node and keeps verifying, rather than failing open.
        cluster = ProxmoxCluster.objects.create(key="ca-unknown", display_name="CA unknown")
        trust = ClusterTransportTrust.objects.create(cluster=cluster, mode=ClusterTransportTrust.Mode.PUBLIC)
        ClusterTransportTrust.objects.filter(pk=trust.pk).update(mode="mode_from_the_future")

        self.assertEqual(resolve_trust_profile(cluster).mode, TRUST_PUBLIC)

    def test_approval_stores_the_bundle_and_its_evidence(self):
        cluster = ProxmoxCluster.objects.create(key="ca-store", display_name="CA store")

        approve_cluster_transport(
            cluster,
            mode=ClusterTransportTrust.Mode.PUBLIC_PLUS_CA,
            ca_pem="CA-X",
            details={"ca_uuid": "e4b043e3"},
        )

        stored = ClusterTransportTrust.objects.get(cluster=cluster)
        self.assertEqual(stored.ca_pem, "CA-X")
        self.assertEqual(stored.details["ca_uuid"], "e4b043e3")

    def test_the_additive_mode_refuses_an_empty_bundle(self):
        cluster = ProxmoxCluster.objects.create(key="ca-empty", display_name="CA empty")

        with self.assertRaises(TransportTrustError):
            approve_cluster_transport(cluster, mode=ClusterTransportTrust.Mode.PUBLIC_PLUS_CA, ca_pem="   ")

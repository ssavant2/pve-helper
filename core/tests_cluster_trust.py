from __future__ import annotations

from unittest.mock import patch

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
    TransportTrustError,
    TrustProfile,
    approve_cluster_transport,
    complete_trust_cutover,
    legacy_trust_profile,
    resolve_trust_profile,
)

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

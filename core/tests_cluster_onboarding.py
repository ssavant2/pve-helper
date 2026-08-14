from __future__ import annotations

import base64
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase, override_settings

from core.models import (
    ClusterCredential,
    ClusterMembershipState,
    ClusterTransportTrust,
    ProxmoxCluster,
    ProxmoxEndpoint,
    RuntimeConfigurationState,
)
from core.services.cluster_identity import ClusterIdentityError, ObservedClusterIdentity
from core.services.cluster_onboarding import (
    ClusterCandidate,
    ClusterOnboardingError,
    ClusterTrustMismatchError,
    VerifiedConnection,
    _trust_mismatch,
    persist_new_cluster,
    verify_new_cluster,
    verify_replacement_credential,
)
from core.services.cluster_topology_role import TopologyRole
from core.services.cluster_trust import (
    TRUST_PUBLIC,
    InspectedCertificate,
    TrustProfile,
    approve_cluster_transport,
)
from core.services.proxmox import ProxmoxAPIError, ProxmoxTlsTrustError
from core.services.public_errors import PROVIDER_FAILURE_MESSAGE

TEST_KEY = base64.b64encode(b"o" * 32).decode()


class _CandidateClient:
    permissions = {"/": {"VM.Audit": 1, "VM.PowerMgmt": 1}}
    role = {"VM.Audit": 1, "VM.PowerMgmt": 1}

    def __init__(self, endpoint, *, credential, trust_profile):
        self.endpoint = endpoint
        self.credential = credential
        self.trust_profile = trust_profile

    def get(self, path):
        values = {
            "version": {"version": "9.2.4"},
            "nodes": [{"node": "pve201"}],
            "access/permissions": self.permissions,
            "access/roles/Administrator": self.role,
            "cluster/status": [{"type": "cluster", "name": "Candidate Cluster"}],
        }
        return values[path]


@override_settings(
    PVE_HELPER_ENCRYPTION_KEYS=f"test:{TEST_KEY}",
    PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID="test",
)
class ClusterOnboardingTests(TestCase):
    def setUp(self):
        RuntimeConfigurationState.objects.create(
            bootstrap_completed=True,
            identity_contract_version=1,
        )
        self.certificate = InspectedCertificate(
            subject="CN=pve201.example.test",
            issuer="CN=Example CA",
            sha256_fingerprint="abc123",
        )
        self.identity = ObservedClusterIdentity(
            ca_uuid="11111111-1111-1111-1111-111111111111",
            ca_fingerprint="AA:BB",
        )
        self.candidate = ClusterCandidate(
            key="clusterb",
            display_name="Cluster B",
            endpoint_url="https://pve201.example.test:8006",
            endpoint_name="pve201",
            trust_mode=TRUST_PUBLIC,
            token_id="pve-helper@pve!pve-helper",
            token_secret="super-secret",
        )

    def _verify(self):
        with (
            patch(
                "core.services.cluster_onboarding.inspect_transport",
                return_value=self.certificate,
            ),
            patch("core.services.cluster_onboarding.ProxmoxClient", _CandidateClient),
            patch(
                "core.services.cluster_onboarding.discover_cluster_identity",
                return_value=self.identity,
            ),
        ):
            return verify_new_cluster(
                self.candidate,
                expected_certificate_fingerprint=self.certificate.sha256_fingerprint,
            )

    def test_rejects_proxmox_older_than_supported_baseline(self):
        class OldVersionClient(_CandidateClient):
            def get(self, path):
                if path == "version":
                    return {"version": "8.4.14"}
                return super().get(path)

        with (
            patch(
                "core.services.cluster_onboarding.inspect_transport",
                return_value=self.certificate,
            ),
            patch("core.services.cluster_onboarding.ProxmoxClient", OldVersionClient),
        ):
            with self.assertRaisesMessage(
                ClusterOnboardingError,
                "Proxmox VE 9.2 or later is required; the endpoint reports 8.4.14.",
            ):
                verify_new_cluster(
                    self.candidate,
                    expected_certificate_fingerprint=self.certificate.sha256_fingerprint,
                )

    def test_rejects_unverifiable_proxmox_version(self):
        class MissingVersionClient(_CandidateClient):
            def get(self, path):
                if path == "version":
                    return {}
                return super().get(path)

        with (
            patch(
                "core.services.cluster_onboarding.inspect_transport",
                return_value=self.certificate,
            ),
            patch("core.services.cluster_onboarding.ProxmoxClient", MissingVersionClient),
        ):
            with self.assertRaisesMessage(
                ClusterOnboardingError,
                "Could not verify the Proxmox VE version.",
            ):
                verify_new_cluster(
                    self.candidate,
                    expected_certificate_fingerprint=self.certificate.sha256_fingerprint,
                )

    def test_verification_uses_ephemeral_secret_and_persists_nothing(self):
        candidate, verified = self._verify()

        self.assertEqual(candidate.key, "clusterb")
        self.assertEqual(verified.identity, self.identity)
        self.assertEqual(verified.node_names, ("pve201",))
        self.assertEqual(ProxmoxCluster.objects.count(), 0)
        self.assertNotIn("super-secret", repr(candidate))

    def test_handoff_verification_narrowly_reuses_source_endpoint_and_ca(self):
        source = ProxmoxCluster.objects.create(
            key="source",
            display_name="Source",
            enabled=True,
            discovered_ca_uuid=self.identity.ca_uuid,
        )
        ProxmoxEndpoint.objects.create(
            cluster=source,
            name=self.candidate.endpoint_name,
            url=self.candidate.endpoint_url,
        )
        ClusterMembershipState.objects.create(
            cluster=source,
            topology_role=TopologyRole.STANDALONE,
            transition_pending=True,
            pending_topology_role=TopologyRole.COROSYNC,
        )

        class CompleteCorosyncClient(_CandidateClient):
            def get(self, path):
                if path == "cluster/status":
                    return [
                        {"type": "cluster", "name": "Candidate Cluster", "nodes": 1, "quorate": 1},
                        {"type": "node", "name": "pve201", "nodeid": 1, "online": 1, "local": 1},
                    ]
                return super().get(path)

        with (
            patch("core.services.cluster_onboarding.inspect_transport", return_value=self.certificate),
            patch("core.services.cluster_onboarding.ProxmoxClient", CompleteCorosyncClient),
            patch("core.services.cluster_onboarding.discover_cluster_identity", return_value=self.identity),
        ):
            candidate, verified = verify_new_cluster(
                self.candidate,
                expected_certificate_fingerprint=self.certificate.sha256_fingerprint,
                handoff_from=source,
            )

        self.assertEqual(candidate.endpoint_url, self.candidate.endpoint_url)
        self.assertEqual(verified.identity.ca_uuid, source.discovered_ca_uuid)
        self.assertTrue(verified.membership_complete)
        self.assertEqual(verified.topology_role, TopologyRole.COROSYNC)

        with self.assertRaisesMessage(ClusterOnboardingError, "already registered"):
            verify_new_cluster(
                self.candidate,
                expected_certificate_fingerprint=self.certificate.sha256_fingerprint,
            )

    def test_handoff_verification_rejects_non_strict_membership_payload(self):
        source = ProxmoxCluster.objects.create(key="source", display_name="Source", enabled=True)
        ClusterMembershipState.objects.create(
            cluster=source,
            topology_role=TopologyRole.STANDALONE,
            transition_pending=True,
            pending_topology_role=TopologyRole.COROSYNC,
        )
        with patch(
            "core.services.cluster_onboarding._verify_connection",
            return_value=VerifiedConnection(
                certificate=self.certificate,
                identity=self.identity,
                node_names=("pve201",),
                version="9.2.4",
                discovered_name="Candidate Cluster",
                administrator_privileges=("VM.Audit",),
                topology_role=TopologyRole.UNKNOWN,
                membership_complete=False,
            ),
        ):
            with self.assertRaisesMessage(ClusterOnboardingError, "fresh complete membership"):
                verify_new_cluster(
                    self.candidate,
                    expected_certificate_fingerprint=self.certificate.sha256_fingerprint,
                    handoff_from=source,
                )

    def test_verification_requires_every_current_administrator_privilege(self):
        class LimitedClient(_CandidateClient):
            permissions = {"/": {"VM.Audit": 1}}

        with (
            patch(
                "core.services.cluster_onboarding.inspect_transport",
                return_value=self.certificate,
            ),
            patch("core.services.cluster_onboarding.ProxmoxClient", LimitedClient),
        ):
            with self.assertRaisesMessage(
                ClusterOnboardingError,
                "Missing: VM.PowerMgmt",
            ):
                verify_new_cluster(
                    self.candidate,
                    expected_certificate_fingerprint=self.certificate.sha256_fingerprint,
                )

    def test_certificate_change_is_rejected_before_credentials_are_sent(self):
        changed = InspectedCertificate(
            subject=self.certificate.subject,
            issuer=self.certificate.issuer,
            sha256_fingerprint="changed",
        )
        with (
            patch("core.services.cluster_onboarding.inspect_transport", return_value=changed),
            patch("core.services.cluster_onboarding.ProxmoxClient") as client_class,
        ):
            with self.assertRaisesMessage(ClusterOnboardingError, "changed after inspection"):
                verify_new_cluster(
                    self.candidate,
                    expected_certificate_fingerprint=self.certificate.sha256_fingerprint,
                )
        client_class.assert_not_called()

    def test_a_rejected_certificate_chain_names_the_certificate_to_trust(self):
        """The repair needs the issuer, so the failure carries it as fields.

        "The Proxmox API request failed" sent the operator to look at the token and
        the node's cluster membership; the actual fault was that nothing here trusts
        the CA that signed what the node serves. The evidence lives on the exception
        rather than inside its sentence, because the surface lays two certificates
        side by side and a paragraph cannot be compared.
        """

        class UntrustedClient(_CandidateClient):
            def get(self, path):
                raise ProxmoxTlsTrustError("The endpoint's TLS certificate was rejected.", request_sent=False)

        with (
            patch("core.services.cluster_onboarding.inspect_transport", return_value=self.certificate),
            patch("core.services.cluster_onboarding.ProxmoxClient", UntrustedClient),
        ):
            with self.assertRaises(ClusterTrustMismatchError) as caught:
                verify_new_cluster(
                    self.candidate,
                    expected_certificate_fingerprint=self.certificate.sha256_fingerprint,
                )

        message = str(caught.exception)
        self.assertIn("does not trust", message)
        self.assertNotIn(PROVIDER_FAILURE_MESSAGE, message)
        diagnosis = caught.exception.diagnosis
        self.assertEqual(diagnosis.presented, self.certificate)
        self.assertEqual(diagnosis.trust_mode, TRUST_PUBLIC)
        self.assertIn("public CA store", diagnosis.trust_summary)
        # A brand-new cluster has no sibling to compare against, and inventing an
        # "accepted" certificate from nothing would be the guess this whole panel
        # exists to replace.
        self.assertIsNone(diagnosis.reference)
        self.assertTrue(any("approve_cluster_transport" in remedy for remedy in diagnosis.remedies))

    def test_an_ordinary_provider_failure_stays_redacted(self):
        class BrokenClient(_CandidateClient):
            def get(self, path):
                raise ProxmoxAPIError("500 from /api2/json/version: internal detail")

        with (
            patch("core.services.cluster_onboarding.inspect_transport", return_value=self.certificate),
            patch("core.services.cluster_onboarding.ProxmoxClient", BrokenClient),
        ):
            with self.assertRaises(ClusterOnboardingError) as caught:
                verify_new_cluster(
                    self.candidate,
                    expected_certificate_fingerprint=self.certificate.sha256_fingerprint,
                )

        message = str(caught.exception)
        self.assertIn(PROVIDER_FAILURE_MESSAGE, message)
        self.assertNotIn("internal detail", message)

    def test_identity_discovery_fails_over_visible_cluster_nodes(self):
        class RedundantClient(_CandidateClient):
            def get(self, path):
                if path == "nodes":
                    return [{"node": "pve3"}, {"node": "pve99"}]
                return super().get(path)

        with (
            patch(
                "core.services.cluster_onboarding.inspect_transport",
                return_value=self.certificate,
            ),
            patch("core.services.cluster_onboarding.ProxmoxClient", RedundantClient),
            patch(
                "core.services.cluster_onboarding.discover_cluster_identity",
                side_effect=[ClusterIdentityError("node is down"), self.identity],
            ) as discover,
        ):
            _candidate, verified = verify_new_cluster(
                self.candidate,
                expected_certificate_fingerprint=self.certificate.sha256_fingerprint,
            )

        self.assertEqual(verified.identity, self.identity)
        self.assertEqual([call.args[1] for call in discover.call_args_list], ["pve3", "pve99"])

    def test_persist_writes_one_complete_enabled_configuration(self):
        verified = VerifiedConnection(
            certificate=self.certificate,
            identity=self.identity,
            node_names=("pve201",),
            version="9.2.4",
            discovered_name="Candidate Cluster",
            administrator_privileges=("VM.Audit", "VM.PowerMgmt"),
        )

        cluster = persist_new_cluster(self.candidate, verified)

        self.assertTrue(cluster.enabled)
        self.assertEqual(cluster.discovered_ca_uuid, self.identity.ca_uuid)
        self.assertTrue(ProxmoxEndpoint.objects.filter(cluster=cluster, name="pve201").exists())
        self.assertEqual(ClusterTransportTrust.objects.get(cluster=cluster).mode, TRUST_PUBLIC)
        credential = ClusterCredential.objects.get(cluster=cluster)
        self.assertEqual(credential.token_id, self.candidate.token_id)
        self.assertNotIn(self.candidate.token_secret, credential.token_secret_sealed)

    def test_first_wizard_cluster_activates_without_creating_a_default_cluster(self):
        RuntimeConfigurationState.objects.all().delete()
        verified = VerifiedConnection(
            certificate=self.certificate,
            identity=self.identity,
            node_names=("pve201",),
            version="9.2.4",
            discovered_name="Candidate Cluster",
            administrator_privileges=("VM.Audit", "VM.PowerMgmt"),
        )

        with self.settings(PVE_ENDPOINTS=[]):
            cluster = persist_new_cluster(self.candidate, verified)

        self.assertEqual(list(ProxmoxCluster.objects.values_list("key", flat=True)), ["clusterb"])
        self.assertTrue(cluster.enabled)
        state = RuntimeConfigurationState.objects.get()
        self.assertTrue(state.bootstrap_completed)
        self.assertEqual(state.identity_contract_version, 1)

    def test_connection_verification_tolerates_one_down_redundant_endpoint(self):
        cluster = ProxmoxCluster.objects.create(
            key="clusterhq",
            display_name="Cluster HQ",
            enabled=True,
            discovered_ca_uuid=self.identity.ca_uuid,
        )
        ProxmoxEndpoint.objects.create(
            cluster=cluster,
            name="pve3",
            url="https://pve3.example.test:8006",
        )
        ProxmoxEndpoint.objects.create(
            cluster=cluster,
            name="pve99",
            url="https://pve99.example.test:8006",
        )
        approve_cluster_transport(cluster, mode=TRUST_PUBLIC)
        verified = VerifiedConnection(
            certificate=self.certificate,
            identity=self.identity,
            node_names=("pve3", "pve99"),
            version="9.2.4",
            discovered_name="Cluster HQ",
            administrator_privileges=("VM.Audit",),
        )

        with patch(
            "core.services.cluster_onboarding._verify_connection",
            side_effect=[ClusterOnboardingError("unreachable"), verified],
        ) as connection_check:
            result = verify_replacement_credential(
                cluster,
                token_id=self.candidate.token_id,
                token_secret=self.candidate.token_secret,
            )

        self.assertEqual(result, verified)
        self.assertEqual(connection_check.call_count, 2)

    def test_connection_verification_never_hides_a_repointed_redundant_endpoint(self):
        cluster = ProxmoxCluster.objects.create(
            key="clusterhq",
            display_name="Cluster HQ",
            enabled=True,
            discovered_ca_uuid=self.identity.ca_uuid,
        )
        for name in ("pve3", "pve99"):
            ProxmoxEndpoint.objects.create(
                cluster=cluster,
                name=name,
                url=f"https://{name}.example.test:8006",
            )
        approve_cluster_transport(cluster, mode=TRUST_PUBLIC)
        expected = VerifiedConnection(
            certificate=self.certificate,
            identity=self.identity,
            node_names=("pve3", "pve99"),
            version="9.2.4",
            discovered_name="Cluster HQ",
            administrator_privileges=("VM.Audit",),
        )
        wrong = VerifiedConnection(
            certificate=self.certificate,
            identity=ObservedClusterIdentity(
                ca_uuid="99999999-9999-9999-9999-999999999999",
                ca_fingerprint="99:99",
            ),
            node_names=("pve201",),
            version="9.2.4",
            discovered_name="Wrong cluster",
            administrator_privileges=("VM.Audit",),
        )

        with patch(
            "core.services.cluster_onboarding._verify_connection",
            side_effect=[expected, wrong],
        ):
            with self.assertRaisesMessage(
                ClusterOnboardingError,
                "different Proxmox CA identities",
            ):
                verify_replacement_credential(
                    cluster,
                    token_id=self.candidate.token_id,
                    token_secret=self.candidate.token_secret,
                )


class TrustDiagnosisTests(SimpleTestCase):
    """The evidence a trust rejection hands the surface.

    The message used to be a paragraph holding a subject, an issuer and a
    fingerprint, and the one thing it never held was the certificate that *works* —
    so the operator was told to install something acceptable without being shown
    what this connection accepts.
    """

    presented = InspectedCertificate(
        subject="CN=pve202.example.test,O=Proxmox Virtual Environment,OU=PVE Cluster Node",
        issuer="O=PVE Cluster Manager CA,CN=Proxmox Virtual Environment",
        sha256_fingerprint="dd76",
    )
    accepted = InspectedCertificate(
        subject="CN=pve201.example.test",
        issuer="C=US,O=Let's Encrypt,CN=R11",
        sha256_fingerprint="ab01",
    )
    nothing = InspectedCertificate(subject="", issuer="", sha256_fingerprint="")

    def _diagnose(self, certificates, endpoints):
        with patch(
            "core.services.cluster_onboarding.accepted_endpoint_certificate",
            side_effect=certificates,
        ):
            error = _trust_mismatch(
                endpoint_url="https://pve202.example.test:8006",
                endpoint_name="pve202",
                trust_profile=TrustProfile(mode=TRUST_PUBLIC),
                certificate=self.presented,
                reference_endpoints=endpoints,
            )
        return error.diagnosis

    def test_the_example_is_the_first_sibling_the_profile_actually_accepts(self):
        """Asked, not assumed. A sibling that has drifted out of trust itself is not
        an example to follow, so it is passed over rather than offered."""

        diagnosis = self._diagnose(
            [self.nothing, self.accepted],
            (("pve200", "https://pve200.example.test:8006"), ("pve201", "https://pve201.example.test:8006")),
        )

        self.assertEqual(diagnosis.reference.endpoint_name, "pve201")
        self.assertEqual(diagnosis.reference.certificate, self.accepted)

    def test_the_example_is_an_issuer_to_match_not_a_certificate_to_copy(self):
        """Only the issuer transfers between nodes; the names do not, and whether the
        sibling's certificate happens to cover this node too is not something the
        remedy has looked at. So it names the issuer as the thing to match and stops
        short of claiming a copy would or would not work."""

        diagnosis = self._diagnose([self.accepted], (("pve201", "https://pve201.example.test:8006"),))

        remedy = next(text for text in diagnosis.remedies if "same CA" in text)
        self.assertIn("The issuer is what has to match", remedy)
        self.assertIn("not as a file to copy", remedy)
        self.assertIn("pve202", remedy)
        self.assertIn("pve201", remedy)

    def test_the_endpoint_being_added_is_never_its_own_example(self):
        """It is already registered on the re-verify path, and its certificate is the
        rejected one — offering it back would be a comparison with itself."""

        diagnosis = self._diagnose(
            [self.accepted],
            (("pve202", "https://pve202.example.test:8006/"), ("pve201", "https://pve201.example.test:8006")),
        )

        self.assertEqual(diagnosis.reference.endpoint_name, "pve201")

    def test_no_accepted_sibling_says_so_rather_than_going_quiet(self):
        diagnosis = self._diagnose([self.nothing], (("pve201", "https://pve201.example.test:8006"),))

        self.assertIsNone(diagnosis.reference)
        self.assertTrue(any("no working example" in remedy for remedy in diagnosis.remedies))

    def test_a_connection_with_no_endpoints_makes_no_claim_about_siblings(self):
        """Onboarding a new cluster has nothing to compare against, which is not the
        same finding as "the siblings are untrusted too"."""

        diagnosis = self._diagnose([], ())

        self.assertIsNone(diagnosis.reference)
        self.assertFalse(any("no working example" in remedy for remedy in diagnosis.remedies))

    def test_the_surface_renders_both_certificates(self):
        diagnosis = self._diagnose([self.accepted], (("pve201", "https://pve201.example.test:8006"),))

        html = render_to_string("core/partials/cluster_trust_diagnosis.html", {"diagnosis": diagnosis})

        self.assertIn("Rejected", html)
        self.assertIn("Accepted today", html)
        self.assertIn(self.presented.sha256_fingerprint, html)
        self.assertIn(self.accepted.sha256_fingerprint, html)
        self.assertIn("public CA store", html)

from __future__ import annotations

import base64
from unittest.mock import patch

from django.test import TestCase, override_settings

from core.models import (
    ClusterCredential,
    ClusterTransportTrust,
    ProxmoxCluster,
    ProxmoxEndpoint,
    RuntimeConfigurationState,
)
from core.services.cluster_identity import ClusterIdentityError, ObservedClusterIdentity
from core.services.cluster_onboarding import (
    ClusterCandidate,
    ClusterOnboardingError,
    VerifiedConnection,
    persist_new_cluster,
    verify_replacement_credential,
    verify_new_cluster,
)
from core.services.cluster_trust import InspectedCertificate, TRUST_PUBLIC, approve_cluster_transport


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

    def test_verification_uses_ephemeral_secret_and_persists_nothing(self):
        candidate, verified = self._verify()

        self.assertEqual(candidate.key, "clusterb")
        self.assertEqual(verified.identity, self.identity)
        self.assertEqual(verified.node_names, ("pve201",))
        self.assertEqual(ProxmoxCluster.objects.count(), 0)
        self.assertNotIn("super-secret", repr(candidate))

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

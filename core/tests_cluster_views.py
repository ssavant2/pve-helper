from __future__ import annotations

import base64
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import (
    AuditEvent,
    ClusterCredential,
    ProxmoxCluster,
    ProxmoxEndpoint,
    RuntimeConfigurationState,
)
from core.services.cluster_credentials import set_cluster_credential
from core.services.cluster_identity import ObservedClusterIdentity
from core.services.cluster_onboarding import (
    ClusterCandidate,
    ClusterOnboardingError,
    VerifiedConnection,
    disable_cluster,
)
from core.services.cluster_trust import (
    InspectedCertificate,
    TRUST_PUBLIC,
    approve_cluster_transport,
)


TEST_KEY = base64.b64encode(b"v" * 32).decode()


@override_settings(
    APP_REQUIRE_LOGIN=False,
    PVE_HELPER_ENCRYPTION_KEYS=f"test:{TEST_KEY}",
    PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID="test",
)
class ClusterConnectionViewTests(TestCase):
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
            ca_uuid="22222222-2222-2222-2222-222222222222",
            ca_fingerprint="AA:22",
        )
        self.verified = VerifiedConnection(
            certificate=self.certificate,
            identity=self.identity,
            node_names=("pve201",),
            version="9.2.4",
            discovered_name="Cluster B",
            administrator_privileges=("VM.Audit", "VM.PowerMgmt"),
        )
        self.candidate = ClusterCandidate(
            key="clusterb",
            display_name="Cluster B",
            endpoint_url="https://pve201.example.test:8006",
            endpoint_name="pve201",
            trust_mode=TRUST_PUBLIC,
            token_id="pve-helper@pve!pve-helper",
            token_secret="never-render-this",
        )

    def _inspection_token(self):
        with patch("core.views.clusters.inspect_transport", return_value=self.certificate):
            response = self.client.post(
                reverse("core:cluster_add"),
                {
                    "action": "inspect",
                    "display_name": self.candidate.display_name,
                    "cluster_key": self.candidate.key,
                    "endpoint_url": self.candidate.endpoint_url,
                    "endpoint_name": self.candidate.endpoint_name,
                },
            )
        self.assertEqual(response.status_code, 200)
        return response.context["trust_form"]["inspection"].value()

    def _candidate_token(self):
        inspection = self._inspection_token()
        with patch(
            "core.views.clusters.verify_new_cluster",
            return_value=(self.candidate, self.verified),
        ):
            response = self.client.post(
                reverse("core:cluster_add"),
                {
                    "action": "verify",
                    "inspection": inspection,
                    "trust_mode": TRUST_PUBLIC,
                    "ca_pem": "",
                    "token_id": self.candidate.token_id,
                    "token_secret": self.candidate.token_secret,
                    "confirm_certificate": "on",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.identity.ca_uuid)
        self.assertNotContains(response, self.candidate.token_secret)
        return response.context["confirm_form"]["candidate"].value()

    def test_zero_cluster_state_has_onboarding_and_unscoped_tags_redirects_to_it(self):
        response = self.client.get(reverse("core:clusters_overview"))
        self.assertContains(response, "No Proxmox cluster is configured")
        self.assertContains(response, reverse("core:cluster_add"))

        response = self.client.get(reverse("core:legacy_tags_overview"))
        self.assertRedirects(response, reverse("core:cluster_add"), fetch_redirect_response=False)

    def test_zero_cluster_state_keeps_aggregate_views_usable(self):
        for route_name in (
            "core:dashboard",
            "core:datastores",
            "core:vms_overview",
            "core:vms",
            "core:scheduled_tasks",
            "core:audit_log",
        ):
            with self.subTest(route=route_name):
                response = self.client.get(reverse(route_name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, reverse("core:cluster_add"))

        search = self.client.get(reverse("core:global_search"))
        self.assertEqual(search.status_code, 200)
        self.assertJSONEqual(search.content, {"query": "", "results": []})

    def test_wizard_persists_only_after_identity_confirmation_and_never_renders_secret(self):
        candidate_token = self._candidate_token()

        self.assertNotIn(self.candidate.token_secret, candidate_token)
        self.assertFalse(ProxmoxCluster.objects.exists())

        with patch(
            "core.views.clusters.verify_new_cluster",
            return_value=(self.candidate, self.verified),
        ):
            response = self.client.post(
                reverse("core:cluster_add"),
                {
                    "action": "confirm",
                    "candidate": candidate_token,
                    "confirm_identity": "on",
                },
            )

        cluster = ProxmoxCluster.objects.get(key="clusterb")
        self.assertRedirects(
            response,
            reverse("core:cluster_connection", kwargs={"cluster_key": cluster.key}),
            fetch_redirect_response=False,
        )
        self.assertTrue(cluster.enabled)
        event = AuditEvent.objects.get(action="cluster.added")
        self.assertEqual(event.details["token_id"], self.candidate.token_id)
        self.assertNotIn(self.candidate.token_secret, str(event.details))

        detail = self.client.get(reverse("core:cluster_connection", kwargs={"cluster_key": cluster.key}))
        self.assertContains(detail, self.candidate.token_id)
        self.assertNotContains(detail, self.candidate.token_secret)

        audit = self.client.get(reverse("core:audit_log"))
        self.assertContains(audit, "Add cluster")
        self.assertContains(audit, self.candidate.display_name)

    def test_wizard_rejects_a_tampered_candidate_without_persisting(self):
        candidate_token = self._candidate_token()
        tampered = ("x" if candidate_token[0] != "x" else "y") + candidate_token[1:]

        response = self.client.post(
            reverse("core:cluster_add"),
            {
                "action": "confirm",
                "candidate": tampered,
                "confirm_identity": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "verification is invalid")
        self.assertFalse(ProxmoxCluster.objects.exists())

    def test_disable_refuses_active_cluster_operation(self):
        cluster = ProxmoxCluster.objects.create(key="clusterb", display_name="Cluster B", enabled=True)
        AuditEvent.objects.create(
            cluster=cluster,
            cluster_key_snapshot=cluster.key,
            action="guest.power.start",
            outcome="running",
        )

        with self.assertRaisesMessage(ClusterOnboardingError, "provider work is active"):
            disable_cluster(cluster)

        cluster.refresh_from_db()
        self.assertTrue(cluster.enabled)

    def test_add_endpoint_requires_two_explicit_confirmations(self):
        cluster = ProxmoxCluster.objects.create(
            key="clusterb",
            display_name="Cluster B",
            enabled=True,
            discovered_ca_uuid=self.identity.ca_uuid,
            discovered_ca_fingerprint=self.identity.ca_fingerprint,
        )
        ProxmoxEndpoint.objects.create(
            cluster=cluster,
            name="pve201",
            url="https://pve201.example.test:8006",
        )
        approve_cluster_transport(cluster, mode=TRUST_PUBLIC)
        set_cluster_credential(
            cluster,
            token_id=self.candidate.token_id,
            token_secret=self.candidate.token_secret,
        )
        add_url = reverse("core:cluster_endpoint_add", kwargs={"cluster_key": cluster.key})
        with patch("core.views.clusters.inspect_transport", return_value=self.certificate):
            inspected = self.client.post(
                add_url,
                {
                    "action": "inspect",
                    "endpoint_url": "https://pve202.example.test:8006",
                    "endpoint_name": "pve202",
                },
            )
        inspection = inspected.context["trust_form"]["inspection"].value()

        with patch("core.views.clusters.verify_endpoint_for_cluster", return_value=self.verified):
            verified = self.client.post(
                add_url,
                {"action": "verify", "inspection": inspection, "confirm_certificate": "on"},
            )
        endpoint_token = verified.context["confirm_form"]["endpoint"].value()
        self.assertFalse(ProxmoxEndpoint.objects.filter(name="pve202").exists())

        with patch("core.views.clusters.verify_endpoint_for_cluster", return_value=self.verified):
            response = self.client.post(
                add_url,
                {"action": "confirm", "endpoint": endpoint_token, "confirm_identity": "on"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ProxmoxEndpoint.objects.filter(cluster=cluster, name="pve202").exists())

    def test_reenabling_endpoint_reverifies_its_pinned_cluster_identity(self):
        cluster = ProxmoxCluster.objects.create(
            key="clusterb",
            display_name="Cluster B",
            enabled=True,
            discovered_ca_uuid=self.identity.ca_uuid,
        )
        endpoint = ProxmoxEndpoint.objects.create(
            cluster=cluster,
            name="pve201",
            url="https://pve201.example.test:8006",
            enabled=False,
        )

        with patch(
            "core.views.clusters.verify_registered_endpoint",
            return_value=self.verified,
        ) as verify:
            response = self.client.post(
                reverse(
                    "core:cluster_endpoint_action",
                    kwargs={"cluster_key": cluster.key, "endpoint_id": endpoint.pk},
                ),
                {"action": "enable"},
            )

        self.assertEqual(response.status_code, 302)
        verify.assert_called_once_with(cluster, endpoint)
        endpoint.refresh_from_db()
        self.assertTrue(endpoint.enabled)

    def test_reenabling_cluster_reverifies_before_changing_state(self):
        cluster = ProxmoxCluster.objects.create(
            key="clusterb",
            display_name="Cluster B",
            enabled=False,
        )

        with patch(
            "core.views.clusters.verify_cluster_connection",
            return_value=self.verified,
        ) as verify:
            response = self.client.post(
                reverse("core:cluster_connection_action", kwargs={"cluster_key": cluster.key}),
                {"action": "enable"},
            )

        self.assertEqual(response.status_code, 302)
        verify.assert_called_once_with(cluster)
        cluster.refresh_from_db()
        self.assertTrue(cluster.enabled)
        self.assertTrue(AuditEvent.objects.filter(action="cluster.enabled", cluster=cluster).exists())

    def test_credential_rotation_verifies_replacement_and_never_audits_secret(self):
        cluster = ProxmoxCluster.objects.create(
            key="clusterb",
            display_name="Cluster B",
            enabled=True,
        )
        set_cluster_credential(
            cluster,
            token_id="old@pve!token",
            token_secret="old-secret",
        )
        replacement_secret = "replacement-secret-never-audit"

        with patch(
            "core.views.clusters.verify_replacement_credential",
            return_value=self.verified,
        ) as verify:
            response = self.client.post(
                reverse("core:cluster_connection_action", kwargs={"cluster_key": cluster.key}),
                {
                    "action": "rotate-credential",
                    "token_id": self.candidate.token_id,
                    "token_secret": replacement_secret,
                },
            )

        self.assertEqual(response.status_code, 302)
        verify.assert_called_once_with(
            cluster,
            token_id=self.candidate.token_id,
            token_secret=replacement_secret,
        )
        credential = ClusterCredential.objects.get(cluster=cluster)
        self.assertEqual(credential.token_id, self.candidate.token_id)
        self.assertNotIn(replacement_secret, credential.token_secret_sealed)
        event = AuditEvent.objects.get(action="cluster.credential_rotated")
        self.assertNotIn(replacement_secret, str(event.details))

    def test_enabled_cluster_must_keep_one_enabled_endpoint(self):
        cluster = ProxmoxCluster.objects.create(
            key="clusterb",
            display_name="Cluster B",
            enabled=True,
        )
        endpoint = ProxmoxEndpoint.objects.create(
            cluster=cluster,
            name="pve201",
            url="https://pve201.example.test:8006",
            enabled=True,
        )

        response = self.client.post(
            reverse(
                "core:cluster_endpoint_action",
                kwargs={"cluster_key": cluster.key, "endpoint_id": endpoint.pk},
            ),
            {"action": "disable"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "must retain at least one enabled endpoint")
        endpoint.refresh_from_db()
        self.assertTrue(endpoint.enabled)

    def test_credential_removal_requires_cluster_to_be_disabled(self):
        cluster = ProxmoxCluster.objects.create(
            key="clusterb",
            display_name="Cluster B",
            enabled=True,
        )
        set_cluster_credential(
            cluster,
            token_id=self.candidate.token_id,
            token_secret=self.candidate.token_secret,
        )

        response = self.client.post(
            reverse("core:cluster_connection_action", kwargs={"cluster_key": cluster.key}),
            {"action": "remove-credential"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Disable the cluster before removing")
        self.assertTrue(ClusterCredential.objects.filter(cluster=cluster).exists())

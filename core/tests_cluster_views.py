from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import (
    AuditEvent,
    ClusterCredential,
    ClusterMembershipState,
    ClusterProjectionCoverage,
    ClusterStorage,
    ClusterStorageMount,
    ClusterTopologyHandoffStorageBinding,
    ProxmoxCluster,
    ProxmoxEndpoint,
    RuntimeConfigurationState,
    StorageMount,
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
    TRUST_PUBLIC,
    InspectedCertificate,
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
        with patch("core.views.clusters.connections.inspect_transport", return_value=self.certificate):
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
            "core.views.clusters.connections.verify_new_cluster",
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
        self.assertContains(response, "No Proxmox host or cluster is configured")
        self.assertContains(response, reverse("core:cluster_add"))

        response = self.client.get(reverse("core:legacy_tags_overview"))
        self.assertRedirects(response, reverse("core:cluster_add"), fetch_redirect_response=False)

    def test_cluster_connection_pages_render_compact_layout_hooks(self):
        cluster = ProxmoxCluster.objects.create(
            key="clusterb",
            display_name="Cluster B",
            enabled=False,
        )
        ProxmoxCluster.objects.create(key="clusterhq", display_name="Cluster HQ", enabled=True)
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

        overview = self.client.get(reverse("core:clusters_overview"))
        self.assertContains(overview, "cluster-list-heading")
        self.assertContains(overview, "Configured hosts &amp; clusters")
        self.assertContains(overview, "2 managed")

        detail = self.client.get(reverse("core:cluster_connection", kwargs={"cluster_key": cluster.key}))
        self.assertContains(detail, "cluster-display-name-form")
        self.assertContains(detail, "cluster-remove-credential-form")
        self.assertContains(detail, "cluster-section-heading")
        body = detail.content.decode()
        self.assertLess(body.index("Verify and rotate credential"), body.index("Remove stored credential"))

    def test_pending_topology_renders_only_the_state_machine_direction_and_handoff_link(self):
        cluster = ProxmoxCluster.objects.create(key="pending", display_name="Pending", enabled=True)
        ClusterMembershipState.objects.create(
            cluster=cluster,
            topology_role="standalone",
            transition_pending=True,
            pending_topology_role="corosync",
        )

        with patch("core.services.cluster_onboarding.ProxmoxClient") as provider:
            response = self.client.get(reverse("core:cluster_connection", kwargs={"cluster_key": cluster.key}))

        self.assertContains(response, "standalone → corosync")
        self.assertContains(response, f"?handoff_from={cluster.key}")
        self.assertNotContains(response, 'name="pending_topology_role"')
        provider.assert_not_called()

    def test_replacement_connection_renders_actionable_storage_intent_status(self):
        cluster = ProxmoxCluster.objects.create(key="replacement", display_name="Replacement", enabled=True)
        mount = StorageMount.objects.create(storage_id="nas", display_name="NAS mount", path="/mnt/nas")
        ClusterTopologyHandoffStorageBinding.objects.create(
            cluster=cluster,
            source_cluster_key_snapshot="old",
            storage_id="local",
            mount=mount,
            scope=ClusterStorageMount.Scope.NODE,
            node="pve1",
            status=ClusterTopologyHandoffStorageBinding.Status.REFUSED,
            refusal_reason="Storage 'local' has no complete present metadata for node 'pve1'.",
        )

        response = self.client.get(reverse("core:cluster_connection", kwargs={"cluster_key": cluster.key}))

        self.assertContains(response, "Hand-off storage mappings")
        self.assertContains(response, "local")
        self.assertContains(response, "pve1")
        self.assertContains(response, "NAS mount")
        self.assertContains(response, "no complete present metadata")
        self.assertContains(response, "bind this storage manually")

    def test_unreadable_pending_role_renders_exact_key_repair_not_handoff(self):
        cluster = ProxmoxCluster.objects.create(key="future", display_name="Future", enabled=True)
        ClusterMembershipState.objects.create(
            cluster=cluster,
            topology_role="standalone",
            transition_pending=True,
            pending_topology_role="corosync-v2",
        )

        response = self.client.get(reverse("core:cluster_connection", kwargs={"cluster_key": cluster.key}))

        self.assertContains(response, "Discard unreadable pending evidence")
        self.assertContains(response, 'name="typed_cluster_key"')
        self.assertNotContains(response, "Review standalone")

    def test_observer_not_member_renders_two_step_recovery_without_provider_call(self):
        cluster = ProxmoxCluster.objects.create(key="observer", display_name="Observer", enabled=True)
        ClusterProjectionCoverage.objects.create(
            cluster=cluster,
            domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP,
            error_code="observer_not_a_member",
        )
        ProxmoxEndpoint.objects.create(
            cluster=cluster,
            name="pve-stale",
            url="https://pve-stale.example.test:8006",
        )

        with patch("core.views.clusters.connections.inspect_membership_recovery") as inspect:
            response = self.client.get(reverse("core:cluster_connection", kwargs={"cluster_key": cluster.key}))

        self.assertContains(response, "Verify and inspect candidate members")
        self.assertNotContains(response, "Replace accepted membership")
        inspect.assert_not_called()

    def _inspect_key(self, cluster_key):
        with patch("core.views.clusters.connections.inspect_transport", return_value=self.certificate):
            return self.client.post(
                reverse("core:cluster_add"),
                {
                    "action": "inspect",
                    "display_name": "Replacement",
                    "cluster_key": cluster_key,
                    "endpoint_url": "https://pve1.example.test:8006",
                    "endpoint_name": "pve1",
                },
            )

    def test_add_form_rejects_a_retired_key_as_permanently_reserved(self):
        ProxmoxCluster.objects.create(
            key="clusterb",
            display_name="Retired B",
            enabled=False,
            retired_at=timezone.now(),
            retirement_mode=ProxmoxCluster.RetirementMode.FORCED,
            retirement_reason="The site was decommissioned.",
        )

        response = self._inspect_key("clusterb")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "belonged to a retired cluster")
        self.assertEqual(response.context["step"], "identity")

    def test_add_form_points_a_managed_key_collision_at_delete_unused(self):
        ProxmoxCluster.objects.create(key="clusterb", display_name="Existing B", enabled=False)

        response = self._inspect_key("clusterb")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already uses this permanent key")
        self.assertContains(response, "Delete unused connection")

    def test_zero_cluster_state_keeps_aggregate_views_usable(self):
        for route_name in (
            "core:dashboard",
            "core:vms_overview",
            "core:vms",
            "core:audit_log",
        ):
            with self.subTest(route=route_name):
                response = self.client.get(reverse(route_name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, reverse("core:cluster_add"))

        # Scheduled Tasks is cluster-scoped and therefore not an aggregate view: with
        # no cluster there is nothing for it to show, so the legacy path sends the
        # operator to where a cluster is added instead of rendering an empty list.
        response = self.client.get(reverse("core:legacy_scheduled_tasks"))
        self.assertRedirects(response, reverse("core:cluster_add"), fetch_redirect_response=False)

        search = self.client.get(reverse("core:global_search"))
        self.assertEqual(search.status_code, 200)
        self.assertJSONEqual(search.content, {"query": "", "results": []})

    def test_wizard_persists_only_after_identity_confirmation_and_never_renders_secret(self):
        candidate_token = self._candidate_token()

        self.assertNotIn(self.candidate.token_secret, candidate_token)
        self.assertFalse(ProxmoxCluster.objects.exists())

        with patch(
            "core.views.clusters.connections.verify_new_cluster",
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

    def test_handoff_post_workflow_signs_storage_selection_before_final_mutation(self):
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
            topology_role="standalone",
            transition_pending=True,
            pending_topology_role="corosync",
        )
        mount = StorageMount.objects.create(storage_id="nas", display_name="NAS mount", path="/mnt/nas")
        definition = ClusterStorage.objects.create(
            cluster=source,
            storage_id="nas",
            storage_type="nfs",
            shared=True,
        )
        binding = ClusterStorageMount.objects.create(
            cluster_storage=definition,
            mount=mount,
            scope=ClusterStorageMount.Scope.SHARED,
        )
        start = self.client.get(reverse("core:cluster_add"), {"handoff_from": source.key})
        handoff = start.context["handoff"]
        with patch("core.views.clusters.connections.inspect_transport", return_value=self.certificate):
            inspected = self.client.post(
                reverse("core:cluster_add"),
                {
                    "action": "inspect",
                    "handoff": handoff,
                    "display_name": self.candidate.display_name,
                    "cluster_key": self.candidate.key,
                    "endpoint_url": self.candidate.endpoint_url,
                    "endpoint_name": self.candidate.endpoint_name,
                },
            )
        inspection = inspected.context["trust_form"]["inspection"].value()
        with patch(
            "core.views.clusters.connections.verify_new_cluster",
            return_value=(self.candidate, self.verified),
        ):
            verified = self.client.post(
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
            candidate_token = verified.context["confirm_form"]["candidate"].value()
            with patch("core.views.clusters.connections.complete_topology_handoff") as complete:
                reviewed = self.client.post(
                    reverse("core:cluster_add"),
                    {
                        "action": "confirm",
                        "candidate": candidate_token,
                        "confirm_identity": "on",
                        "storage_binding": str(binding.pk),
                    },
                )
            complete.assert_not_called()
            self.assertEqual(reviewed.context["step"], "handoff-confirm")
            signed_confirmation = reviewed.context["final_form"]["handoff_confirmation"].value()
            tampered_confirmation = ("x" if signed_confirmation[0] != "x" else "y") + signed_confirmation[1:]
            refused = self.client.post(
                reverse("core:cluster_add"),
                {
                    "action": "complete-handoff",
                    "handoff_confirmation": tampered_confirmation,
                    "confirm_handoff": "on",
                },
            )
            self.assertContains(refused, "verification is invalid")
            self.assertFalse(source.is_retired)

            with (
                patch(
                    "core.views.clusters.connections.cluster_handoff_retirement_preflight",
                    return_value=SimpleNamespace(gate_clear=True, confirmation="retirement", blocker_codes=()),
                ),
                patch("core.views.clusters.connections.complete_topology_handoff", return_value=source) as complete,
                patch("core.views.clusters.connections._queue_first_inventory"),
            ):
                response = self.client.post(
                    reverse("core:cluster_add"),
                    {
                        "action": "complete-handoff",
                        "handoff_confirmation": signed_confirmation,
                        "confirm_handoff": "on",
                        "storage_binding": str(binding.pk + 999),
                    },
                )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(complete.call_args.kwargs["selected_storage_binding_ids"], (binding.pk,))

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
        with patch("core.views.clusters.connections.inspect_transport", return_value=self.certificate):
            inspected = self.client.post(
                add_url,
                {
                    "action": "inspect",
                    "endpoint_url": "https://pve202.example.test:8006",
                    "endpoint_name": "pve202",
                },
            )
        inspection = inspected.context["trust_form"]["inspection"].value()

        with patch("core.views.clusters.connections.verify_endpoint_for_cluster", return_value=self.verified):
            verified = self.client.post(
                add_url,
                {"action": "verify", "inspection": inspection, "confirm_certificate": "on"},
            )
        endpoint_token = verified.context["confirm_form"]["endpoint"].value()
        self.assertFalse(ProxmoxEndpoint.objects.filter(name="pve202").exists())

        with patch("core.views.clusters.connections.verify_endpoint_for_cluster", return_value=self.verified):
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
            "core.views.clusters.connections.verify_registered_endpoint",
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
            "core.views.clusters.connections.verify_cluster_connection",
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
            "core.views.clusters.connections.verify_replacement_credential",
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

    def _retirement_ready_cluster(self, *, enabled: bool) -> ProxmoxCluster:
        cluster = ProxmoxCluster.objects.create(
            key="clusterb",
            display_name="Cluster B",
            enabled=enabled,
            discovered_ca_uuid=self.identity.ca_uuid,
            discovered_ca_fingerprint=self.identity.ca_fingerprint,
        )
        ProxmoxEndpoint.objects.create(
            cluster=cluster,
            name="pve201",
            url=self.candidate.endpoint_url,
            enabled=True,
        )
        approve_cluster_transport(cluster, mode=TRUST_PUBLIC)
        set_cluster_credential(
            cluster,
            token_id=self.candidate.token_id,
            token_secret=self.candidate.token_secret,
        )
        return cluster

    def test_enabled_cluster_shows_verified_retirement_with_its_precondition(self):
        cluster = self._retirement_ready_cluster(enabled=True)

        response = self.client.get(reverse("core:cluster_connection", kwargs={"cluster_key": cluster.key}))

        # The recommended path must be visible with its precondition named, not
        # hidden behind the forced path it is meant to keep operators away from.
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Verified retirement")
        self.assertEqual(response.context["verified_retirement_blocker"], "Disable the cluster first")
        self.assertContains(response, "Disable the cluster first to use the recommended path.")

    def test_disabled_cluster_offers_verified_retirement_without_a_blocker(self):
        cluster = self._retirement_ready_cluster(enabled=False)

        response = self.client.get(reverse("core:cluster_connection", kwargs={"cluster_key": cluster.key}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Verified retirement")
        self.assertEqual(response.context["verified_retirement_blocker"], "")

    def test_verified_retirement_blocker_names_the_missing_precondition(self):
        cluster = self._retirement_ready_cluster(enabled=False)
        cluster.endpoints.update(enabled=False)

        response = self.client.get(reverse("core:cluster_connection", kwargs={"cluster_key": cluster.key}))
        self.assertEqual(response.context["verified_retirement_blocker"], "An enabled endpoint is required")

        cluster.discovered_ca_uuid = ""
        cluster.save(update_fields=["discovered_ca_uuid"])
        response = self.client.get(reverse("core:cluster_connection", kwargs={"cluster_key": cluster.key}))
        self.assertEqual(response.context["verified_retirement_blocker"], "A pinned Proxmox CA identity is required")

        ClusterCredential.objects.filter(cluster=cluster).delete()
        response = self.client.get(reverse("core:cluster_connection", kwargs={"cluster_key": cluster.key}))
        self.assertEqual(
            response.context["verified_retirement_blocker"],
            "A stored credential and transport trust are required",
        )

    def test_nodes_are_rendered_above_the_endpoint_panel(self):
        """The enrollment table is the page's inventory; endpoints are transports.

        Order is the whole point of the panel, so it is asserted rather than left to
        whoever next moves a section. Two tables of the same names, endpoints first,
        read as one list with a redundant copy.
        """
        cluster = self._retirement_ready_cluster(enabled=True)

        body = self.client.get(reverse("core:cluster_connection", kwargs={"cluster_key": cluster.key})).content.decode()

        self.assertLess(body.index("<h2>Nodes</h2>"), body.index("<h2>Endpoints"))

    def test_endpoint_panel_opens_only_when_a_transport_needs_attention(self):
        cluster = self._retirement_ready_cluster(enabled=True)
        detail_url = reverse("core:cluster_connection", kwargs={"cluster_key": cluster.key})

        # The rendered attribute, not only the flag: the panel folding away is the
        # change, and a context value the template ignores would not deliver it.
        response = self.client.get(detail_url)
        self.assertFalse(response.context["endpoints_need_attention"])
        self.assertNotContains(response, '<details class="cluster-endpoints" open>')

        cluster.endpoints.update(enabled=False)
        response = self.client.get(detail_url)
        self.assertTrue(response.context["endpoints_need_attention"])
        self.assertContains(response, '<details class="cluster-endpoints" open>')

        cluster.endpoints.all().delete()
        response = self.client.get(detail_url)
        self.assertTrue(response.context["endpoints_need_attention"])
        self.assertContains(response, '<details class="cluster-endpoints" open>')

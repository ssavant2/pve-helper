"""Operator-facing R3 danger-zone retirement wiring."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, ProxmoxCluster, ProxmoxEndpoint
from core.services.cluster_retirement import (
    ERROR_CODE_PREFLIGHT_IDENTITY_MISMATCH,
    ERROR_CODE_RETIREMENT_CONFIRMATION,
    RetirementPreflightIdentityMismatch,
)
from core.tests_cluster_retire_preflight import CA_UUID, IdentityClient


@override_settings(APP_REQUIRE_LOGIN=False)
class ClusterRetirementViewTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(username="retirement-view-operator")
        self.client.force_login(self.actor)

    def _action_url(self, cluster):
        return reverse("core:cluster_connection_action", kwargs={"cluster_key": cluster.key})

    def _retired_cluster(self, *, key="retired", mode=ProxmoxCluster.RetirementMode.FORCED):
        return ProxmoxCluster.objects.create(
            key=key,
            display_name="Retired cluster",
            enabled=False,
            discovered_name="Former production cluster",
            retired_at=timezone.now(),
            retired_by=self.actor,
            retirement_mode=mode,
            retirement_reason="The site was permanently decommissioned." if mode == "forced" else "",
            retired_ca_uuid=CA_UUID,
            retired_ca_fingerprint="AA:BB:CC",
        )

    def test_overview_separates_managed_connections_from_retired_archive(self):
        managed = ProxmoxCluster.objects.create(
            key="managed",
            display_name="Managed cluster",
            enabled=False,
        )
        retired = self._retired_cluster()

        response = self.client.get(reverse("core:clusters_overview"))

        self.assertContains(response, "1 managed")
        self.assertContains(response, "Retired clusters")
        self.assertContains(response, "1 retired")
        self.assertContains(response, managed.display_name)
        self.assertContains(response, retired.display_name)
        self.assertContains(response, retired.key)
        self.assertContains(response, "Forced")
        self.assertEqual(list(response.context["clusters"]), [managed])
        self.assertEqual(list(response.context["retired_clusters"]), [retired])

    def test_only_retired_installation_shows_onboarding_and_archive(self):
        retired = self._retired_cluster()

        response = self.client.get(reverse("core:clusters_overview"))

        self.assertContains(response, "No Proxmox cluster is configured")
        self.assertContains(response, reverse("core:cluster_add"))
        self.assertContains(response, "Retired clusters")
        self.assertContains(response, retired.display_name)

    def test_retired_detail_is_read_only_and_uses_tombstone_identity(self):
        retired = self._retired_cluster()

        response = self.client.get(reverse("core:cluster_connection", kwargs={"cluster_key": retired.key}))

        self.assertContains(response, "Read-only retired connection")
        self.assertContains(response, retired.retired_ca_uuid)
        self.assertContains(response, retired.retired_ca_fingerprint)
        self.assertContains(response, retired.retirement_reason)
        self.assertContains(response, self.actor.username)
        self.assertContains(response, f"{reverse('core:audit_log')}?cluster={retired.key}")
        main_content = response.content.decode().split("<main", 1)[1].split("</main>", 1)[0]
        self.assertNotIn("<form", main_content)
        self.assertNotContains(response, "Add endpoint")
        self.assertNotContains(response, "Update name")
        self.assertNotContains(response, "rotate credential")
        self.assertNotContains(response, "Enable cluster")
        self.assertNotContains(response, "Danger zone")

    def test_retired_connection_mutation_routes_fail_closed(self):
        retired = self._retired_cluster()
        endpoint = ProxmoxEndpoint.objects.create(
            cluster=retired,
            name="old-endpoint",
            url="https://old-endpoint.retired.test:8006/",
        )

        action_response = self.client.post(self._action_url(retired), {"action": "display-name"})
        add_response = self.client.get(
            reverse("core:cluster_endpoint_add", kwargs={"cluster_key": retired.key}),
        )
        endpoint_response = self.client.post(
            reverse(
                "core:cluster_endpoint_action",
                kwargs={"cluster_key": retired.key, "endpoint_id": endpoint.pk},
            ),
            {"action": "disable"},
        )

        self.assertEqual(action_response.status_code, 404)
        self.assertEqual(add_response.status_code, 404)
        self.assertEqual(endpoint_response.status_code, 404)

    def test_danger_zone_gates_verified_retirement_but_always_offers_forced_retirement(self):
        enabled = ProxmoxCluster.objects.create(
            key="enabled",
            display_name="Enabled cluster",
            enabled=True,
        )
        enabled_page = self.client.get(reverse("core:cluster_connection", kwargs={"cluster_key": enabled.key}))
        self.assertContains(enabled_page, "Force retire")
        self.assertNotContains(enabled_page, "Verified retirement")

        disabled = ProxmoxCluster.objects.create(
            key="disabled",
            display_name="Disabled cluster",
            enabled=False,
            discovered_ca_uuid=CA_UUID,
        )
        endpoint = ProxmoxEndpoint.objects.create(
            cluster=disabled,
            name="pve1",
            url="https://pve1.disabled.test:8006/",
        )
        disabled_page = self.client.get(reverse("core:cluster_connection", kwargs={"cluster_key": disabled.key}))
        self.assertContains(disabled_page, "Verified retirement")
        self.assertContains(disabled_page, f'value="{endpoint.pk}"')
        self.assertContains(disabled_page, "Retire cluster")
        self.assertContains(disabled_page, "Force retire")

    def test_disable_refusal_offers_forced_retirement_in_the_refusal(self):
        cluster = ProxmoxCluster.objects.create(
            key="disable-refusal",
            display_name="Disable refusal",
            enabled=True,
        )
        AuditEvent.objects.create(
            cluster=cluster,
            cluster_key_snapshot=cluster.key,
            action="guest.power.start",
            outcome="running",
        )

        response = self.client.post(
            self._action_url(cluster),
            {
                "action": "disable",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "provider work is active")
        self.assertContains(response, "Force-retire a permanently unavailable site")

    def test_verified_preflight_returns_signed_impact_for_the_selected_endpoint(self):
        cluster = ProxmoxCluster.objects.create(
            key="verified",
            display_name="Verified cluster",
            enabled=False,
            discovered_ca_uuid=CA_UUID,
        )
        endpoint = ProxmoxEndpoint.objects.create(
            cluster=cluster,
            name="pve1",
            url="https://pve1.verified.test:8006/",
        )

        with patch(
            "core.services.cluster_retirement.client_for_endpoint",
            return_value=IdentityClient(),
        ):
            response = self.client.post(
                self._action_url(cluster),
                {
                    "action": "retirement-preflight",
                    "mode": "verified",
                    "endpoint_id": endpoint.pk,
                },
                HTTP_ACCEPT="application/json",
                HTTP_X_REQUESTED_WITH="fetch",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["ready"])
        self.assertTrue(payload["confirmation"])
        self.assertEqual(payload["impact"]["identity_verification"], "matched")
        self.assertEqual(payload["impact"]["endpoint"], endpoint.name)
        self.assertEqual(payload["impact"]["counts"]["current_projections"], 0)
        self.assertEqual(payload["impact"]["counts"]["history"], 0)

    def test_preflight_failure_returns_only_stable_public_error_and_records_audit(self):
        cluster = ProxmoxCluster.objects.create(
            key="mismatch",
            display_name="Mismatch cluster",
            enabled=False,
            discovered_ca_uuid=CA_UUID,
        )
        endpoint = ProxmoxEndpoint.objects.create(
            cluster=cluster,
            name="pve1",
            url="https://pve1.mismatch.test:8006/",
        )
        observed_uuid = "99999999-9999-9999-9999-999999999999"

        with patch(
            "core.views.clusters.cluster_retirement_preflight",
            side_effect=RetirementPreflightIdentityMismatch(
                observed_uuid=observed_uuid,
                pinned_uuid=CA_UUID,
            ),
        ):
            response = self.client.post(
                self._action_url(cluster),
                {
                    "action": "retirement-preflight",
                    "mode": "verified",
                    "endpoint_id": endpoint.pk,
                },
                HTTP_ACCEPT="application/json",
                HTTP_X_REQUESTED_WITH="fetch",
            )

        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], ERROR_CODE_PREFLIGHT_IDENTITY_MISMATCH)
        self.assertNotIn(observed_uuid, str(payload))
        event = AuditEvent.objects.get(action="cluster.retirement_verification_failed")
        self.assertEqual(event.outcome, "refused")
        self.assertEqual(event.details["reason_code"], ERROR_CODE_PREFLIGHT_IDENTITY_MISMATCH)
        self.assertNotIn(observed_uuid, str(event.details))

    def test_forced_preflight_and_final_post_retire_an_enabled_cluster(self):
        cluster = ProxmoxCluster.objects.create(
            key="forced",
            display_name="Forced cluster",
            enabled=True,
        )
        preflight = self.client.post(
            self._action_url(cluster),
            {
                "action": "retirement-preflight",
                "mode": "forced",
            },
            HTTP_ACCEPT="application/json",
            HTTP_X_REQUESTED_WITH="fetch",
        )

        self.assertEqual(preflight.status_code, 200)
        preflight_payload = preflight.json()
        self.assertTrue(preflight_payload["ready"])
        self.assertEqual(preflight_payload["impact"]["identity_verification"], "skipped")

        response = self.client.post(
            self._action_url(cluster),
            {
                "action": "retire",
                "confirmation": preflight_payload["confirmation"],
                "typed_cluster_key": cluster.key,
                "reason": "The decommissioned site is permanently unavailable.",
                "permanent_unavailability_asserted": "yes",
            },
            HTTP_ACCEPT="application/json",
            HTTP_X_REQUESTED_WITH="fetch",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "ok": True,
                "mode": ProxmoxCluster.RetirementMode.FORCED,
                "redirect_url": reverse("core:clusters_overview"),
            },
        )
        cluster.refresh_from_db()
        self.assertFalse(cluster.enabled)
        self.assertIsNotNone(cluster.retired_at)
        self.assertEqual(cluster.retired_by, self.actor)

    def test_final_post_rechecks_exact_forced_key_and_returns_stable_refusal(self):
        cluster = ProxmoxCluster.objects.create(
            key="forced-refusal",
            display_name="Forced refusal",
            enabled=True,
        )
        preflight = self.client.post(
            self._action_url(cluster),
            {
                "action": "retirement-preflight",
                "mode": "forced",
            },
            HTTP_ACCEPT="application/json",
            HTTP_X_REQUESTED_WITH="fetch",
        ).json()

        response = self.client.post(
            self._action_url(cluster),
            {
                "action": "retire",
                "confirmation": preflight["confirmation"],
                "typed_cluster_key": "wrong-key",
                "reason": "The site was decommissioned.",
                "permanent_unavailability_asserted": "yes",
            },
            HTTP_ACCEPT="application/json",
            HTTP_X_REQUESTED_WITH="fetch",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], ERROR_CODE_RETIREMENT_CONFIRMATION)
        cluster.refresh_from_db()
        self.assertTrue(cluster.enabled)
        self.assertIsNone(cluster.retired_at)
        self.assertTrue(
            AuditEvent.objects.filter(
                action="cluster.retirement_refused",
                details__reason_code=ERROR_CODE_RETIREMENT_CONFIRMATION,
            ).exists()
        )

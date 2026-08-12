"""Readable Audit presentation for the R3 cluster-retirement lifecycle."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, ProxmoxCluster


@override_settings(APP_REQUIRE_LOGIN=False)
class ClusterRetirementAuditPresentationTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(username="retirement-audit-operator")
        self.client.force_login(self.actor)
        self.managed = ProxmoxCluster.objects.create(
            key="managed-audit",
            display_name="Managed audit cluster",
            enabled=False,
        )
        self.retired = ProxmoxCluster.objects.create(
            key="retired-audit",
            display_name="Retired audit cluster",
            enabled=False,
            retired_at=timezone.now(),
            retired_by=self.actor,
            retirement_mode=ProxmoxCluster.RetirementMode.FORCED,
            retirement_reason="Site permanently offline.",
        )

    def _event(self, action, *, cluster=None, details=None, outcome="success"):
        cluster = cluster or self.retired
        return AuditEvent.objects.create(
            username=self.actor.username,
            action=action,
            object_type="cluster",
            object_id=cluster.key,
            outcome=outcome,
            cluster=cluster,
            cluster_key_snapshot=cluster.key,
            details=details or {},
        )

    def test_retirement_actions_and_structured_details_are_operator_readable(self):
        events = [
            self._event(
                "cluster.retirement_verification_failed",
                details={
                    "retirement_mode": "verified",
                    "reason_code": "cluster_retirement_preflight_identity_mismatch",
                },
                outcome="refused",
            ),
            self._event(
                "cluster.retirement_refused",
                details={
                    "retirement_mode": "forced",
                    "reason_code": "participant_changed",
                },
                outcome="refused",
            ),
            self._event(
                "cluster.retired",
                details={
                    "retirement_mode": "verified",
                    "identity_verification": "matched",
                    "endpoint_count": 1,
                    "cleanup": {"schedules_deleted": 1},
                },
            ),
            self._event(
                "cluster.force_retired",
                details={
                    "retirement_mode": "forced",
                    "retirement_reason": "Site permanently offline.",
                    "identity_verification": "skipped",
                    "endpoint_count": 2,
                    "cleanup": {
                        "schedules_deleted": 2,
                        "current_guests_deleted": 1,
                    },
                },
            ),
            self._event("cluster.unused_connection_deleted"),
            self._event(
                "cluster.retired",
                details={
                    "retirement_mode": "verified",
                    "identity_verification": "superseded_by_verified_handoff",
                    "endpoint_count": 1,
                    "cleanup": {},
                },
            ),
        ]

        response = self.client.get(reverse("core:audit_log"), {"cluster": self.retired.key})

        self.assertEqual(response.status_code, 200)
        presented = {event.pk: (event.display_action, event.display_detail) for event in response.context["events"]}
        self.assertEqual(
            presented[events[0].pk],
            ("Verify cluster retirement", "Verified retirement · The endpoint reports a different cluster identity"),
        )
        self.assertEqual(
            presented[events[1].pk],
            ("Retirement refused", "Forced retirement · Protected cluster activity changed after preflight"),
        )
        self.assertEqual(
            presented[events[2].pk],
            ("Retire cluster", "Identity matched · 1 endpoint removed · 1 cleanup change"),
        )
        self.assertEqual(
            presented[events[3].pk],
            (
                "Force-retire cluster",
                "Identity verification skipped · 2 endpoints removed · 3 cleanup changes · "
                "Reason: Site permanently offline.",
            ),
        )
        self.assertEqual(presented[events[4].pk], ("Delete unused cluster connection", ""))
        self.assertEqual(
            presented[events[5].pk],
            (
                "Retire cluster",
                "Old identity superseded by verified topology hand-off · 1 endpoint removed · 0 cleanup changes",
            ),
        )
        self.assertNotContains(response, "cluster.retirement_preflight_identity_mismatch")
        self.assertNotContains(response, "cluster.force_retired")

    def test_retired_cluster_is_selectable_and_filters_by_durable_key_snapshot(self):
        retired_event = self._event(
            "cluster.force_retired",
            details={
                "retirement_mode": "forced",
                "identity_verification": "skipped",
                "endpoint_count": 0,
                "cleanup": {},
            },
        )
        self._event(
            "cluster.disabled",
            cluster=self.managed,
            details={"display_name": self.managed.display_name},
        )

        response = self.client.get(reverse("core:audit_log"), {"cluster": self.retired.key})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["audit_cluster"], self.retired.key)
        self.assertEqual([event.pk for event in response.context["events"]], [retired_event.pk])
        self.assertContains(response, "Retired audit cluster (retired)")
        self.assertContains(response, f'<option value="{self.retired.key}" selected>')
        self.assertContains(response, f'<option value="{self.managed.key}">')

    def test_unknown_cluster_filter_is_rejected_without_hiding_events(self):
        self._event("cluster.disabled", cluster=self.managed)

        response = self.client.get(reverse("core:audit_log"), {"cluster": "missing-cluster"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["audit_cluster"], "")
        self.assertEqual(response.context["audit_total"], 2)

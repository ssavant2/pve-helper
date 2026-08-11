"""Executable acceptance contract for Module 5 phase 5a1J."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from core.models import AuditEvent, ClusterNodeEnrollment, ProxmoxCluster, ProxmoxEndpoint
from core.services.audit_events import (
    CLUSTER_CONFIGURATION_AUDIT_ACTIONS,
    CLUSTER_PROVIDER_AUDIT_ACTIONS,
    record_audit_event,
)
from core.services.cluster_deletion import ClusterDeletionPostconditionFailed, delete_unused_cluster_connection
from core.services.cluster_deletion_eligibility import unused_connection_deletion_eligibility
from core.services.cluster_enrollment import EnrollmentRetirementResult, retire_cluster_enrollments
from core.services.cluster_lifecycle_registry import (
    CLUSTER_REVERSE_RELATIONS,
    FootprintPolicy,
    RelationClass,
)

ENROLLMENT_AUDIT_ACTIONS = frozenset(
    {
        "cluster.node.enrolled",
        "cluster.node.enrollment_failed",
        "cluster.node.mode_changed",
        "cluster.node.removed",
    }
)


def _cluster(key: str = "enrollment") -> ProxmoxCluster:
    return ProxmoxCluster.objects.create(key=key, display_name=key, enabled=False)


def _enrollment(
    cluster: ProxmoxCluster,
    node_name: str = "pve1",
    *,
    actor=None,
    endpoint=None,
) -> ClusterNodeEnrollment:
    return ClusterNodeEnrollment.objects.create(
        cluster=cluster,
        node_name=node_name,
        mode=ClusterNodeEnrollment.Mode.MANAGED,
        enrolled_at=timezone.now(),
        enrolled_by=actor,
        onboarded_via_endpoint=endpoint,
    )


class ClusterEnrollmentSchemaTests(TestCase):
    def test_cluster_activation_defaults_preserve_legacy_publication(self):
        cluster = _cluster()

        self.assertEqual(cluster.enrollment_contract_version, 0)
        self.assertEqual(cluster.enrollment_generation, 0)
        self.assertIsNone(cluster.enrollment_activated_at)
        self.assertIsNone(cluster.enrollment_activated_by)
        self.assertFalse(cluster.node_enrollments.exists())

    def test_enrollment_has_exact_node_ref_identity_and_immutable_snapshot(self):
        cluster = _cluster()
        enrollment = _enrollment(cluster)

        self.assertEqual(enrollment.node_ref_snapshot, "nr1:enrollment:pve1")
        enrollment.mode = ClusterNodeEnrollment.Mode.SAFETY_ONLY
        enrollment.save(update_fields=["mode", "updated_at"])
        enrollment.node_name = "pve2"
        with self.assertRaises(ValidationError):
            enrollment.save()
        enrollment.refresh_from_db()
        self.assertEqual(enrollment.node_name, "pve1")
        self.assertEqual(enrollment.node_ref_snapshot, "nr1:enrollment:pve1")

    def test_database_rejects_bulk_identity_rewrites_and_mismatched_snapshot_inserts(self):
        cluster = _cluster()
        enrollment = _enrollment(cluster)

        invalid_updates = (
            {"node_name": "pve2"},
            {"node_ref_snapshot": "nr1:enrollment:pve2"},
            {"node_name": "pve2", "node_ref_snapshot": "nr1:enrollment:pve2"},
        )
        for update in invalid_updates:
            with self.subTest(update=update), self.assertRaises(IntegrityError), transaction.atomic():
                ClusterNodeEnrollment.objects.filter(pk=enrollment.pk).update(**update)

        with self.assertRaises(IntegrityError), transaction.atomic():
            ClusterNodeEnrollment.objects.bulk_create(
                [
                    ClusterNodeEnrollment(
                        cluster=cluster,
                        node_name="pve2",
                        node_ref_snapshot="nr1:another-cluster:pve2",
                        mode="managed",
                        enrolled_at=timezone.now(),
                    )
                ]
            )

        enrollment.node_name = "pve2"
        enrollment.node_ref_snapshot = "nr1:enrollment:pve2"
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClusterNodeEnrollment.objects.bulk_update(
                [enrollment],
                ["node_name", "node_ref_snapshot"],
            )

    def test_duplicate_node_ref_is_rejected_but_same_name_in_another_cluster_is_valid(self):
        first = _cluster("first")
        second = _cluster("second")
        _enrollment(first)
        _enrollment(second)

        with self.assertRaises(IntegrityError), transaction.atomic():
            _enrollment(first)

    def test_database_rejects_invalid_node_names_mode_and_empty_snapshot(self):
        cluster = _cluster()
        now = timezone.now()
        invalid_rows = (
            ClusterNodeEnrollment(
                cluster=cluster,
                node_name="",
                node_ref_snapshot="nr1:enrollment:invalid",
                mode="managed",
                enrolled_at=now,
            ),
            ClusterNodeEnrollment(
                cluster=cluster,
                node_name="bad:name",
                node_ref_snapshot="nr1:enrollment:bad:name",
                mode="managed",
                enrolled_at=now,
            ),
            ClusterNodeEnrollment(
                cluster=cluster,
                node_name="pve2",
                node_ref_snapshot="nr1:enrollment:pve2",
                mode="future_mode",
                enrolled_at=now,
            ),
            ClusterNodeEnrollment(
                cluster=cluster,
                node_name="pve3",
                node_ref_snapshot="",
                mode="managed",
                enrolled_at=now,
            ),
        )
        for index, row in enumerate(invalid_rows):
            with self.subTest(index=index), self.assertRaises(IntegrityError), transaction.atomic():
                ClusterNodeEnrollment.objects.bulk_create([row])

    def test_actor_and_endpoint_deletion_preserve_enrollment_configuration(self):
        actor = get_user_model().objects.create_user(username="enroller")
        cluster = _cluster()
        endpoint = ProxmoxEndpoint.objects.create(
            cluster=cluster,
            name="pve1",
            url="https://pve1.enrollment.test:8006/",
        )
        enrollment = _enrollment(cluster, actor=actor, endpoint=endpoint)
        enrollment.mode_changed_by = actor
        enrollment.mode_changed_at = timezone.now()
        enrollment.mode_change_reason = "Safety coverage only"
        enrollment.save(update_fields=["mode_changed_by", "mode_changed_at", "mode_change_reason", "updated_at"])
        cluster.enrollment_activated_by = actor
        cluster.save(update_fields=["enrollment_activated_by", "updated_at"])

        actor.delete()
        endpoint.delete()
        enrollment.refresh_from_db()
        cluster.refresh_from_db()

        self.assertIsNone(enrollment.enrolled_by)
        self.assertIsNone(enrollment.mode_changed_by)
        self.assertIsNone(enrollment.onboarded_via_endpoint)
        self.assertIsNone(cluster.enrollment_activated_by)
        self.assertEqual(enrollment.node_ref_snapshot, "nr1:enrollment:pve1")


class ClusterEnrollmentLifecycleTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(username="operator")

    def test_relation_is_disposable_configuration_with_no_footprint(self):
        relation = CLUSTER_REVERSE_RELATIONS["node_enrollments"]
        self.assertIs(relation.kind, RelationClass.CONFIG)
        self.assertFalse(relation.blocks_hard_delete)
        self.assertIs(relation.footprint_policy, FootprintPolicy.NONE)
        self.assertIsNone(relation.footprint_reason)

    def test_owner_finalizer_snapshots_and_removes_enrollments(self):
        cluster = _cluster()
        _enrollment(cluster, "pve2")
        _enrollment(cluster, "pve1")

        result = retire_cluster_enrollments(cluster)

        self.assertEqual(result.enrollment_rows_deleted, 2)
        self.assertEqual(
            result.enrollments,
            (
                {"node_ref_snapshot": "nr1:enrollment:pve1", "mode": "managed"},
                {"node_ref_snapshot": "nr1:enrollment:pve2", "mode": "managed"},
            ),
        )
        self.assertEqual(result.enrollments_omitted, 0)
        self.assertFalse(cluster.node_enrollments.exists())

    def test_owner_finalizer_bounds_snapshots_and_reports_omitted_count(self):
        cluster = _cluster()
        now = timezone.now()
        ClusterNodeEnrollment.objects.bulk_create(
            [
                ClusterNodeEnrollment(
                    cluster=cluster,
                    node_name=f"pve{index:03d}",
                    node_ref_snapshot=f"nr1:enrollment:pve{index:03d}",
                    mode="managed",
                    enrolled_at=now,
                )
                for index in range(101)
            ]
        )

        result = retire_cluster_enrollments(cluster)

        self.assertEqual(result.enrollment_rows_deleted, 101)
        self.assertEqual(len(result.enrollments), 100)
        self.assertEqual(result.enrollments[0]["node_ref_snapshot"], "nr1:enrollment:pve000")
        self.assertEqual(result.enrollments[-1]["node_ref_snapshot"], "nr1:enrollment:pve099")
        self.assertEqual(result.enrollments_omitted, 1)

    def test_all_four_audit_actions_are_configuration_only_and_stamp_no_footprint(self):
        self.assertTrue(ENROLLMENT_AUDIT_ACTIONS <= CLUSTER_CONFIGURATION_AUDIT_ACTIONS)
        self.assertFalse(ENROLLMENT_AUDIT_ACTIONS & CLUSTER_PROVIDER_AUDIT_ACTIONS)
        cluster = _cluster()

        for action in sorted(ENROLLMENT_AUDIT_ACTIONS):
            record_audit_event(
                action=action,
                cluster=cluster,
                username="operator",
                object_type="node",
                object_id="nr1:enrollment:pve1",
            )

        cluster.refresh_from_db()
        self.assertIsNone(cluster.operational_footprint_at)
        self.assertEqual(cluster.operational_footprint_reason, "")
        self.assertTrue(unused_connection_deletion_eligibility(cluster).eligible)

    def test_hard_delete_snapshots_enrollment_and_preserves_configuration_audit(self):
        cluster = _cluster()
        pk = cluster.pk
        _enrollment(cluster)
        for action in sorted(ENROLLMENT_AUDIT_ACTIONS):
            record_audit_event(action=action, cluster=cluster, username="operator")

        result = delete_unused_cluster_connection(cluster, actor=self.actor)

        self.assertEqual(result.enrollment_rows_deleted, 1)
        self.assertFalse(ProxmoxCluster.objects.filter(pk=pk).exists())
        event = AuditEvent.objects.get(pk=result.audit_event_id)
        self.assertEqual(event.details["cluster_node_enrollments_deleted"], 1)
        self.assertEqual(
            event.details["node_enrollments"],
            [{"node_ref_snapshot": "nr1:enrollment:pve1", "mode": "managed"}],
        )
        self.assertEqual(
            AuditEvent.objects.filter(
                action__in=ENROLLMENT_AUDIT_ACTIONS,
                cluster__isnull=True,
                cluster_key_snapshot="enrollment",
            ).count(),
            4,
        )

    def test_hard_delete_rolls_back_when_enrollment_owner_does_not_delete(self):
        cluster = _cluster()
        enrollment = _enrollment(cluster)

        with (
            patch(
                "core.services.cluster_deletion.retire_cluster_enrollments",
                return_value=EnrollmentRetirementResult(0, (), 0),
            ),
            self.assertRaises(ClusterDeletionPostconditionFailed),
        ):
            delete_unused_cluster_connection(cluster, actor=self.actor)

        self.assertTrue(ProxmoxCluster.objects.filter(pk=cluster.pk).exists())
        self.assertTrue(ClusterNodeEnrollment.objects.filter(pk=enrollment.pk).exists())
        self.assertFalse(AuditEvent.objects.filter(action="cluster.unused_connection_deleted").exists())

"""Executable acceptance contract for Module 5 phase 5a1H-1.

Every decision branch in the phase's entry contract has a test here that fails when
the branch is deleted. That is the phase's exit criterion — a reviewer's approval is
not, and a green suite after deleting a branch means the test is missing.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    AuditEvent,
    ClusterMembershipState,
    ClusterNodeEnrollment,
    ClusterNodeState,
    ClusterProjectionCoverage,
    ConsoleSession,
    CurrentGuestInventory,
    ProxmoxCluster,
    ProxmoxEndpoint,
    ProxmoxStorageConsumer,
    ScheduledAction,
)
from core.services.cluster_enrollment import (
    ClusterEnrollmentError,
    activate_cluster_enrollment,
    change_enrollment_mode,
    enroll_node,
    node_change_blockers,
    remove_enrollment,
)
from core.services.cluster_onboarding import ClusterOnboardingError, VerifiedConnection
from core.services.cluster_projection_read import read_cluster_projection
from core.views.clusters.enrollment import (
    STATE_DISCOVERED,
    STATE_ENROLLED_ABSENT,
    STATE_ENROLLED_UNDISCOVERED,
    STATE_MANAGED,
    STATE_SAFETY_ONLY,
    _assert_represents_node,
    _candidate_url_suggestion,
    node_enrollment_rows,
)


def _cluster(key: str = "hq", *, enabled: bool = True) -> ProxmoxCluster:
    return ProxmoxCluster.objects.create(key=key, display_name=key, enabled=enabled)


def _publish_membership(cluster, *nodes, generation: int = 4, present: bool = True) -> None:
    """Publish one complete membership generation containing ``nodes``."""

    now = timezone.now()
    ClusterMembershipState.objects.update_or_create(
        cluster=cluster,
        defaults={
            "membership_generation": generation,
            "member_count": len(nodes),
            "quorate": True,
            "observed_from": nodes[0] if nodes else "",
            "topology_role": "corosync",
        },
    )
    ClusterProjectionCoverage.objects.update_or_create(
        cluster=cluster,
        domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP,
        node_name=None,
        defaults={
            "generation": generation,
            # Membership coverage is the root scope; the constraint requires it to
            # carry no `based_on_generation`.
            "based_on_generation": None,
            "complete": True,
            "attempted_at": now,
            "observed_at": now,
            "error_code": "",
        },
    )
    for index, node_name in enumerate(nodes, start=1):
        ClusterNodeState.objects.update_or_create(
            cluster=cluster,
            node_name=node_name,
            defaults={
                "nodeid": index,
                "present": present,
                "online": True,
                "reported_ring_address": f"10.10.10.{30 + index}",
                "membership_generation": generation,
                "first_discovered_at": now,
                "last_discovered_at": now,
            },
        )


def _verified(local_node_name: str = "pve1") -> VerifiedConnection:
    from core.services.cluster_identity import ObservedClusterIdentity
    from core.services.cluster_trust import InspectedCertificate

    return VerifiedConnection(
        certificate=InspectedCertificate(subject="s", issuer="i", sha256_fingerprint="ab"),
        identity=ObservedClusterIdentity(ca_uuid="uuid-1", ca_fingerprint="fp-1"),
        node_names=("pve1", "pve2"),
        version="9.2.4",
        discovered_name="hq",
        administrator_privileges=(),
        local_node_name=local_node_name,
    )


class CandidateNodeProofTests(TestCase):
    """The `local=1` row is the only proof of *which* member a transport is."""

    def test_a_proven_matching_node_is_accepted(self):
        _assert_represents_node(_verified("pve1"), "pve1")

    def test_a_different_member_is_refused_and_named(self):
        with self.assertRaises(ClusterOnboardingError) as caught:
            _assert_represents_node(_verified("pve2"), "pve1")

        self.assertIn("pve2", str(caught.exception))
        self.assertIn("pve1", str(caught.exception))

    def test_an_unproven_mapping_is_refused_rather_than_guessed(self):
        """The tolerated loose payload leaves no proof, and enrollment must not infer one."""

        with self.assertRaises(ClusterOnboardingError) as caught:
            _assert_represents_node(_verified(""), "pve1")

        self.assertIn("local", str(caught.exception))

    def test_the_confirm_step_rebinds_the_node_and_not_only_the_identity(self):
        """Without this a URL that resolves elsewhere between verify and confirm commits."""

        from core.views.clusters.connections import _assert_verified_unchanged, _verified_data

        expected = _verified_data(_verified("pve1"))
        _assert_verified_unchanged(expected, _verified("pve1"))
        with self.assertRaises(ClusterOnboardingError):
            _assert_verified_unchanged(expected, _verified("pve2"))

    def test_the_signed_candidate_round_trip_preserves_the_proof(self):
        from core.views.clusters.connections import _verified_data, _verified_from_data

        restored = _verified_from_data(_verified_data(_verified("pve3")))

        self.assertEqual(restored.local_node_name, "pve3")


class CandidateUrlProvenanceTests(TestCase):
    def test_a_reported_ring_address_prefills_a_reviewable_candidate(self):
        self.assertEqual(_candidate_url_suggestion("10.10.10.31"), "https://10.10.10.31:8006")

    def test_no_reported_address_means_no_suggestion_and_never_a_synthesized_name(self):
        self.assertEqual(_candidate_url_suggestion(""), "")
        self.assertEqual(_candidate_url_suggestion("10.10.10.0/24"), "")


class EnrollmentGenerationTests(TestCase):
    """The clock advances once per committed change and never for a no-op."""

    def setUp(self):
        self.cluster = _cluster()

    def test_enrolling_advances_the_generation_exactly_once(self):
        write = enroll_node(self.cluster, node_name="pve1", mode="managed")
        self.cluster.refresh_from_db()

        self.assertTrue(write.changed)
        self.assertEqual(self.cluster.enrollment_generation, 1)
        self.assertEqual(write.generation, 1)

    def test_re_enrolling_an_enrolled_node_advances_nothing(self):
        enroll_node(self.cluster, node_name="pve1", mode="managed")
        write = enroll_node(self.cluster, node_name="pve1", mode="managed")
        self.cluster.refresh_from_db()

        self.assertFalse(write.changed)
        self.assertEqual(self.cluster.enrollment_generation, 1)

    def test_a_mode_change_advances_once_and_records_its_provenance(self):
        actor = get_user_model().objects.create_user(username="op", password="x")
        enroll_node(self.cluster, node_name="pve1", mode="managed")
        write = change_enrollment_mode(
            self.cluster, node_name="pve1", mode="safety_only", actor=actor, reason="hidden on purpose"
        )
        self.cluster.refresh_from_db()

        self.assertTrue(write.changed)
        self.assertEqual(write.previous_mode, "managed")
        self.assertEqual(self.cluster.enrollment_generation, 2)
        self.assertEqual(write.enrollment.mode_change_reason, "hidden on purpose")
        self.assertEqual(write.enrollment.mode_changed_by, actor)

    def test_setting_the_mode_it_already_has_advances_nothing(self):
        enroll_node(self.cluster, node_name="pve1", mode="managed")
        write = change_enrollment_mode(self.cluster, node_name="pve1", mode="managed")
        self.cluster.refresh_from_db()

        self.assertFalse(write.changed)
        self.assertEqual(self.cluster.enrollment_generation, 1)

    def test_removal_advances_once_and_deletes_the_row(self):
        enroll_node(self.cluster, node_name="pve1", mode="managed")
        write = remove_enrollment(self.cluster, node_name="pve1")
        self.cluster.refresh_from_db()

        self.assertTrue(write.changed)
        self.assertEqual(self.cluster.enrollment_generation, 2)
        self.assertFalse(ClusterNodeEnrollment.objects.filter(cluster=self.cluster).exists())

    def test_changing_an_unenrolled_node_is_refused_and_advances_nothing(self):
        with self.assertRaises(ClusterEnrollmentError):
            change_enrollment_mode(self.cluster, node_name="ghost", mode="safety_only")
        with self.assertRaises(ClusterEnrollmentError):
            remove_enrollment(self.cluster, node_name="ghost")
        self.cluster.refresh_from_db()

        self.assertEqual(self.cluster.enrollment_generation, 0)

    def test_an_invalid_mode_is_refused_before_anything_is_written(self):
        with self.assertRaises(ClusterEnrollmentError):
            enroll_node(self.cluster, node_name="pve1", mode="whatever")
        self.cluster.refresh_from_db()

        self.assertEqual(self.cluster.enrollment_generation, 0)
        self.assertFalse(ClusterNodeEnrollment.objects.exists())

    def test_enrollment_never_creates_a_storage_consumer(self):
        enroll_node(self.cluster, node_name="pve1", mode="managed")
        change_enrollment_mode(self.cluster, node_name="pve1", mode="safety_only")
        remove_enrollment(self.cluster, node_name="pve1")

        self.assertFalse(ProxmoxStorageConsumer.objects.exists())


class NodeChangeBlockerTests(TestCase):
    """A durable target must never be silently retargeted by a policy change."""

    def setUp(self):
        self.cluster = _cluster()
        enroll_node(self.cluster, node_name="pve1", mode="managed")

    def _guest(self, vmid: int = 101, node: str = "pve1"):
        return CurrentGuestInventory.objects.create(
            cluster=self.cluster,
            node=node,
            object_type="vm",
            vmid=vmid,
            name=f"guest{vmid}",
            observed_at=timezone.now(),
        )

    def _schedule(self, *, vmid: int = 101, target_node: str = ""):
        return ScheduledAction.objects.create(
            cluster=self.cluster,
            action_type=ScheduledAction.ActionType.START,
            target_type="vm",
            target_vmid=vmid,
            target_node=target_node,
            schedule_type=ScheduledAction.ScheduleType.ONCE,
            run_at=timezone.now(),
            enabled=True,
        )

    def test_a_schedule_is_found_through_current_placement_when_its_snapshot_is_blank(self):
        """`target_node` is blank whenever the guest was absent at save time."""

        self._guest()
        self._schedule(target_node="")

        blockers = node_change_blockers(self.cluster, "pve1")

        self.assertTrue(blockers)
        self.assertIn("vm/101", blockers[0])

    def test_a_stale_snapshot_still_blocks_even_with_no_current_placement(self):
        self._schedule(target_node="pve1")

        self.assertTrue(node_change_blockers(self.cluster, "pve1"))

    def test_a_schedule_for_a_guest_on_another_node_does_not_block(self):
        self._guest(node="pve2")
        self._schedule()

        self.assertEqual(node_change_blockers(self.cluster, "pve1"), [])

    def test_a_live_console_session_blocks(self):
        ConsoleSession.objects.create(
            cluster=self.cluster,
            target_node="pve1",
            target_type="vm",
            target_vmid=101,
            status=ConsoleSession.Status.CONNECTED,
            expires_at=timezone.now() + timezone.timedelta(minutes=10),
        )

        self.assertTrue(node_change_blockers(self.cluster, "pve1"))

    def test_the_phases_own_read_only_refresh_does_not_block_its_own_node(self):
        """`active_cluster_operation_labels` would self-block here; this predicate must not."""

        AuditEvent.objects.create(
            action="cluster.host_projection.refresh",
            cluster=self.cluster,
            outcome="queued",
            details={"node_name": "pve1"},
        )

        self.assertEqual(node_change_blockers(self.cluster, "pve1"), [])

    def test_a_blocked_node_refuses_both_hide_and_remove(self):
        self._guest()
        self._schedule()

        with self.assertRaises(ClusterEnrollmentError):
            change_enrollment_mode(self.cluster, node_name="pve1", mode="safety_only")
        with self.assertRaises(ClusterEnrollmentError):
            remove_enrollment(self.cluster, node_name="pve1")
        self.cluster.refresh_from_db()

        self.assertEqual(self.cluster.enrollment_generation, 1)


class NodePanelRowTests(TestCase):
    def setUp(self):
        self.cluster = _cluster()

    def test_every_row_state_is_derived_from_presence_and_enrollment(self):
        _publish_membership(self.cluster, "pve1", "pve2", "pve3")
        enroll_node(self.cluster, node_name="pve1", mode="managed")
        enroll_node(self.cluster, node_name="pve2", mode="safety_only")
        ClusterNodeState.objects.filter(cluster=self.cluster, node_name="pve2").update(present=False)

        states = {row["node_name"]: row["state"] for row in node_enrollment_rows(self.cluster)}

        self.assertEqual(states["pve1"], STATE_MANAGED)
        self.assertEqual(states["pve2"], STATE_ENROLLED_ABSENT)
        self.assertEqual(states["pve3"], STATE_DISCOVERED)

    def test_a_safety_only_node_that_is_present_reads_as_safety_only(self):
        _publish_membership(self.cluster, "pve1")
        enroll_node(self.cluster, node_name="pve1", mode="safety_only")

        self.assertEqual(node_enrollment_rows(self.cluster)[0]["state"], STATE_SAFETY_ONLY)

    def test_an_enrollment_without_a_discovery_row_is_still_listed(self):
        """Onboarding enrolls before the first reconcile; iterating discovery alone hides it."""

        enroll_node(self.cluster, node_name="pve1", mode="managed")

        rows = node_enrollment_rows(self.cluster)

        self.assertEqual([row["node_name"] for row in rows], ["pve1"])
        self.assertEqual(rows[0]["state"], STATE_ENROLLED_UNDISCOVERED)

    def test_the_panel_renders_the_discovery_timestamp_the_spec_requires(self):
        _publish_membership(self.cluster, "pve1")

        self.assertIsNotNone(node_enrollment_rows(self.cluster)[0]["last_discovered_at"])

    def test_the_read_owner_exposes_the_discovery_timestamps(self):
        _publish_membership(self.cluster, "pve1")

        node = read_cluster_projection(self.cluster.key).nodes[0]

        self.assertIsNotNone(node.first_discovered_at)
        self.assertIsNotNone(node.last_discovered_at)


class ConnectionsPassiveRenderTests(TestCase):
    """The Nodes panel must not turn a page view into provider traffic."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="op", password="secret")
        self.client.force_login(self.user)
        self.cluster = _cluster()
        _publish_membership(self.cluster, "pve1", "pve2")
        enroll_node(self.cluster, node_name="pve1", mode="managed")

    def test_rendering_the_connection_makes_zero_provider_calls(self):
        with (
            patch("core.services.proxmox.ProxmoxClient.__init__", side_effect=AssertionError("provider call")),
            patch("core.services.proxmox.ProxmoxClient.get", side_effect=AssertionError("provider call")),
        ):
            response = self.client.get(reverse("core:cluster_connection", args=[self.cluster.key]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Discovered, not added")
        self.assertContains(response, "Add node")


class EnrollmentActivationTests(TestCase):
    """5a1H-2. The one irreversible control in the phase."""

    def setUp(self):
        self.cluster = _cluster()
        _publish_membership(self.cluster, "pve1", "pve2", "pve3")

    def test_activation_enrolls_the_reviewed_set_and_advances_the_generation_once(self):
        """One reviewed set is one decision, not one decision per node."""

        actor = get_user_model().objects.create_user(username="op", password="x")

        result = activate_cluster_enrollment(
            self.cluster,
            selections={"pve1": "managed", "pve3": "managed", "pve2": "safety_only"},
            actor=actor,
        )
        self.cluster.refresh_from_db()

        self.assertEqual(self.cluster.enrollment_generation, 1)
        self.assertEqual(result.generation, 1)
        self.assertEqual(self.cluster.enrollment_contract_version, 1)
        self.assertIsNotNone(self.cluster.enrollment_activated_at)
        self.assertEqual(self.cluster.enrollment_activated_by, actor)
        modes = dict(ClusterNodeEnrollment.objects.filter(cluster=self.cluster).values_list("node_name", "mode"))
        self.assertEqual(modes, {"pve1": "managed", "pve2": "safety_only", "pve3": "managed"})

    def test_an_unenrolled_member_gets_no_row_at_all(self):
        """Absence of a row *is* unenrolled; activation must not invent a third mode."""

        activate_cluster_enrollment(self.cluster, selections={"pve1": "managed"})

        self.assertFalse(ClusterNodeEnrollment.objects.filter(cluster=self.cluster, node_name="pve2").exists())

    def test_activation_is_refused_once_the_contract_is_active(self):
        activate_cluster_enrollment(self.cluster, selections={"pve1": "managed"})

        with self.assertRaises(ClusterEnrollmentError):
            activate_cluster_enrollment(self.cluster, selections={"pve2": "managed"})
        self.cluster.refresh_from_db()

        self.assertEqual(self.cluster.enrollment_generation, 1)
        self.assertEqual(self.cluster.enrollment_contract_version, 1)

    def test_an_empty_set_is_refused_rather_than_publishing_nothing_silently(self):
        with self.assertRaises(ClusterEnrollmentError):
            activate_cluster_enrollment(self.cluster, selections={})
        self.cluster.refresh_from_db()

        self.assertEqual(self.cluster.enrollment_contract_version, 0)

    def test_a_node_outside_current_membership_cannot_be_reviewed(self):
        with self.assertRaises(ClusterEnrollmentError):
            activate_cluster_enrollment(self.cluster, selections={"pve99": "managed"})
        self.cluster.refresh_from_db()

        self.assertEqual(self.cluster.enrollment_contract_version, 0)
        self.assertFalse(ClusterNodeEnrollment.objects.filter(cluster=self.cluster).exists())

    def test_activation_needs_no_endpoint_for_a_hidden_node(self):
        """The deliberately weaker standard: a `safety_only` node has no endpoint."""

        activate_cluster_enrollment(self.cluster, selections={"pve1": "managed", "pve2": "safety_only"})

        hidden = ClusterNodeEnrollment.objects.get(cluster=self.cluster, node_name="pve2")
        self.assertIsNone(hidden.onboarded_via_endpoint)

    def test_activation_creates_no_storage_consumer(self):
        activate_cluster_enrollment(self.cluster, selections={"pve1": "managed", "pve2": "safety_only"})

        self.assertFalse(ProxmoxStorageConsumer.objects.exists())


class OnboardingEnrollmentContractTests(TestCase):
    """A new connection starts under the enrollment contract, never in legacy mode."""

    def test_a_proven_local_node_activates_the_contract_and_enrolls_it(self):
        from core.services.cluster_onboarding import _activate_enrollment_contract

        cluster = _cluster("new", enabled=True)
        endpoint = ProxmoxEndpoint.objects.create(cluster=cluster, name="pve1", url="https://pve1:8006", enabled=True)

        _activate_enrollment_contract(cluster, _verified("pve1"), endpoint)
        cluster.refresh_from_db()

        self.assertEqual(cluster.enrollment_contract_version, 1)
        self.assertIsNotNone(cluster.enrollment_activated_at)
        self.assertEqual(cluster.enrollment_generation, 1)
        enrollment = ClusterNodeEnrollment.objects.get(cluster=cluster)
        self.assertEqual(enrollment.node_name, "pve1")
        self.assertEqual(enrollment.mode, "managed")
        self.assertEqual(enrollment.onboarded_via_endpoint, endpoint)

    def test_an_unproven_node_leaves_the_connection_in_review_instead_of_stranding_it(self):
        from core.services.cluster_onboarding import _activate_enrollment_contract

        cluster = _cluster("unproven", enabled=True)
        endpoint = ProxmoxEndpoint.objects.create(cluster=cluster, name="e", url="https://e:8006", enabled=True)

        _activate_enrollment_contract(cluster, _verified(""), endpoint)
        cluster.refresh_from_db()

        self.assertEqual(cluster.enrollment_contract_version, 0)
        self.assertEqual(cluster.enrollment_generation, 0)
        self.assertFalse(ClusterNodeEnrollment.objects.filter(cluster=cluster).exists())

"""Contract tests for the standalone↔corosync role transition state machine.

Module 5 phase 5a0B. The state machine is pure, so these are ``SimpleTestCase``
and touch neither the database nor a provider: 5a0B deliberately does not invent
a live reader, and the rule is falsifiable without one. Phase 5a1B inherits these
as the contract its ``cluster/status`` adapter must satisfy.

Each test pins a sentence from ``docs/hosts&clusters.local.md`` rather than the
implementation's current shape.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from core.services.cluster_topology_role import (
    MembershipObservation,
    RoleTransition,
    TopologyRole,
    classify_role,
    evaluate_role_transition,
    resolve_transition,
)


def standalone_observation() -> MembershipObservation:
    """A complete read of a true standalone host: no corosync cluster row."""
    return MembershipObservation(complete=True, has_cluster_row=False, member_count=1, quorate=False)


def corosync_observation(*, member_count: int = 3, quorate: bool = True) -> MembershipObservation:
    return MembershipObservation(complete=True, has_cluster_row=True, member_count=member_count, quorate=quorate)


def failed_observation() -> MembershipObservation:
    """An incomplete read. ``has_cluster_row`` is set to the *wrong* answer on
    purpose: an incomplete observation must not be read for content at all."""
    return MembershipObservation(complete=False, has_cluster_row=True, member_count=0, quorate=False)


class RoleClassificationTests(SimpleTestCase):
    def test_a_complete_observation_without_a_cluster_row_is_standalone(self):
        self.assertIs(classify_role(standalone_observation()), TopologyRole.STANDALONE)

    def test_a_one_node_corosync_cluster_is_a_cluster_not_a_standalone(self):
        # The distinction the plan calls out explicitly: `clusterc`/`pve201`
        # returns a type=cluster row with nodes=1, quorate=1. It is a cluster.
        one_node = corosync_observation(member_count=1, quorate=True)
        self.assertIs(classify_role(one_node), TopologyRole.COROSYNC)

    def test_a_non_quorate_cluster_is_still_a_cluster(self):
        self.assertIs(classify_role(corosync_observation(quorate=False)), TopologyRole.COROSYNC)

    def test_an_incomplete_observation_classifies_nothing(self):
        self.assertIs(classify_role(failed_observation()), TopologyRole.UNKNOWN)


class RoleTransitionTests(SimpleTestCase):
    def test_first_complete_observation_adopts_without_a_handoff(self):
        decision = evaluate_role_transition(TopologyRole.UNKNOWN, corosync_observation())
        self.assertIs(decision.transition, RoleTransition.ADOPTED)
        self.assertIs(decision.role, TopologyRole.COROSYNC)
        self.assertFalse(decision.blocks_provider_work)

    def test_membership_churn_inside_a_cluster_is_not_an_identity_change(self):
        # A node joins, a node leaves, quorum is lost: all corosync, all stable.
        for observation in (
            corosync_observation(member_count=2),
            corosync_observation(member_count=20),
            corosync_observation(member_count=3, quorate=False),
        ):
            with self.subTest(members=observation.member_count, quorate=observation.quorate):
                decision = evaluate_role_transition(TopologyRole.COROSYNC, observation)
                self.assertIs(decision.transition, RoleTransition.STABLE)
                self.assertFalse(decision.blocks_provider_work)

    def test_standalone_becoming_clustered_is_transition_pending_and_blocks(self):
        decision = evaluate_role_transition(TopologyRole.STANDALONE, corosync_observation())
        self.assertIs(decision.transition, RoleTransition.TRANSITION_PENDING)
        self.assertTrue(decision.transition_pending)
        self.assertTrue(decision.blocks_provider_work)
        # The recorded role does not flip on observation alone -- that is what
        # makes this a two-identity hand-off rather than an in-place flag.
        self.assertIs(decision.role, TopologyRole.STANDALONE)
        # ...but the direction is machine-readable, not only in the message. A
        # caller must be able to ask "pending toward what?" without parsing prose.
        self.assertIs(decision.pending_role, TopologyRole.COROSYNC)
        self.assertIn("corosync", decision.reason)

    def test_a_cluster_reverting_to_standalone_also_blocks(self):
        decision = evaluate_role_transition(TopologyRole.COROSYNC, standalone_observation())
        self.assertIs(decision.transition, RoleTransition.TRANSITION_PENDING)
        self.assertIs(decision.role, TopologyRole.COROSYNC)
        self.assertIs(decision.pending_role, TopologyRole.STANDALONE)
        self.assertTrue(decision.blocks_provider_work)

    def test_an_unrecognized_stored_role_is_unknown_rather_than_an_exception(self):
        # An older binary's value or a hand-edited row must not raise out of a
        # periodic reconciler. "Unclassified" is what UNKNOWN means, and the next
        # complete observation re-adopts from it.
        decision = evaluate_role_transition("cluster-ish", corosync_observation())
        self.assertIs(decision.transition, RoleTransition.ADOPTED)
        self.assertIs(decision.role, TopologyRole.COROSYNC)
        self.assertFalse(decision.blocks_provider_work)


class DegradedObservationTests(SimpleTestCase):
    def test_a_failed_read_preserves_the_previous_good_role(self):
        for stored in (TopologyRole.STANDALONE, TopologyRole.COROSYNC, TopologyRole.UNKNOWN):
            with self.subTest(stored=stored):
                decision = evaluate_role_transition(stored, failed_observation())
                self.assertIs(decision.transition, RoleTransition.INDETERMINATE)
                self.assertIs(decision.role, stored)

    def test_a_failed_read_does_not_block_provider_work(self):
        # Unreachability is ordinary degradation. Blocking on it would make a
        # flapping network indistinguishable from a changed identity.
        decision = evaluate_role_transition(TopologyRole.COROSYNC, failed_observation())
        self.assertFalse(decision.blocks_provider_work)

    def test_a_failed_read_cannot_clear_a_pending_transition(self):
        decision = evaluate_role_transition(
            TopologyRole.STANDALONE, failed_observation(), pending_role=TopologyRole.COROSYNC
        )
        self.assertTrue(decision.transition_pending)
        self.assertIs(decision.pending_role, TopologyRole.COROSYNC)
        self.assertTrue(decision.blocks_provider_work)


class PendingTransitionTests(SimpleTestCase):
    def test_a_pending_transition_survives_an_agreeing_observation(self):
        # The trap: the host is now genuinely corosync, so every later read says
        # corosync. Publishing it would complete a hand-off nobody confirmed.
        decision = evaluate_role_transition(
            TopologyRole.STANDALONE, corosync_observation(), pending_role=TopologyRole.COROSYNC
        )
        self.assertIs(decision.transition, RoleTransition.TRANSITION_PENDING)
        self.assertIs(decision.role, TopologyRole.STANDALONE)
        self.assertIs(decision.pending_role, TopologyRole.COROSYNC)
        self.assertTrue(decision.blocks_provider_work)

    def test_a_reverted_change_clears_the_block_without_a_handoff(self):
        # The exit that keeps stickiness from stranding a working cluster: the
        # host left the cluster again, or the change was reverted in Proxmox, so
        # the scope is once more the role it is registered as. There is nothing
        # left to hand off, and asking the operator to confirm a transition that
        # no longer exists would be a gate with nowhere to go.
        decision = evaluate_role_transition(
            TopologyRole.STANDALONE, standalone_observation(), pending_role=TopologyRole.COROSYNC
        )
        self.assertIs(decision.transition, RoleTransition.STABLE)
        self.assertIs(decision.role, TopologyRole.STANDALONE)
        self.assertFalse(decision.transition_pending)
        self.assertFalse(decision.blocks_provider_work)

    def test_an_explicit_handoff_adopts_the_confirmed_role_and_unblocks(self):
        pending = evaluate_role_transition(TopologyRole.STANDALONE, corosync_observation())
        resolved = resolve_transition(pending, confirmed_role=TopologyRole.COROSYNC)
        self.assertIs(resolved.transition, RoleTransition.ADOPTED)
        self.assertIs(resolved.role, TopologyRole.COROSYNC)
        self.assertIs(resolved.previous_role, TopologyRole.STANDALONE)
        self.assertFalse(resolved.blocks_provider_work)

    def test_a_handoff_cannot_be_used_to_edit_a_stable_role(self):
        stable = evaluate_role_transition(TopologyRole.COROSYNC, corosync_observation())
        with self.assertRaises(ValueError):
            resolve_transition(stable, confirmed_role=TopologyRole.STANDALONE)

    def test_a_handoff_must_confirm_a_concrete_role(self):
        pending = evaluate_role_transition(TopologyRole.STANDALONE, corosync_observation())
        with self.assertRaises(ValueError):
            resolve_transition(pending, confirmed_role=TopologyRole.UNKNOWN)

    def test_a_handoff_must_confirm_the_role_the_scope_is_pending_toward(self):
        # Without this guard the seam is a back door: a scope pending toward
        # corosync could be "confirmed" as standalone, unblocking provider work
        # against an identity nobody observed. The next complete observation
        # re-trips the block, so the old shape was a transient unblock rather
        # than a durable forgery -- still not a guarantee worth claiming.
        pending = evaluate_role_transition(TopologyRole.STANDALONE, corosync_observation())
        self.assertIs(pending.pending_role, TopologyRole.COROSYNC)
        with self.assertRaises(ValueError):
            resolve_transition(pending, confirmed_role=TopologyRole.STANDALONE)

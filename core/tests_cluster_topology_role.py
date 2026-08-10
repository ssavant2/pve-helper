"""Contract tests for the standalone↔corosync role transition state machine.

Module 5 phase 5a0B. The state machine is pure, so these are ``SimpleTestCase``
and touch neither the database nor a provider: 5a0B deliberately does not invent
a live reader, and the rule is falsifiable without one. Phase 5a1B inherits these
as the contract its ``cluster/status`` adapter must satisfy.

Each test pins a sentence from ``docs/hosts&clusters.local.md`` rather than the
implementation's current shape.
"""

from __future__ import annotations

import dataclasses

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

    def test_an_unrecognized_stored_role_is_readopted_but_says_so(self):
        # An older binary's value or a hand-edited row must not raise out of a
        # periodic reconciler, and it must self-heal rather than strand. But
        # adopting over a value this build merely could not read is a flip
        # between the Hosts and Clusters groups, so it gets its own verdict
        # instead of passing as an ordinary first classification.
        decision = evaluate_role_transition("cluster-ish", corosync_observation())
        self.assertIs(decision.transition, RoleTransition.ADOPTED_OVER_UNRECOGNIZED)
        self.assertIs(decision.role, TopologyRole.COROSYNC)
        self.assertFalse(decision.blocks_provider_work)
        self.assertIn("not recognized", decision.reason)

    def test_a_never_classified_scope_adopts_quietly(self):
        # The contrast that gives the previous test its meaning: UNKNOWN means
        # never classified, and adopting over it is ordinary and silent.
        decision = evaluate_role_transition(TopologyRole.UNKNOWN, corosync_observation())
        self.assertIs(decision.transition, RoleTransition.ADOPTED)
        self.assertEqual(decision.reason, "")


class TransitionVocabularyRatchetTests(SimpleTestCase):
    """A new verdict must be considered against the block matrix, not merely added.

    Modelled on `test_reverse_relation_count_matches_the_contract`: pinning a
    count is what turns "remember to update the table" into a failing build.
    """

    def test_the_transition_vocabulary_is_ratcheted(self):
        self.assertEqual(
            sorted(member.value for member in RoleTransition),
            [
                "adopted",
                "adopted_over_unrecognized",
                "indeterminate",
                "observer_not_a_member",
                "stable",
                "transition_pending",
                "transition_withdrawn",
            ],
            "A new RoleTransition means a new return path. Add it to "
            "test_no_path_lets_an_unreadable_target_delete_the_block's matrix and to "
            "the 5a0B rule table before updating this list.",
        )

    def test_only_pendingness_decides_a_block(self):
        # The property that makes a missed verdict non-catastrophic: no caller
        # can unblock provider work by failing to handle a new transition,
        # because the block never derives from `transition`.
        blocking = evaluate_role_transition(TopologyRole.STANDALONE, corosync_observation())
        for member in RoleTransition:
            with self.subTest(transition=member):
                self.assertTrue(dataclasses.replace(blocking, transition=member).blocks_provider_work)


class ObservationProvenanceTests(SimpleTestCase):
    """A read is complete *of what the answering node can see*. That is not the
    same as complete *for this scope*, and conflating them is how an evicted
    member gets to declare the cluster standalone."""

    def test_a_departed_member_does_not_speak_for_the_scope(self):
        # `pvecm delnode pve3` leaves pve3 a standalone host whose endpoint this
        # installation may still have registered -- endpoints and membership are
        # separate by design. Its cluster/status is honest and complete, and says
        # nothing about clusterhq.
        evicted = MembershipObservation(
            complete=True,
            has_cluster_row=False,
            member_count=1,
            observed_from="pve3",
            accepted_members=frozenset({"pve1", "pve2"}),
        )
        self.assertFalse(evicted.speaks_for_the_scope)
        self.assertIs(classify_role(evicted), TopologyRole.UNKNOWN)

    def test_an_evicted_member_cannot_block_a_healthy_cluster(self):
        # The defect this rule exists for: without it the scope alternates
        # between blocked and unblocked depending on which endpoint answered,
        # with no operator ever asked and no durable trace.
        evicted = MembershipObservation(
            complete=True,
            has_cluster_row=False,
            observed_from="pve3",
            accepted_members=frozenset({"pve1", "pve2"}),
        )
        decision = evaluate_role_transition(TopologyRole.COROSYNC, evicted)
        self.assertIs(decision.transition, RoleTransition.OBSERVER_NOT_A_MEMBER)
        self.assertFalse(decision.blocks_provider_work)
        self.assertIs(decision.role, TopologyRole.COROSYNC)

    def test_a_stale_endpoint_is_not_reported_as_a_coverage_problem(self):
        # Both causes of UNKNOWN preserve the role, but they need different
        # repairs: one says the cluster is unreachable, the other says an
        # endpoint outlived its node. Reporting the second as the first sends
        # the operator to look for a network fault that does not exist.
        foreign = MembershipObservation(
            complete=True,
            has_cluster_row=False,
            observed_from="pve3",
            accepted_members=frozenset({"pve1", "pve2"}),
        )
        unreachable = MembershipObservation(
            complete=False, has_cluster_row=True, observed_from="pve1", accepted_members=frozenset({"pve1"})
        )
        stale = evaluate_role_transition(TopologyRole.COROSYNC, foreign)
        down = evaluate_role_transition(TopologyRole.COROSYNC, unreachable)

        self.assertNotEqual(stale, down)
        self.assertIs(down.transition, RoleTransition.INDETERMINATE)
        self.assertIn("pve3", stale.reason)
        self.assertNotIn("incomplete", stale.reason)

    def test_a_non_member_read_cannot_withdraw_a_pending_transition(self):
        foreign = MembershipObservation(
            complete=True,
            has_cluster_row=True,
            observed_from="pve3",
            accepted_members=frozenset({"pve1", "pve2"}),
        )
        decision = evaluate_role_transition(TopologyRole.COROSYNC, foreign, pending_role=TopologyRole.STANDALONE)
        self.assertIs(decision.pending_role, TopologyRole.STANDALONE)
        self.assertTrue(decision.blocks_provider_work)

    def test_a_member_of_record_still_speaks_for_the_scope(self):
        genuine = MembershipObservation(
            complete=True,
            has_cluster_row=False,
            observed_from="pve1",
            accepted_members=frozenset({"pve1", "pve2"}),
        )
        self.assertTrue(genuine.speaks_for_the_scope)
        self.assertIs(classify_role(genuine), TopologyRole.STANDALONE)

    def test_the_check_is_disabled_before_any_membership_is_accepted(self):
        # First onboarding has no prior membership. A check that refused the
        # first read would deadlock adoption, which is a strand of its own.
        first = MembershipObservation(complete=True, has_cluster_row=True, observed_from="pve1")
        self.assertTrue(first.speaks_for_the_scope)
        self.assertIs(classify_role(first), TopologyRole.COROSYNC)

    def test_an_unidentified_reader_degrades_rather_than_refuses(self):
        # 5a1B supplies observed_from from cluster/status's local=1 row. If it
        # cannot, the check is skipped rather than turning every read into a
        # refusal -- degrade to the old behavior, never to a lockout.
        anonymous = MembershipObservation(complete=True, has_cluster_row=True, accepted_members=frozenset({"pve1"}))
        self.assertTrue(anonymous.speaks_for_the_scope)


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

    def test_a_reverted_change_withdraws_the_block_and_says_so(self):
        # The exit that keeps stickiness from stranding a working cluster: the
        # host left the cluster again, or the change was reverted in Proxmox, so
        # the scope is once more the role it is registered as. There is nothing
        # left to hand off, and asking the operator to confirm a transition that
        # no longer exists would be a gate with nowhere to go.
        decision = evaluate_role_transition(
            TopologyRole.STANDALONE, standalone_observation(), pending_role=TopologyRole.COROSYNC
        )
        self.assertIs(decision.transition, RoleTransition.TRANSITION_WITHDRAWN)
        self.assertIs(decision.role, TopologyRole.STANDALONE)
        self.assertFalse(decision.transition_pending)
        self.assertFalse(decision.blocks_provider_work)
        self.assertIn("withdrawn", decision.reason)

    def test_a_withdrawal_is_distinguishable_from_an_ordinary_steady_state(self):
        # It clears a provider-work block. A block that clears itself while
        # rendering as an ordinary STABLE is harder to diagnose than one that
        # persists, and the shipped precedent -- CA-identity quarantine -- is
        # only ever cleared by an explicit audited action. 5a1B must be able to
        # tell these apart in order to record the clear.
        withdrawn = evaluate_role_transition(
            TopologyRole.STANDALONE, standalone_observation(), pending_role=TopologyRole.COROSYNC
        )
        ordinary = evaluate_role_transition(TopologyRole.STANDALONE, standalone_observation())
        self.assertNotEqual(withdrawn, ordinary)
        self.assertIs(ordinary.transition, RoleTransition.STABLE)

    def test_no_path_lets_an_unreadable_target_delete_the_block(self):
        """The readability of a persisted target must not decide whether a block
        exists -- on **any** return path.

        Written as a matrix rather than one case on purpose: this defect was
        found, fixed on the one path under the author's nose, and reproduced on
        the other two in the next review round.

        The docstring used to claim the table forced a new path to join it. It
        did not -- it is a hand-written dict, and a fifth path would have slipped
        in silently. `test_the_transition_vocabulary_is_ratcheted` is what
        actually makes that true.
        """
        evicted = MembershipObservation(
            complete=True,
            has_cluster_row=False,
            observed_from="pve3",
            accepted_members=frozenset({"pve1", "pve2"}),
        )
        paths = {
            "incomplete read": failed_observation(),
            "reader is not a member": evicted,
            "complete, role disagrees": corosync_observation(),
            "complete, role agrees": standalone_observation(),
        }
        for label, observation in paths.items():
            with self.subTest(path=label):
                readable = evaluate_role_transition(
                    TopologyRole.STANDALONE, observation, pending_role=TopologyRole.COROSYNC
                )
                unreadable = evaluate_role_transition(TopologyRole.STANDALONE, observation, pending_role="corosync-v2")
                self.assertEqual(
                    readable.blocks_provider_work,
                    unreadable.blocks_provider_work,
                    f"on '{label}' the block appears or disappears purely because the "
                    "persisted target could not be read",
                )
                self.assertIs(readable.transition, unreadable.transition)

    def test_both_persistence_shapes_round_trip_an_unnameable_block(self):
        """5a1A can persist either shape, and neither may drop the block.

        The trap this closes: `pending_role` is `UNKNOWN` in exactly this state,
        so a caller that persists that field alone and feeds it back reads it as
        "not pending" one cycle later. Either persist the raw stored target, or
        persist the pair -- both are contractual, the field alone is not.
        """
        first = evaluate_role_transition(TopologyRole.STANDALONE, failed_observation(), pending_role="corosync-v2")
        self.assertTrue(first.blocks_provider_work)

        raw_shape = evaluate_role_transition(TopologyRole.STANDALONE, failed_observation(), pending_role="corosync-v2")
        pair_shape = evaluate_role_transition(
            TopologyRole.STANDALONE,
            failed_observation(),
            pending_role=first.pending_role,
            pending_unreadable=first.pending_unreadable,
        )
        for label, decision in (("raw target", raw_shape), ("role + flag", pair_shape)):
            with self.subTest(persisted=label):
                self.assertTrue(decision.blocks_provider_work)
                self.assertTrue(decision.pending_unreadable)

    def test_an_unnameable_block_is_representable_rather_than_dropped(self):
        # The root cause of the above: pendingness used to be derived from the
        # role alone, so "pending toward something this build cannot name" did
        # not exist as a state -- and the paths with no nameable role to
        # substitute silently dropped the block instead of holding it.
        decision = evaluate_role_transition(TopologyRole.STANDALONE, failed_observation(), pending_role="corosync-v2")
        self.assertTrue(decision.pending_unreadable)
        self.assertIs(decision.pending_role, TopologyRole.UNKNOWN)
        self.assertTrue(decision.blocks_provider_work)
        self.assertIn("could not be read", decision.reason)

    def test_an_unnameable_block_escalates_rather_than_stranding(self):
        """The gate rule, applied to identity: unknown state escalates.

        This refused once, on the reasoning that the target becomes nameable
        again from the next complete member read. That is false whenever no
        accepted member can answer -- every accepted node removed from the
        cluster, or none of them holding a registered endpoint. Then provider
        work is blocked, the hand-off is refused, and there is nowhere for the
        operator to go, forever. A refusal that waits on a machine which cannot
        answer is a permanent lockout, not a safeguard.
        """
        stuck = evaluate_role_transition(
            TopologyRole.STANDALONE, self.no_member_can_answer(), pending_role="corosync-v2"
        )
        self.assertTrue(stuck.blocks_provider_work)
        self.assertTrue(stuck.pending_unreadable)

        resolved = resolve_transition(stuck, confirmed_role=TopologyRole.COROSYNC)
        self.assertIs(resolved.role, TopologyRole.COROSYNC)
        self.assertFalse(resolved.blocks_provider_work)
        # ...and it is recorded as acknowledged, not derived: no observation
        # backed this role, and 5a1G must audit it that way.
        self.assertTrue(resolved.confirmed_without_target)
        self.assertIn("no observation backs", resolved.reason)

    @staticmethod
    def no_member_can_answer() -> MembershipObservation:
        """No accepted member is reachable or registered, so no read can ever
        speak for the scope. The premise the removed refusal assumed away."""
        return MembershipObservation(
            complete=True,
            has_cluster_row=True,
            observed_from="pve9",
            accepted_members=frozenset({"removed-a", "removed-b"}),
        )

    def test_the_lockout_state_does_not_resolve_itself(self):
        for _ in range(3):
            decision = evaluate_role_transition(
                TopologyRole.STANDALONE, self.no_member_can_answer(), pending_role="corosync-v2"
            )
            self.assertIs(decision.transition, RoleTransition.OBSERVER_NOT_A_MEMBER)
            self.assertTrue(decision.blocks_provider_work)

    def test_an_unnameable_block_is_renameable_from_a_member_read(self):
        recovered = evaluate_role_transition(
            TopologyRole.STANDALONE, corosync_observation(), pending_role="corosync-v2"
        )
        self.assertIs(recovered.pending_role, TopologyRole.COROSYNC)
        self.assertTrue(recovered.pending_role_substituted)
        self.assertIs(
            resolve_transition(recovered, confirmed_role=TopologyRole.COROSYNC).role,
            TopologyRole.COROSYNC,
        )

    def test_an_unreadable_pending_target_does_not_silently_delete_the_block(self):
        # The asymmetry this closes: an unrecognized *stored* role only mislabels
        # a group, and got its own verdict. An unrecognized *pending* role
        # deletes a provider-work block, and was reading as "not pending" --
        # STABLE, no verdict, no record. That is the silent unblock this module
        # rejects one branch away.
        withdrawn = evaluate_role_transition(
            TopologyRole.STANDALONE, standalone_observation(), pending_role="corosync-v2"
        )
        self.assertIs(withdrawn.transition, RoleTransition.TRANSITION_WITHDRAWN)
        self.assertIn("unreadable", withdrawn.reason)

    def test_an_unreadable_pending_target_is_retargeted_rather_than_stranded(self):
        # Keeping a target this build cannot name would strand the scope:
        # resolve_transition can only confirm a role it can name. So the block
        # survives, retargeted to what is actually observed, and says why.
        decision = evaluate_role_transition(TopologyRole.STANDALONE, corosync_observation(), pending_role="corosync-v2")
        self.assertIs(decision.transition, RoleTransition.TRANSITION_PENDING)
        self.assertTrue(decision.blocks_provider_work)
        self.assertIs(decision.pending_role, TopologyRole.COROSYNC)
        self.assertIn("could not be read", decision.reason)

    def test_the_pending_target_is_not_re_derived_each_cycle(self):
        # With two roles the observed and the already-pending target coincide,
        # so this is a guard for the widened enum: re-deriving would silently
        # retarget the question the operator was asked, cycle after cycle.
        decision = evaluate_role_transition(
            TopologyRole.STANDALONE, corosync_observation(), pending_role=TopologyRole.COROSYNC
        )
        self.assertIs(decision.pending_role, TopologyRole.COROSYNC)

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

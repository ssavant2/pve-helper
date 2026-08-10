"""The standalone↔corosync role transition state machine, as pure functions.

Module 5 phase 5a0B owns this contract; ``docs/hosts&clusters.local.md`` (*Read
model, freshness, standalone*) is the prose. Phase 5a1B builds the live
``cluster/status`` reader that feeds it and the persistence that records its
outcome. **Nothing here performs I/O, touches the ORM or knows a provider
payload's shape** — it takes an already-normalized observation and returns a
decision, so the rule can be falsified without a cluster.

Three facts the rest of the module follows from:

* **A role is classified only from a complete membership observation.** Not from
  endpoint count, node count, ``discovered_name``, URL namespace or a separate
  local identity. A true standalone is a complete observation carrying no
  ``type=cluster`` row; a one-node corosync cluster carries one and is a cluster.
  An incomplete observation classifies nothing — it preserves whatever was
  previously known, because a failed read must never flip a host between the
  Hosts and Clusters groups.

* **Standalone→clustered is an identity transition, not an in-place flag.** A
  complete observation that changes the role marks the scope transition-pending
  and blocks new provider work until an explicit operator hand-off. Ordinary
  membership churn *inside* an already-corosync cluster (a node joins, a node
  leaves, quorum is lost) is not an identity change and must not trip this.

* **The state machine decides; it never acts.** Retirement, the two-identity
  hand-off and Audit preservation stay with their owners
  (``docs/cluster-retire.local.md``: generic retirement is explicitly *not* the
  standalone-to-corosync coordinator).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class TopologyRole(enum.StrEnum):
    """What a cluster scope's membership observation proved it to be."""

    #: A complete observation with no corosync cluster row: a single PVE host.
    STANDALONE = "standalone"
    #: A complete observation carrying a corosync cluster row. One-node corosync
    #: is a cluster and belongs here, quorate or not.
    COROSYNC = "corosync"
    #: Never classified from a complete observation, or last classification is
    #: gone. Not a third topology: an absence of evidence.
    UNKNOWN = "unknown"


class RoleTransition(enum.StrEnum):
    """What a new observation means for a scope's recorded role."""

    #: Complete observation, role unchanged. Ordinary churn lands here.
    STABLE = "stable"
    #: Complete observation against a scope that had no classified role yet.
    #: First classification is adoption, not a transition: there is no old
    #: identity to hand off from.
    ADOPTED = "adopted"
    #: Complete observation, role changed. The scope is transition-pending and
    #: new provider work is blocked until the explicit operator hand-off.
    TRANSITION_PENDING = "transition_pending"
    #: A pending transition ended without a hand-off because the scope came back
    #: to its registered role. Distinct from :attr:`STABLE` on purpose: it clears
    #: a provider-work block, and a block that clears itself must still be
    #: recorded. The shipped precedent -- CA-identity quarantine -- is only ever
    #: cleared by an explicit, audited operator action, so an automatic clear
    #: that renders as an ordinary steady state is the wrong default.
    TRANSITION_WITHDRAWN = "transition_withdrawn"
    #: A complete observation was adopted over a stored value this binary does
    #: not recognize. Adoption is right -- a garbage row must self-heal rather
    #: than strand -- but it is a silent flip between the Hosts and Clusters
    #: groups unless it says so.
    ADOPTED_OVER_UNRECOGNIZED = "adopted_over_unrecognized"
    #: Incomplete observation. Previous-good role is preserved and stale; no
    #: conclusion is drawn in either direction.
    INDETERMINATE = "indeterminate"
    #: The read was complete, but came from a node this scope does not accept as
    #: a member, so it is not evidence about this scope. Distinct from
    #: :attr:`INDETERMINATE` because the repair is different and the operator is
    #: otherwise told the wrong thing: coverage is fine, the *endpoint* is stale.
    OBSERVER_NOT_A_MEMBER = "observer_not_a_member"


@dataclass(frozen=True)
class MembershipObservation:
    """One normalized membership read, independent of the provider's wire shape.

    ``complete`` is the coverage verdict the 5a1B reader supplies, not something
    inferred here. ``has_cluster_row`` is meaningful only when ``complete``.

    **``observed_from`` is not bookkeeping — it is what makes ``complete``
    answerable.** A read is complete *of what the answering node can see*, and
    that is not the same as complete *for this scope*. Run ``pvecm delnode`` on a
    member and leave its endpoint registered — the shipped model keeps endpoints
    and enrollment separate on purpose — and that host now returns a perfectly
    honest, complete ``cluster/status`` with no ``type=cluster`` row. Believing it
    would say the whole cluster became standalone, on the word of a machine that
    was evicted from it.

    So an observation names the node it came from (the ``local=1`` row of
    ``cluster/status``, the same one-call proof the endpoint→node identity read
    uses) and the caller supplies ``accepted_members``: the membership this scope
    has already accepted. A read from outside that set is **not evidence about
    this scope's topology** and classifies as ``UNKNOWN`` rather than standalone.

    ``accepted_members`` empty means "nothing accepted yet" and disables the
    check -- first onboarding has no prior membership to check against, and a
    check that refuses the first read would deadlock adoption.
    """

    complete: bool
    has_cluster_row: bool
    member_count: int = 0
    quorate: bool = False
    #: The node that answered, from ``cluster/status``'s ``local=1`` row. Empty
    #: means the reader could not identify it, which disables the check below --
    #: 5a1B is expected to supply it, and a missing value degrades to today's
    #: behavior rather than to a refusal.
    observed_from: str = ""
    #: The membership this scope has already accepted. Empty disables the check.
    accepted_members: frozenset[str] = frozenset()

    @property
    def speaks_for_the_scope(self) -> bool:
        """Whether the answering node is one this scope accepts as a member."""
        if not self.accepted_members or not self.observed_from:
            return True
        return self.observed_from in self.accepted_members


@dataclass(frozen=True)
class RoleDecision:
    """The state machine's verdict for one observation against one stored role."""

    transition: RoleTransition
    #: The role to publish. On an incomplete observation this is the stored role,
    #: unchanged -- including ``UNKNOWN``.
    role: TopologyRole
    previous_role: TopologyRole
    #: The role this scope is pending *toward*, or ``UNKNOWN`` when nothing is
    #: pending. This is the field 5a1A persists: without it a pending state
    #: reconstructed in a later generation could not say which way it points, and
    #: the operator prompt would have to be parsed out of ``reason``.
    pending_role: TopologyRole = TopologyRole.UNKNOWN
    #: A stable, operator-facing reason, or "" when nothing needs explaining.
    reason: str = ""

    @property
    def transition_pending(self) -> bool:
        """Whether the scope waits for the explicit two-identity hand-off."""
        return self.pending_role is not TopologyRole.UNKNOWN

    @property
    def blocks_provider_work(self) -> bool:
        """Whether new provider work must be refused for this scope.

        Only a pending role transition blocks. An incomplete observation does
        not: unreachability is an ordinary degraded state that the projection
        already renders as stale, and blocking on it would make a flapping
        network indistinguishable from a changed identity.

        **This block may not be persisted ahead of its operator exit.** A gate
        escalates; it never strands. The shipped precedent is CA-identity
        quarantine, which landed its block together with the panel that explains
        it and the *Re-approve current identity* action that clears it
        (``core/services/cluster_identity.py``, ``cluster_connection.html``). The
        phase that first writes this state to a row must ship the confirmation
        surface in the same slice, or a functioning cluster is blocked with
        nowhere for the operator to go.
        """
        return self.transition_pending


def classify_role(observation: MembershipObservation) -> TopologyRole:
    """Return the role a single observation proves, or ``UNKNOWN``.

    The one rule: a complete observation with no corosync cluster row is
    standalone, with one is corosync. Member count and quorum are deliberately
    not inputs -- a one-node corosync cluster is a cluster, and a non-quorate
    multi-node cluster has not become a standalone host.

    A read from a node this scope does not accept as a member proves nothing
    about the scope, however complete it is of itself. See
    :attr:`MembershipObservation.speaks_for_the_scope`.
    """
    if not observation.complete or not observation.speaks_for_the_scope:
        return TopologyRole.UNKNOWN
    return TopologyRole.COROSYNC if observation.has_cluster_row else TopologyRole.STANDALONE


def _coerce_role(value: object) -> tuple[TopologyRole, bool]:
    """Return ``(role, was_recognized)``.

    A persisted role that is not a member of the enum -- an older binary's value,
    a hand-edited row -- must not raise out of a periodic reconciler, so it maps
    to ``UNKNOWN`` and the next complete observation re-adopts.

    **But ``UNKNOWN`` and "unrecognized" are not the same thing**, which is why
    this returns the second value. ``UNKNOWN`` means never classified, and
    adopting over it is correct and quiet. Adopting over a value this binary
    merely could not read is a silent flip between the Hosts and Clusters groups
    -- the exact move the module forbids elsewhere. Today ``TopologyRole`` has two
    concrete members so only a hand-edited row reaches it; **widening this enum
    makes it live**, because a rolling deploy has one binary writing values the
    other cannot read. Anyone adding a member must revisit **both** consumers of
    the flag in :func:`evaluate_role_transition`: the stored role, where the worst
    case is a group-label flip, and the *pending* role, where reading an
    unrecognized value as "not pending" would delete a provider-work block with
    no verdict and no record.
    """
    try:
        return TopologyRole(value), True
    except ValueError:
        return TopologyRole.UNKNOWN, False


def evaluate_role_transition(
    stored_role: TopologyRole,
    observation: MembershipObservation,
    *,
    pending_role: TopologyRole = TopologyRole.UNKNOWN,
) -> RoleDecision:
    """Decide what one observation means for a scope's recorded role.

    ``pending_role`` carries a hand-off a previous generation opened, and says
    which role it points *toward*. It is sticky on purpose: an observation that
    merely agrees with the new role does not complete it, and neither does a
    failed read. Only :func:`resolve_transition` clears it -- with one exception
    that is an exit rather than a bypass, below.
    """
    stored_role, stored_recognized = _coerce_role(stored_role)
    pending_role, pending_recognized = _coerce_role(pending_role)
    observed = classify_role(observation)

    if observed is TopologyRole.UNKNOWN:
        # Two different things produce UNKNOWN and they need different repairs.
        # Reporting both as "coverage is incomplete" tells an operator to go
        # looking for an unreachable cluster when the actual fix is to delete a
        # stale endpoint for a node that was evicted.
        if not observation.speaks_for_the_scope:
            return RoleDecision(
                transition=RoleTransition.OBSERVER_NOT_A_MEMBER,
                role=stored_role,
                previous_role=stored_role,
                pending_role=pending_role,
                reason=(
                    f"The node that answered ({observation.observed_from}) is not a member this "
                    "scope accepts, so its complete response is not evidence about this scope. "
                    "Its endpoint is probably stale."
                ),
            )
        return RoleDecision(
            transition=RoleTransition.INDETERMINATE,
            role=stored_role,
            previous_role=stored_role,
            pending_role=pending_role,
            reason="Membership coverage is incomplete; the previous role is preserved as stale.",
        )

    # An unrecognized pending value is still an open block. Reading it as "not
    # pending" would let a hand-edited row or a widened enum *delete* a
    # provider-work block with no verdict and no record -- the silent unblock
    # this module rejects one branch away, and the more dangerous direction of
    # the two, since the stored-role case only mislabels a group.
    if pending_role is not TopologyRole.UNKNOWN or not pending_recognized:
        unreadable = " Its previous target could not be read by this build." if not pending_recognized else ""
        if observed is stored_role:
            # The scope came back to the role it is registered as: the host
            # rejoined, or the change was reverted in Proxmox. There is no longer
            # anything to hand off, so the block clears itself. This is the exit
            # that keeps stickiness from being a strand -- the operator is not
            # asked to confirm a transition that no longer exists.
            #
            # It gets its own verdict rather than reporting an ordinary STABLE:
            # this clears a provider-work block, and a block that clears itself
            # without saying so is worse to diagnose than one that persists.
            target = pending_role.value if pending_recognized else "an unreadable role"
            return RoleDecision(
                transition=RoleTransition.TRANSITION_WITHDRAWN,
                role=stored_role,
                previous_role=stored_role,
                reason=(
                    f"The pending transition toward {target} was withdrawn: this scope is "
                    f"observed as {stored_role.value} again, which is what it is registered "
                    f"as. Provider work is no longer blocked.{unreadable}"
                ),
            )
        # A pending hand-off outranks a fresh reading. Publishing the new role
        # here would silently complete the transition the operator has not yet
        # confirmed, which is the whole thing this state blocks.
        #
        # The pending target is the one already opened, not the freshly observed
        # role. With two roles they coincide; under a widened enum, re-deriving
        # it would silently retarget the question the operator was asked, each
        # cycle, with no one told. The exception is a target this build cannot
        # read: keeping it would strand the scope, because resolve_transition
        # cannot confirm a role it cannot name.
        return RoleDecision(
            transition=RoleTransition.TRANSITION_PENDING,
            role=stored_role,
            previous_role=stored_role,
            pending_role=pending_role if pending_recognized else observed,
            reason=(
                f"This scope is registered as {stored_role.value} but is observed as "
                f"{observed.value}. Provider work is blocked until the identity hand-off "
                f"is confirmed.{unreadable}"
            ),
        )

    if not stored_recognized:
        # Adopt, because a garbage row must self-heal rather than strand -- but
        # say so, because this is a role flip nobody asked for.
        return RoleDecision(
            transition=RoleTransition.ADOPTED_OVER_UNRECOGNIZED,
            role=observed,
            previous_role=TopologyRole.UNKNOWN,
            reason=(
                f"The stored topology role was not recognized by this build and has been "
                f"re-adopted as {observed.value} from a complete observation."
            ),
        )

    if stored_role is TopologyRole.UNKNOWN:
        return RoleDecision(
            transition=RoleTransition.ADOPTED,
            role=observed,
            previous_role=stored_role,
        )

    if observed is stored_role:
        return RoleDecision(
            transition=RoleTransition.STABLE,
            role=observed,
            previous_role=stored_role,
        )

    return RoleDecision(
        transition=RoleTransition.TRANSITION_PENDING,
        role=stored_role,
        previous_role=stored_role,
        pending_role=observed,
        reason=(
            f"This scope is registered as {stored_role.value} but is observed as "
            f"{observed.value}. Provider work is blocked until the identity hand-off is confirmed."
        ),
    )


def resolve_transition(decision: RoleDecision, *, confirmed_role: TopologyRole) -> RoleDecision:
    """Apply an operator-confirmed hand-off to a pending decision.

    The caller has already performed the two-identity transaction specified in
    ``docs/hosts&clusters.local.md`` (*The standalone↔corosync hand-off*) and
    owned by phase 5a1G, which needs 5a1A's schema before it can be written.
    This function is that transaction's seam, not its implementation: it moves
    only the role.

    Three guards, each of which exists because its absence would make this a way
    to edit a role rather than to conclude a hand-off:

    * the decision must actually be pending;
    * the confirmed role must be concrete;
    * **the confirmed role must be the role the scope is pending toward.** An
      operator confirming a hand-off is answering the question the state machine
      asked, not choosing a role freely.

    **A decision is a value, not a lock.** It can outlive the state it describes:
    a pending decision handed to an operator stays internally consistent even
    after a later cycle withdrew the transition, so these guards cannot detect
    that the question is stale. 5a1G re-derives the decision from current state
    under the cluster lifecycle lock before committing the hand-off (its
    acceptance criterion 7); that is a transaction property, not one a pure
    function can enforce.
    """
    if not decision.transition_pending:
        raise ValueError("Only a transition-pending decision can be resolved by a hand-off.")
    confirmed_role, _ = _coerce_role(confirmed_role)
    if confirmed_role is TopologyRole.UNKNOWN:
        raise ValueError("A hand-off must confirm a concrete role.")
    if confirmed_role is not decision.pending_role:
        raise ValueError(
            f"This scope is pending toward {decision.pending_role.value}; a hand-off cannot "
            f"confirm {confirmed_role.value} instead."
        )
    return RoleDecision(
        transition=RoleTransition.ADOPTED,
        role=confirmed_role,
        previous_role=decision.previous_role,
    )

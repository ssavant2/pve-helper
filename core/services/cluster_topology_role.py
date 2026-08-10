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
    #: Incomplete observation. Previous-good role is preserved and stale; no
    #: conclusion is drawn in either direction.
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class MembershipObservation:
    """One normalized membership read, independent of the provider's wire shape.

    ``complete`` is the coverage verdict the 5a1B reader supplies, not something
    inferred here. ``has_cluster_row`` is meaningful only when ``complete``.
    """

    complete: bool
    has_cluster_row: bool
    member_count: int = 0
    quorate: bool = False


@dataclass(frozen=True)
class RoleDecision:
    """The state machine's verdict for one observation against one stored role."""

    transition: RoleTransition
    #: The role to publish. On an incomplete observation this is the stored role,
    #: unchanged -- including ``UNKNOWN``.
    role: TopologyRole
    previous_role: TopologyRole
    #: True only while the scope waits for the explicit two-identity hand-off.
    transition_pending: bool
    #: A stable, operator-facing reason, or "" when nothing needs explaining.
    reason: str = ""

    @property
    def blocks_provider_work(self) -> bool:
        """Whether new provider work must be refused for this scope.

        Only a pending role transition blocks. An incomplete observation does
        not: unreachability is an ordinary degraded state that the projection
        already renders as stale, and blocking on it would make a flapping
        network indistinguishable from a changed identity.
        """
        return self.transition_pending


def classify_role(observation: MembershipObservation) -> TopologyRole:
    """Return the role a single observation proves, or ``UNKNOWN``.

    The one rule: a complete observation with no corosync cluster row is
    standalone, with one is corosync. Member count and quorum are deliberately
    not inputs -- a one-node corosync cluster is a cluster, and a non-quorate
    multi-node cluster has not become a standalone host.
    """
    if not observation.complete:
        return TopologyRole.UNKNOWN
    return TopologyRole.COROSYNC if observation.has_cluster_row else TopologyRole.STANDALONE


def evaluate_role_transition(
    stored_role: TopologyRole,
    observation: MembershipObservation,
    *,
    transition_already_pending: bool = False,
) -> RoleDecision:
    """Decide what one observation means for a scope's recorded role.

    ``transition_already_pending`` carries a hand-off that a previous generation
    opened. It is sticky on purpose: only the explicit operator hand-off clears
    it (see :func:`resolve_transition`), never a later observation that happens
    to agree with the new role, and never one that cannot be completed.
    """
    stored_role = TopologyRole(stored_role)
    observed = classify_role(observation)

    if observed is TopologyRole.UNKNOWN:
        return RoleDecision(
            transition=RoleTransition.INDETERMINATE,
            role=stored_role,
            previous_role=stored_role,
            transition_pending=transition_already_pending,
            reason="Membership coverage is incomplete; the previous role is preserved as stale.",
        )

    if transition_already_pending:
        # A pending hand-off outranks a fresh reading. Publishing the new role
        # here would silently complete the transition the operator has not yet
        # confirmed, which is the whole thing this state blocks.
        return RoleDecision(
            transition=RoleTransition.TRANSITION_PENDING,
            role=stored_role,
            previous_role=stored_role,
            transition_pending=True,
            reason="A topology role transition is awaiting an explicit operator hand-off.",
        )

    if stored_role is TopologyRole.UNKNOWN:
        return RoleDecision(
            transition=RoleTransition.ADOPTED,
            role=observed,
            previous_role=stored_role,
            transition_pending=False,
        )

    if observed is stored_role:
        return RoleDecision(
            transition=RoleTransition.STABLE,
            role=observed,
            previous_role=stored_role,
            transition_pending=False,
        )

    return RoleDecision(
        transition=RoleTransition.TRANSITION_PENDING,
        role=stored_role,
        previous_role=stored_role,
        transition_pending=True,
        reason=(
            f"This scope was observed as {observed.value} but is registered as "
            f"{stored_role.value}. Provider work is blocked until the identity hand-off is confirmed."
        ),
    )


def resolve_transition(decision: RoleDecision, *, confirmed_role: TopologyRole) -> RoleDecision:
    """Apply an operator-confirmed hand-off to a pending decision.

    The caller has already performed the two-identity transaction that
    ``docs/cluster-retire.local.md`` assigns to 5a0B -- verify the candidate,
    lock both identities, revalidate digests, transfer or release the endpoint,
    retire the old scope, rebind storage from an explicit list. This only moves
    the role, and only for a decision that was actually pending: calling it on a
    stable decision is a programming error, not a shortcut for editing the role.
    """
    if not decision.transition_pending:
        raise ValueError("Only a transition-pending decision can be resolved by a hand-off.")
    confirmed_role = TopologyRole(confirmed_role)
    if confirmed_role is TopologyRole.UNKNOWN:
        raise ValueError("A hand-off must confirm a concrete role.")
    return RoleDecision(
        transition=RoleTransition.ADOPTED,
        role=confirmed_role,
        previous_role=decision.previous_role,
        transition_pending=False,
        reason="",
    )

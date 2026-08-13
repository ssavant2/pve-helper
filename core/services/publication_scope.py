"""The single owner of *which nodes of a cluster pve-helper may publish*.

Every publication decision in the application resolves through this module. The
alternative — a version check and an enrollment lookup repeated at each reconciler,
each read seam and each target list — is a branch with no owner, and the one place
that forgot it would silently publish a hidden node.

Two sets, not one:

``managed``
    May be published. Ordinary guest surfaces, placement/migration/clone/replication
    targets, and everything else an operator drives.

``safety``
    ``managed`` plus ``safety_only``. Contributes *evidence* — disk references, live
    guest references behind the storage risk gate — and never inventory. Hiding a node
    is a statement about what pve-helper manages, never a statement that its guests
    stopped existing, so a hidden node's disk must still block a destructive file
    action.

**Un-published rows are marked, never deleted.** :class:`CurrentGuestInventory` is
read as safety evidence by ``file_actions`` and by volume-usage/orphan classification
in ``storage_catalog``. Deleting a hidden node's rows would make ``safety_only`` mean
"invisible to the safety gate" — the inverse of the mode — and would turn a live disk
into an orphan candidate.

Scan evidence (``ProxmoxInventory`` and the ``referenced_volids``/``template_vmids``
sets derived from it) is deliberately **not** filtered here. ``partial_scan``
rebuilds those references from stored ``ProxmoxInventory`` rows, so filtering them
would make the full scan and the partial directory refresh reach different verdicts
about the same file. Storage-side enrollment filtering is owned by 5a4A.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.utils import timezone

from core.models import ClusterNodeEnrollment, CurrentGuestInventory
from core.services.public_errors import PublicMessageError

#: The contract version at which enrollment starts deciding publication. Below it a
#: cluster publishes exactly as it did before enrollment existed.
ENROLLMENT_CONTRACT_ACTIVE = 1


class UnpublishedTargetError(PublicMessageError, ValueError):
    """A write named a node this cluster does not publish. Safe to show an operator."""


@dataclass(frozen=True)
class PublicationScope:
    """What one cluster may publish, resolved once and reused for a whole pass."""

    cluster_id: int
    contract_version: int
    generation: int
    managed_nodes: frozenset[str]
    safety_nodes: frozenset[str]

    @property
    def filtering(self) -> bool:
        return self.contract_version >= ENROLLMENT_CONTRACT_ACTIVE

    def publishes(self, node: str) -> bool:
        """Whether this cluster may publish inventory for ``node``.

        An activated cluster is authoritative even when its enrollment set is empty:
        empty publishes *nothing*. That is a visible, recoverable state the operator
        can fix in Connections; the alternative reading — empty means everything —
        would make a mis-activation publish exactly what it was meant to hide.
        """

        if not self.filtering:
            return True
        return node in self.managed_nodes

    def observes(self, node: str) -> bool:
        """Whether ``node`` still contributes safety evidence. Hiding never removes it."""

        if not self.filtering:
            return True
        return node in self.safety_nodes


def publication_scope(cluster) -> PublicationScope:
    """Resolve one cluster's publication policy in a single query.

    Called once per reconcile pass, not once per guest: the answer does not vary
    within a pass, and re-reading it per row would put an O(guests) query load on the
    worker for a value that cannot change under the pass's transaction.
    """

    managed: set[str] = set()
    safety: set[str] = set()
    for node_name, mode in ClusterNodeEnrollment.objects.filter(cluster=cluster).values_list("node_name", "mode"):
        safety.add(node_name)
        if mode == ClusterNodeEnrollment.Mode.MANAGED:
            managed.add(node_name)
    return PublicationScope(
        cluster_id=cluster.pk,
        contract_version=cluster.enrollment_contract_version,
        generation=cluster.enrollment_generation,
        managed_nodes=frozenset(managed),
        safety_nodes=frozenset(safety),
    )


def publication_scopes(clusters) -> dict[int, PublicationScope]:
    """Resolve many clusters' policies in **one** query, keyed by cluster id.

    The sidebar tree renders on every HTML response and must be flat in both node
    and cluster count. Calling :func:`publication_scope` in a loop there is a query
    per cluster on a shared surface — the quadratic-at-real-scale shape the shell
    query budget exists to catch. Same rule, same owner, one round trip.
    """

    by_cluster = {cluster.pk: (set(), set()) for cluster in clusters}
    rows = ClusterNodeEnrollment.objects.filter(cluster_id__in=list(by_cluster)).values_list(
        "cluster_id", "node_name", "mode"
    )
    for cluster_id, node_name, mode in rows:
        managed, safety = by_cluster[cluster_id]
        safety.add(node_name)
        if mode == ClusterNodeEnrollment.Mode.MANAGED:
            managed.add(node_name)
    return {
        cluster.pk: PublicationScope(
            cluster_id=cluster.pk,
            contract_version=cluster.enrollment_contract_version,
            generation=cluster.enrollment_generation,
            managed_nodes=frozenset(by_cluster[cluster.pk][0]),
            safety_nodes=frozenset(by_cluster[cluster.pk][1]),
        )
        for cluster in clusters
    }


def published_node_names(cluster, client, *, fallback: str = "") -> list[str]:
    """This cluster's live node list, restricted to what it may publish.

    The raw ``node_names`` read lives here rather than at the call sites, so that
    ``RAW_MEMBERSHIP_READ_ALLOWLIST`` can strike a module the moment it migrates
    instead of trusting that whoever kept the raw call also remembered the filter.

    Provider errors propagate: a target list that silently became empty because
    Proxmox did not answer is indistinguishable from one that is empty because
    nothing is enrolled, and the two require opposite responses from the operator.
    """

    scope = publication_scope(cluster)
    return [node for node in client.node_names(fallback=fallback) if scope.publishes(node)]


def publishes_node(cluster, node: str) -> bool:
    """Whether one write may name ``node``. The predicate behind every preflight."""

    return publication_scope(cluster).publishes(node)


def unpublished_target_message(node: str, *, what: str = "target") -> str:
    """The operator-facing refusal, composed from named values only.

    Deliberately not built by stringifying an exception. These messages reach a JSON
    bulk-action response, and the boundary rule is that provider/Python exception text
    never crosses it — a rule worth keeping mechanical rather than case by case.
    """

    return f"Node '{node}' is not an enrolled {what} on this connection."


def refuse_unpublished_target(cluster, node: str, *, what: str = "target") -> None:
    """Fail a write closed when its node is not one this cluster publishes.

    The filtered target lists above are a rendering-time courtesy. This is the
    enforcement: a form rendered before a node was hidden, a replayed request or a
    hand-built POST must not reach a node the operator has taken out of scope.
    """

    if publishes_node(cluster, node):
        return
    raise UnpublishedTargetError(unpublished_target_message(node, what=what))


def apply_publication_scope(cluster, *, scope: PublicationScope | None = None) -> int:
    """Re-stamp every stored row of one cluster against the current scope.

    An enrollment change must take effect when the operator makes it, not whenever a
    reconcile next happens to run. Waiting for the next pass would leave a just-hidden
    node's guests on screen for a cadence, and leave them there indefinitely while the
    cluster is unreachable.

    Two set-based updates and no provider call. Returns the number of rows re-stamped.
    """

    if scope is None:
        scope = publication_scope(cluster)
    now = timezone.now()

    def _restamp(rows, *, published: bool) -> int:
        # Only rows whose stamp is actually wrong are written, so an enrollment change
        # on one node does not rewrite every guest of the cluster.
        return rows.exclude(published=published, based_on_enrollment_generation=scope.generation).update(
            published=published, based_on_enrollment_generation=scope.generation, updated_at=now
        )

    rows = CurrentGuestInventory.objects.filter(cluster=cluster)
    if not scope.filtering:
        return _restamp(rows, published=True)

    # `node__in=[]` matches nothing and its complement matches everything, so an empty
    # managed set un-publishes the whole cluster. That is the intended reading.
    managed = sorted(scope.managed_nodes)
    return _restamp(rows.filter(node__in=managed), published=True) + _restamp(
        rows.exclude(node__in=managed), published=False
    )

"""Composition for the workspace Datastores tab (phase 5a4A).

Presentation-free, and **it originates no read of its own**. Every value here comes
from the storage catalog's published projection — `ClusterStorage`, its
`ClusterStorageNodeState` rows and the cluster's `StorageCatalogState` — which
`docs/storage-model.local.md` owns. Module 5 adds per-node reachability to that
surface and nothing else; there is no parallel storage read model here, and this
module issues zero provider calls on render.

Three decisions the shape below encodes, each of which was a choice:

**Reachability publishes at (datastore, node).** That is the grain a decision is
actually made at — one node of three failing to mount a shared export is not the
datastore failing — and the rows already exist at that grain, so this is a read,
not a projection. ``metadata_complete`` is the *cluster's* telemetry and is carried
alongside as a banner value: no panel may blank on it, because a whole tab going
empty on a cluster-wide flag is how one node's silence becomes "no datastores".

**Currency is generation equality, not age.** A row is current when its
``observed_metadata_generation`` matches the catalog state's; the timestamp is
displayed and never decides anything. There is no staleness threshold in this
module because inventing one would be inventing a fact — the projection already
knows exactly which rows the last complete pass wrote.

**A node-state row's generation means nothing on its own.** A tombstoned datastore
leaves node rows behind whose generation is simply the last one that saw them, and
reading those as "stale" rather than "gone" is how a deleted datastore comes back as
a warning. The tab excludes tombstoned definitions in its query, so currency is only
ever asked of a published one — one rule in one place rather than two that could
disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from core.models import ClusterStorage
from core.services.datastore_nav import datastore_url
from core.services.publication_scope import PublicationScope, publication_scope
from core.services.storage_catalog import catalog_state

#: What one node reports about one datastore. Ordered from best to worst so a
#: caller summarising several instances can take the minimum.
ATTACHED = "attached"
INACTIVE = "inactive"
ABSENT = "absent"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class NodeReachability:
    """One `(datastore, node)` row, as a state rather than four booleans.

    The distinction the whole tab rests on is between the last two. ``absent`` is a
    node that answered and does not have it; ``unknown`` is a node that did not
    answer, or one this connection is not allowed to ask. They look identical in the
    database — both leave ``present`` false — and they are opposite statements to an
    operator, so neither may ever render as "not configured".
    """

    node: str
    state: str
    current: bool
    total_bytes: int | None
    used_bytes: int | None
    available_bytes: int | None
    last_seen_at: datetime | None

    @property
    def used_pct(self) -> int | None:
        if not self.total_bytes or self.used_bytes is None:
            return None
        return round(self.used_bytes / self.total_bytes * 100)


@dataclass(frozen=True)
class DatastoreRow:
    definition: ClusterStorage
    #: Empty for a shared datastore, which is one cluster-wide object. A node-local
    #: one carries its node because `local` is a different disk on every node.
    scope_node: str
    url: str
    nodes: tuple[NodeReachability, ...]
    #: The definition's `nodes=` restriction, verbatim. Configuration the operator
    #: set in Proxmox, shown so the tab explains why a member is missing from the
    #: list — it creates no node page and is never an enrollment statement.
    restricted_to: tuple[str, ...]

    @property
    def storage_id(self) -> str:
        return self.definition.storage_id

    @property
    def shared(self) -> bool:
        return bool(self.definition.shared)

    @property
    def content(self) -> tuple[str, ...]:
        return tuple(self.definition.content or ())

    @property
    def instance(self) -> NodeReachability | None:
        """The instance capacity is read from: the first attached one, else the first.

        A shared datastore is one backend behind every node that sees it, so any
        attached instance answers for its capacity. Preferring an attached one keeps
        a single unreachable node from blanking a figure every other node can see.
        """

        return next((row for row in self.nodes if row.state == ATTACHED), self.nodes[0] if self.nodes else None)

    @property
    def state(self) -> str:
        """The worst thing any published node says about it.

        Deliberately pessimistic: this column is what an operator scans down, and a
        datastore that is fine on two nodes and unknown on a third is not fine.
        """

        order = (ATTACHED, INACTIVE, ABSENT, UNKNOWN)
        return max((row.state for row in self.nodes), key=order.index, default=UNKNOWN)

    @property
    def current(self) -> bool:
        return all(row.current for row in self.nodes) and bool(self.nodes)

    @property
    def attached_nodes(self) -> tuple[str, ...]:
        return tuple(row.node for row in self.nodes if row.state == ATTACHED)


@dataclass(frozen=True)
class DatastorePanel:
    rows: tuple[DatastoreRow, ...]
    #: Cluster-grained telemetry, carried so the tab can say the last pass was
    #: incomplete. It qualifies the rows; it never replaces them.
    metadata_complete: bool
    metadata_refreshed_at: datetime | None
    #: Members this connection may not read, so the tab can say what it is not
    #: showing rather than quietly showing less.
    unread_nodes: tuple[str, ...]

    @property
    def shared_rows(self) -> tuple[DatastoreRow, ...]:
        return tuple(row for row in self.rows if row.shared)

    @property
    def local_rows(self) -> tuple[DatastoreRow, ...]:
        return tuple(row for row in self.rows if not row.shared)


def _reachability(state, *, generation) -> NodeReachability:
    if state.unreachable:
        label = UNKNOWN
    elif not state.present:
        label = ABSENT
    elif state.active and state.enabled:
        label = ATTACHED
    else:
        label = INACTIVE
    return NodeReachability(
        node=state.node,
        state=label,
        # Equality, not age. Only ever asked of a published definition: a
        # tombstone's leftover rows keep the last generation that saw them, and
        # calling those "stale" is how a deleted datastore returns as a warning.
        # The exclusion is in the query below, so there is no second rule here
        # that could disagree with it.
        current=bool(generation and state.observed_metadata_generation == generation),
        total_bytes=state.total_bytes,
        used_bytes=state.used_bytes,
        available_bytes=state.available_bytes,
        last_seen_at=state.last_seen_at,
    )


def datastore_panel(
    cluster,
    *,
    node: str = "",
    scope: PublicationScope | None = None,
    members: tuple[str, ...] = (),
) -> DatastorePanel:
    """Every datastore this cluster publishes, or the ones one node sees.

    Four queries, each bulk: the catalog state, the definitions, their node states
    prefetched in one round trip, and the publication boundary once for the page.
    Nothing here scales with node count or datastore count in queries.

    ``members`` is the cluster's discovered node names, passed in rather than read:
    the workspace shell around this tab has already loaded the membership projection,
    and re-reading it here would buy a fifth query for a value sitting in the caller.
    """

    if scope is None:
        scope = publication_scope(cluster)
    state = catalog_state(cluster)
    generation = state.metadata_generation
    definitions = (
        ClusterStorage.objects.filter(
            cluster=cluster,
            cluster__retired_at__isnull=True,
            unmanaged_at__isnull=True,
            present=True,
        )
        .select_related("cluster__storage_catalog_state")
        .prefetch_related("node_states")
        .order_by("storage_id")
    )

    rows: list[DatastoreRow] = []
    for definition in definitions:
        published = sorted(
            (row for row in definition.node_states.all() if scope.publishes(row.node)),
            key=lambda row: row.node,
        )
        if node:
            published = [row for row in published if row.node == node]
        if not published:
            continue
        restricted_to = tuple(definition.nodes or ())
        if definition.shared:
            rows.append(
                DatastoreRow(
                    definition=definition,
                    scope_node="",
                    url=datastore_url("core:api_storage_summary", cluster.key, definition.storage_id),
                    nodes=tuple(_reachability(row, generation=generation) for row in published),
                    restricted_to=restricted_to,
                )
            )
            continue
        # One row per node: the same storage id on two nodes is two different disks,
        # and collapsing them would put one node's capacity under the other's name.
        for row in published:
            rows.append(
                DatastoreRow(
                    definition=definition,
                    scope_node=row.node,
                    url=datastore_url("core:api_storage_summary", cluster.key, definition.storage_id, row.node),
                    nodes=(_reachability(row, generation=generation),),
                    restricted_to=restricted_to,
                )
            )

    return DatastorePanel(
        rows=tuple(sorted(rows, key=lambda row: (not row.shared, row.storage_id, row.scope_node))),
        metadata_complete=state.metadata_complete,
        metadata_refreshed_at=state.metadata_refreshed_at,
        unread_nodes=_unread_nodes(members, scope),
    )


def _unread_nodes(members: tuple[str, ...], scope: PublicationScope) -> tuple[str, ...]:
    """Discovered members this connection does not read, for the tab's own footnote.

    No contract-version branch: a legacy connection observes everything, so this is
    empty for one without a second rule that could disagree with `observes`.
    """

    return tuple(sorted(name for name in members if not scope.observes(name)))

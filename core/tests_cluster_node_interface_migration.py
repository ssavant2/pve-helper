"""Migration `0016` in both directions, against populated coverage. 5a4B-i.

The forward direction is not what this file exists for. A third domain inside an
existing OR arm makes `core_projection_coverage_scope` monotonically weaker, so
validating it against populated rows cannot fail and proves nothing.

The **reverse** is the direction that aborts. Re-adding the two-domain constraint
validates every existing coverage row, and a `node_network` row fails it -- which
would strand a rollback halfway, with the fix being hand-deleted rows on a live
cluster. `_drop_node_network_coverage` is what makes the rollback an ordinary
migration, and it shipped untested; this is the test that was owed.

It runs the real executor against the real schema, so it must leave the database
where it found it. Every path restores the full migration state in cleanup.
"""

from __future__ import annotations

from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.utils import IntegrityError
from django.test import TransactionTestCase
from django.utils import timezone

from core.models import (
    ClusterNodeInterface,
    ClusterProjectionCoverage,
    ProxmoxCluster,
)

_BEFORE = ("core", "0015_guest_publication_scope")


def _migrate(target):
    """Run the executor to `target`, or to the latest state when it is None."""
    executor = MigrationExecutor(connection)
    if target is None:
        targets = executor.loader.graph.leaf_nodes("core")
    else:
        targets = [target]
    executor.migrate(targets)


def _node_network_coverage_is_accepted() -> bool:
    """Does the schema, as it stands right now, accept a node_network row?

    Written through the ORM but read as a schema question: the check constraint is
    what answers it, and it is the only thing this file asserts about direction.
    """
    cluster = ProxmoxCluster.objects.get(key="migration-clusterc")
    try:
        with transaction.atomic():
            ClusterProjectionCoverage.objects.create(
                cluster=cluster,
                domain="node_network",
                node_name="pve202",
                generation=4,
                based_on_generation=2,
                complete=True,
            )
    except IntegrityError:
        return False
    return True


class NodeNetworkMigrationTests(TransactionTestCase):
    """0016 forward and back with `node_network` rows present."""

    def setUp(self):
        self.addCleanup(_migrate, None)
        self.cluster = ProxmoxCluster.objects.create(key="migration-clusterc", display_name="migration-clusterc")
        now = timezone.now()
        # Both shapes the reverse has to survive: the coverage row the narrower
        # constraint rejects, and an interface row on the table it drops.
        ClusterProjectionCoverage.objects.create(
            cluster=self.cluster,
            domain=ClusterProjectionCoverage.DOMAIN_NODE_NETWORK,
            node_name="pve201",
            generation=3,
            based_on_generation=2,
            complete=True,
            attempted_at=now,
            observed_at=now,
        )
        # A sibling domain that must survive the rollback untouched: the reverse
        # step is allowed to drop this phase's coverage and nothing else.
        ClusterProjectionCoverage.objects.create(
            cluster=self.cluster,
            domain=ClusterProjectionCoverage.DOMAIN_NODE_RUNTIME,
            node_name="pve201",
            generation=3,
            based_on_generation=2,
            complete=True,
            attempted_at=now,
            observed_at=now,
        )
        ClusterNodeInterface.objects.create(
            cluster=self.cluster,
            node_name="pve201",
            iface="vmbr0",
            interface_type="bridge",
            attachable=True,
            observed_generation=3,
        )

    def test_the_rollback_completes_with_node_network_coverage_present(self):
        # Without the RunPython reverse this raises: PostgreSQL validates the
        # re-added constraint against the row created in setUp.
        _migrate(_BEFORE)

        self.assertFalse(_node_network_coverage_is_accepted())
        self.assertFalse(
            ClusterProjectionCoverage.objects.filter(domain="node_network").exists(),
            "the reverse step must remove exactly the rows the narrower constraint rejects",
        )

    def test_the_rollback_keeps_the_sibling_node_runtime_coverage(self):
        _migrate(_BEFORE)

        self.assertTrue(ClusterProjectionCoverage.objects.filter(domain="node_runtime", node_name="pve201").exists())

    def test_the_forward_direction_readmits_the_domain_after_a_rollback(self):
        _migrate(_BEFORE)
        _migrate(None)

        self.assertTrue(_node_network_coverage_is_accepted())
        # The table the reversed CreateModel dropped is back, and empty: coverage
        # is a statement about the last refresh, so a later sweep republishes it.
        self.assertEqual(ClusterNodeInterface.objects.count(), 0)

    def test_the_forward_direction_runs_over_populated_sibling_coverage(self):
        # The forward direction can only be staged from `0015`, where this domain's
        # rows cannot exist -- so what it actually has to survive is the *other*
        # domains' populated rows, which the widened constraint revalidates.
        _migrate(_BEFORE)
        self.assertTrue(
            ClusterProjectionCoverage.objects.filter(domain="node_runtime", node_name="pve201").exists(),
            "the sibling row the forward migration must revalidate has to be there to prove anything",
        )

        _migrate(None)

        self.assertTrue(ClusterProjectionCoverage.objects.filter(domain="node_runtime", node_name="pve201").exists())
        self.assertTrue(_node_network_coverage_is_accepted())

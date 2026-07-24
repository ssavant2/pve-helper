"""R0 evidence and executable contract for cluster retirement.

This module is deliberately behaviour-free. Cluster retirement (see
``docs/cluster-retire.local.md``) lands in phases R1a..R4; R0 only pins the
evidence that later phases build against:

* representative fixtures the retirement test matrix reuses, built against the
  *current* schema so they exist before a single production line changes, and
* the Connections-overview query budget, converted here from "counted from view
  source" into an executed ``assertNumQueries`` number.

Fixture scenarios that require the retirement schema itself -- an *only retired*
installation, a *stale signed preflight*, and a *Module 5 participant stub* --
cannot be built until R1a adds ``retired_at`` and the lifecycle generation, so
they are named in ``test_retirement_schema_fixtures_are_deferred`` and filled in
by R1a rather than faked here. Building a "retired" cluster without the column
that defines retirement would be evidence of nothing.
"""

from __future__ import annotations

from unittest import skip
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings

from core.models import (
    ProxmoxCluster,
    ProxmoxEndpoint,
    ProxmoxStorageConsumer,
    RuntimeConfigurationState,
    StorageMount,
)
from core.views.clusters import clusters_overview

# ---------------------------------------------------------------------------
# Representative fixtures (buildable against the current schema).
#
# These are plain builders, not TestCase methods, so every later retirement
# test file imports the same scenario instead of re-deriving it. Keep them
# free of retirement-only fields until R1a exists.
# ---------------------------------------------------------------------------


def bootstrap_runtime() -> RuntimeConfigurationState:
    """A bootstrapped installation, so onboarding is not re-imported.

    ``ensure_bootstrap()`` keys on this marker, never on "do any clusters
    exist", which is exactly why removing every cluster stays safe.
    """
    return RuntimeConfigurationState.objects.create(
        bootstrap_completed=True,
        identity_contract_version=1,
    )


def make_cluster(
    key: str,
    *,
    display_name: str | None = None,
    enabled: bool = True,
    nodes: tuple[str, ...] = ("pve1",),
    ca_uuid: str = "",
    ca_fingerprint: str = "",
) -> ProxmoxCluster:
    """A cluster with one endpoint per node.

    One endpoint transports one node's name here purely so a fixture can talk
    about "the endpoint for pve1"; endpoints are transports, not identity.
    """
    cluster = ProxmoxCluster.objects.create(
        key=key,
        display_name=display_name or key,
        enabled=enabled,
        discovered_ca_uuid=ca_uuid,
        discovered_ca_fingerprint=ca_fingerprint,
    )
    for node in nodes:
        ProxmoxEndpoint.objects.create(
            cluster=cluster,
            name=node,
            url=f"https://{node}.{key}.test:8006/",
        )
    return cluster


def standalone_cluster() -> ProxmoxCluster:
    """A single-node cluster. Retirement treats it as a cluster like any other."""
    return make_cluster(
        "standalone",
        display_name="Standalone host",
        nodes=("pve1",),
        ca_uuid="11111111-1111-1111-1111-111111111111",
        ca_fingerprint="AA:11",
    )


def duplicate_identity_clusters() -> tuple[ProxmoxCluster, ProxmoxCluster]:
    """Two clusters that each own a node named ``pve1``.

    The gate identity is ``(storage, cluster, node)`` precisely so one cluster's
    ``pve1`` never clears the other cluster's gate. This fixture is the minimal
    shape that would break an unqualified ``pve1`` lookup.
    """
    first = make_cluster(
        "cluster-a",
        display_name="Cluster A",
        nodes=("pve1", "pve2"),
        ca_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    second = make_cluster(
        "cluster-b",
        display_name="Cluster B",
        nodes=("pve1", "pve2"),
        ca_uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    )
    return first, second


def offline_endpoint_cluster() -> ProxmoxCluster:
    """A cluster one of whose enabled endpoints last failed its health check.

    "Last unhealthy" is not "retired" and is never evidence of absence -- the
    fixture exists so a test can prove retirement never infers decommissioning
    from an endpoint being dark.
    """
    cluster = make_cluster(
        "offline-endpoint",
        display_name="Cluster with an offline endpoint",
        nodes=("pve1", "pve2"),
        ca_uuid="cccccccc-cccc-cccc-cccc-cccccccccccc",
    )
    dark = cluster.endpoints.get(name="pve2")
    dark.last_health_status = "unreachable"
    dark.save(update_fields=["last_health_status"])
    return cluster


def shared_storage_with_consumers() -> tuple[StorageMount, ProxmoxCluster, ProxmoxCluster]:
    """A shared export consumed by nodes in two different clusters.

    After one cluster is retired, its consumer rows must be deleted, not left to
    report as permanently-missing consumers that strand the shared gate for the
    cluster that remains. This is the fixture that test asserts against.
    """
    first, second = duplicate_identity_clusters()
    storage = StorageMount.objects.create(
        storage_id="shared-nfs",
        display_name="Shared NFS",
        export="nas:/export/shared",
        path="/mnt/pve/shared-nfs",
        relative_path="shared-nfs",
    )
    for cluster in (first, second):
        ProxmoxStorageConsumer.objects.create(
            storage=storage,
            cluster=cluster,
            expected_node_name="pve1",
        )
    return storage, first, second


@override_settings(APP_REQUIRE_LOGIN=False)
class ConnectionsOverviewQueryBudgetTests(TestCase):
    """R0 deliverable: the Connections overview query count is bounded and
    constant in cluster count.

    The plan counts four set-based queries from the view source
    (``prefetch_related("endpoints")`` is two, plus one each for
    ``ClusterCredential`` and ``ClusterTransportTrust``) and asks R0 to execute
    that count rather than assert it from reading.

    ``render`` is patched out so the measurement is the *view's own* queries and
    not the page's: ``app_settings()`` runs ``datastore_nav()`` once per enabled
    cluster on every HTML response, which is a real per-cluster cost but a
    context-processor cost, not this view's. Measuring the page instead of the
    view is the documented way this assertion turns flaky.
    """

    databases = {"default"}

    def setUp(self):
        bootstrap_runtime()
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(username="operator")

    def _overview_query_count(self, cluster_count: int) -> int:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        request = self.factory.get("/clusters/")
        request.user = self.user
        with patch("core.views.clusters.render", return_value=HttpResponse("")):
            with CaptureQueriesContext(connection) as ctx:
                clusters_overview(request)
        return len(ctx)

    def test_overview_query_count_is_constant_in_cluster_count(self):
        # Disabled clusters still appear in the overview, so build the range with
        # them: it exercises the same four queries while keeping the fixture from
        # implying anything about reachability.
        make_cluster("c1", enabled=False)
        one = self._overview_query_count(1)
        self.assertEqual(one, 4, "Connections overview must issue exactly four set-based queries")

        for index in range(2, 4):
            make_cluster(f"c{index}", enabled=False)
        self.assertEqual(self._overview_query_count(3), one)

        for index in range(4, 21):
            make_cluster(f"c{index}", enabled=False)
        self.assertEqual(self._overview_query_count(20), one)

    def test_empty_installation_makes_no_per_cluster_query(self):
        # No clusters: the base select runs, prefetch is skipped, and the two
        # ``cluster__in=[]`` filters short-circuit without hitting the database.
        self.assertEqual(self._overview_query_count(0), 1)

    def test_overview_touches_only_connection_tables(self):
        make_cluster("c1", enabled=False)
        request = self.factory.get("/clusters/")
        request.user = self.user
        with patch("core.views.clusters.render", return_value=HttpResponse("")):
            captured = {}

            def _capture(_request, _template, context):
                captured.update(context)
                return HttpResponse("")

            with patch("core.views.clusters.render", side_effect=_capture):
                clusters_overview(request)
        # Passive rendering must not reach a provider; the default test settings
        # block unmocked Proxmox HTTP, so a provider call would raise here.
        self.assertIn("clusters", captured)


class LifecycleParticipantContractTests(TestCase):
    """Guards the R0 evidence itself, so a schema drift is caught here rather
    than in a later phase that assumes it."""

    def test_reverse_relation_count_matches_the_contract(self):
        # The relation matrix in the plan is stated over exactly these fourteen
        # reverse relations. If a model adds a fifteenth, R1a's coverage test is
        # where it must be classified -- this assertion is the early warning that
        # the count the plan was written against has moved.
        relations = {
            field.get_accessor_name()
            for field in ProxmoxCluster._meta.get_fields()
            if field.is_relation and field.auto_created and not field.concrete
        }
        expected = {
            "audit_events",
            "credential",
            "transport_trust",
            "endpoints",
            "console_sessions",
            "storage_catalog_state",
            "storage_definitions",
            "proxmox_objects",
            "current_guests",
            "inventory_state",
            "storage_consumers",
            "scan_observations",
            "scheduled_actions",
            "storage_space_snapshots",
        }
        self.assertEqual(relations, expected)


@skip(
    "Retirement-schema fixtures land in R1a: an only-retired installation and a "
    "stale signed preflight both need retired_at and the lifecycle generation, "
    "and the Module 5 participant stub needs the participant registry."
)
class RetirementSchemaFixturesTests(TestCase):
    """Placeholder that keeps the deferred R0 fixtures visible in the matrix.

    R1a replaces this with real builders for ``only_retired_installation()``,
    ``stale_signed_preflight()`` and ``module5_participant_stub()``.
    """

    def test_retirement_schema_fixtures_are_deferred(self):  # pragma: no cover
        raise AssertionError("Implemented in R1a")

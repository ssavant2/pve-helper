"""Cluster-retirement evidence, executable contract, and R1a lifecycle scopes.

Cluster retirement (see ``docs/cluster-retire.local.md``) lands in phases
R1a..R4. R0 pinned the evidence later phases build against: representative
fixtures against the *then-current* schema, and the executed Connections-overview
query budget.

R1a adds the retirement schema itself, so the three fixtures R0 could only name --
an *only retired* installation, a *stale signed preflight*, and a *Module 5
participant stub* -- are now real (``only_retired_installation``,
``stale_signed_preflight``, ``module5_participant_stub``). R1a also adds coverage
for the read scopes (``core.services.cluster_scopes``), the lifecycle-constraint
guarantees, the reverse-relation classification registry, and the scan-retention
landmine (a retired cluster's inventory must survive pruning). None of it wires a
retire button or contacts a provider; that is R3.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from core.models import (
    ProxmoxCluster,
    ProxmoxEndpoint,
    ProxmoxInventory,
    ProxmoxStorageConsumer,
    RuntimeConfigurationState,
    ScanRun,
    StorageMount,
)
from core.services.cluster_lifecycle_registry import (
    CLUSTER_REVERSE_RELATIONS,
    FUTURE_PARTICIPANTS,
)
from core.services.cluster_scopes import (
    has_historical_clusters,
    has_managed_clusters,
    historical_clusters,
    managed_clusters,
    provider_acquirable_clusters,
)
from core.services.scan_retention import prune_scan_history
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


def retire_cluster(
    cluster: ProxmoxCluster,
    *,
    mode: str = ProxmoxCluster.RetirementMode.VERIFIED,
    reason: str = "",
    by=None,
) -> ProxmoxCluster:
    """Move a cluster to the retired tombstone state at the row level.

    R1a has the schema but not the retirement service (that lands in R3), so this
    performs exactly the row transition the finalizer will: disable, stamp
    ``retired_at``/mode, copy the pinned CA identity into the non-unique tombstone
    columns and clear the live ones, and bump ``lifecycle_generation``. It is what
    the retirement-schema fixtures and scope tests build a retired row from.
    """
    cluster.retired_ca_uuid = cluster.discovered_ca_uuid
    cluster.retired_ca_fingerprint = cluster.discovered_ca_fingerprint
    cluster.discovered_ca_uuid = ""
    cluster.discovered_ca_fingerprint = ""
    cluster.enabled = False
    cluster.retired_at = timezone.now()
    cluster.retirement_mode = mode
    cluster.retirement_reason = reason
    cluster.retired_by = by
    cluster.lifecycle_generation += 1
    cluster.save()
    return cluster


def only_retired_installation() -> ProxmoxCluster:
    """A bootstrapped installation whose every cluster is retired.

    The R0 fixture that could not exist before ``retired_at``. It must read as
    "history to show, nothing operational": ``has_historical_clusters()`` is true,
    ``has_managed_clusters()`` is false, and bootstrap is not re-imported.
    """
    bootstrap_runtime()
    return retire_cluster(standalone_cluster(), reason="decommissioned")


def stale_signed_preflight(cluster: ProxmoxCluster) -> tuple[str, dict]:
    """A retirement preflight token whose bound lifecycle generation is now stale.

    R3 issues this token with ``django.core.signing`` and rejects it at the final
    transaction when any bound value moved. Here the token is minted against the
    cluster's current generation and the cluster is then bumped, so the token is
    exactly the "changed bound value" case R3's reload must refuse. Only the schema
    is needed to build it, which is why it defers to R1a rather than R3.
    """
    payload = {
        "cluster_pk": cluster.pk,
        "cluster_key": cluster.key,
        "mode": ProxmoxCluster.RetirementMode.VERIFIED,
        "lifecycle_generation": cluster.lifecycle_generation,
    }
    token = signing.dumps(payload, salt="cluster-retirement-preflight")
    cluster.lifecycle_generation += 1
    cluster.save(update_fields=["lifecycle_generation", "updated_at"])
    return token, payload


def module5_participant_stub():
    """The reserved future lifecycle participant Module 5 will introduce.

    It has no model yet, so it cannot be a reverse relation to introspect; the
    registry reserves it so "a status not listed blocks both modes" stays honest
    the day Module 5's standalone-host relation lands.
    """
    return FUTURE_PARTICIPANTS[0]


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

    @staticmethod
    def _reverse_relations() -> set[str]:
        return {
            field.get_accessor_name()
            for field in ProxmoxCluster._meta.get_fields()
            if field.is_relation and field.auto_created and not field.concrete
        }

    def test_reverse_relation_count_matches_the_contract(self):
        # The relation matrix in the plan is stated over exactly these fourteen
        # reverse relations. If a model adds a fifteenth, the coverage test below
        # is where it must be classified -- this assertion is the early warning
        # that the count the plan was written against has moved.
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
        self.assertEqual(self._reverse_relations(), expected)

    def test_every_reverse_relation_is_classified_by_the_registry(self):
        """The exhaustive coverage the R0 contract owes: no reverse relation may go
        unclassified.

        A retirement finalizer or the hard-delete eligibility check that iterates
        the registry must see every relation, so a future relation added without a
        row in ``CLUSTER_REVERSE_RELATIONS`` is a silent gap where a durable row is
        neither preserved, removed nor treated as a blocker. This asserts the two
        sets are equal in both directions -- an unclassified relation and a stale
        registry entry are both failures.
        """
        relations = self._reverse_relations()
        classified = set(CLUSTER_REVERSE_RELATIONS)

        self.assertEqual(
            sorted(relations - classified),
            [],
            "Reverse relations with no classification in CLUSTER_REVERSE_RELATIONS; "
            "a retirement finalizer would ignore them.",
        )
        self.assertEqual(
            sorted(classified - relations),
            [],
            "CLUSTER_REVERSE_RELATIONS names relations that no longer exist on the model.",
        )
        # The registry keys must be the accessor names, or an iterator that resolves
        # them against the model silently skips the mislabelled rows.
        for accessor, classification in CLUSTER_REVERSE_RELATIONS.items():
            self.assertEqual(accessor, classification.accessor)


class ClusterScopeResolverTests(TestCase):
    """The three scopes must mean three different things, and R1a is where that is
    first testable because ``retired_at`` now exists to separate them."""

    def test_managed_excludes_retired_but_keeps_disabled_and_quarantined(self):
        live = make_cluster("live")
        disabled = make_cluster("disabled", enabled=False)
        quarantined = make_cluster("quarantined")
        quarantined.ingestion_quarantined = True
        quarantined.save(update_fields=["ingestion_quarantined"])
        retired = retire_cluster(make_cluster("retired"))

        managed = set(managed_clusters())
        self.assertEqual(managed, {live, disabled, quarantined})
        self.assertNotIn(retired, managed)

    def test_historical_includes_retired_rows(self):
        live = make_cluster("live")
        retired = retire_cluster(make_cluster("retired"))
        self.assertEqual(set(historical_clusters()), {live, retired})

    def test_provider_acquirable_is_a_strict_subset_of_managed(self):
        # Enabled, not quarantined, but no credential/trust yet: managed but not
        # acquirable, so a mid-onboarding cluster is never contacted.
        make_cluster("onboarding")
        self.assertEqual(list(provider_acquirable_clusters()), [])
        self.assertEqual(managed_clusters().count(), 1)

    def test_only_retired_installation_reads_as_history_not_operational(self):
        only_retired_installation()
        self.assertTrue(has_historical_clusters())
        self.assertFalse(has_managed_clusters())

    def test_empty_installation_has_neither_scope_populated(self):
        self.assertFalse(has_historical_clusters())
        self.assertFalse(has_managed_clusters())


class RetirementConstraintTests(TestCase):
    """The database, not only the service, refuses an inconsistent lifecycle row."""

    def test_a_retired_row_must_be_disabled_and_carry_a_mode(self):
        cluster = make_cluster("c")
        cluster.retired_at = timezone.now()
        # enabled still True and no mode: violates retired_cluster_is_disabled_and_moded.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                cluster.save()

    def test_an_active_row_may_not_carry_retirement_metadata(self):
        cluster = make_cluster("c")
        cluster.retirement_mode = ProxmoxCluster.RetirementMode.FORCED
        cluster.retirement_reason = "left over"
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                cluster.save()

    def test_retirement_releases_the_ca_uuid_for_re_onboarding(self):
        # The pinned uniqueness holds among live rows...
        first = standalone_cluster()
        uuid = first.discovered_ca_uuid
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_cluster("clash", ca_uuid=uuid)
        # ...but once the owner is retired, the same physical identity can be
        # onboarded again under a new key. Narrowing to retired_at IS NULL is what
        # keeps a mistaken retirement from being a permanent lockout on the hardware.
        retire_cluster(first)
        reonboarded = make_cluster("reonboarded", ca_uuid=uuid)
        self.assertEqual(reonboarded.discovered_ca_uuid, uuid)


class RetirementSchemaFixturesTests(TestCase):
    """The three R0 fixtures that could not be built before ``retired_at`` existed,
    now real. Their placeholder skipped-test is gone."""

    def test_stale_signed_preflight_binds_a_now_outdated_generation(self):
        cluster = standalone_cluster()
        token, payload = stale_signed_preflight(cluster)

        recovered = signing.loads(token, salt="cluster-retirement-preflight")
        cluster.refresh_from_db()
        # The token still verifies cryptographically, but its bound generation no
        # longer matches the row -- exactly the changed-bound-value case R3 rejects.
        self.assertEqual(recovered["lifecycle_generation"], payload["lifecycle_generation"])
        self.assertNotEqual(recovered["lifecycle_generation"], cluster.lifecycle_generation)

    def test_module5_participant_stub_is_a_reserved_future_participant(self):
        stub = module5_participant_stub()
        self.assertEqual(stub.owning_module, "module5")
        # It is deliberately absent from the reverse-relation registry: no model, no
        # relation to introspect yet.
        self.assertNotIn(stub.name, CLUSTER_REVERSE_RELATIONS)


class ScanRetentionRetiredClusterTests(TestCase):
    """The named landmine: retention must keep a retired cluster's inventory.

    ``_current_proxmox_inventory_scan_ids()`` iterates the *historical* scope on
    purpose. Narrowing it to managed -- the edit the scope refactor invites -- would
    drop the retired cluster from the keep-set and delete the immutable inventory
    within SCAN_METADATA_RETENTION_DAYS. This retires a cluster, prunes, and proves
    the evidence survives.
    """

    def test_retired_cluster_inventory_survives_pruning(self):
        cluster = standalone_cluster()
        old = timezone.now() - timedelta(days=30)
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED, finished_at=old)
        inventory = ProxmoxInventory.objects.create(
            scan_run=scan,
            cluster=cluster,
            node="pve1",
            object_type=ProxmoxInventory.ObjectType.NODE,
        )

        retire_cluster(cluster)
        prune_scan_history()

        self.assertTrue(
            ProxmoxInventory.objects.filter(pk=inventory.pk).exists(),
            "A retired cluster's immutable inventory must survive retention; the scan "
            "id keeper must use the historical scope, not managed.",
        )

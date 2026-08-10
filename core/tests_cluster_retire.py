"""Cluster-retirement evidence, executable contract, and R1a lifecycle scopes.

Cluster retirement (see ``docs/cluster-retire.local.md``) lands in phases
R1a..R4. R0 pinned the evidence later phases build against: representative
fixtures against the *then-current* schema, and the executed Connections-overview
query budget.

R1a adds the retirement schema itself, so the fixtures R0 could only name -- an
*only retired* installation and a *stale signed preflight* -- are now real
(``only_retired_installation``, ``stale_signed_preflight``). R1a's third fixture,
a *Module 5 participant stub*, is gone: Module 5 phase 5a0B closed the reserved
``standalone_host`` slot it stood for. R1a also adds coverage
for the read scopes (``core.services.cluster_scopes``), the lifecycle-constraint
guarantees, the reverse-relation classification registry, and the scan-retention
landmine (a retired cluster's inventory must survive pruning). None of it wires a
retire button or contacts a provider; that is R3.
"""

from __future__ import annotations

import ast
import threading
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.db import IntegrityError, connection, transaction
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import (
    AuditEvent,
    ClusterCredential,
    ClusterStorage,
    ClusterTransportTrust,
    ConsoleSession,
    CurrentGuestInventoryState,
    ProxmoxCluster,
    ProxmoxEndpoint,
    ProxmoxInventory,
    ProxmoxStorageConsumer,
    RuntimeConfigurationState,
    ScanClusterObservation,
    ScanRun,
    ScheduledAction,
    StorageCatalogState,
    StorageMount,
)
from core.services.audit_events import (
    CLUSTER_CONFIGURATION_AUDIT_ACTIONS,
    CLUSTER_MACHINE_INITIATED_AUDIT_ACTIONS,
    CLUSTER_OPERATOR_INITIATED_AUDIT_ACTIONS,
    CLUSTER_PROVIDER_AUDIT_ACTIONS,
    MACHINE_INITIATED_FOOTPRINT_REASONS,
    record_audit_event,
)
from core.services.cluster_deletion import (
    ClusterDeletionBlocked,
    ClusterDeletionNotAllowed,
    delete_unused_cluster_connection,
)
from core.services.cluster_deletion_eligibility import (
    BLOCKER_FOOTPRINT,
    BLOCKER_RETIRED,
    unused_connection_deletion_eligibility,
)
from core.services.cluster_footprint import (
    FOOTPRINT_CONSOLE_SESSION,
    FOOTPRINT_GUEST_PROJECTION,
    FOOTPRINT_PROVIDER_OPERATION,
    FOOTPRINT_SCAN_OBSERVATION,
    RECONSTRUCTIBLE_FOOTPRINT_REASONS,
    stamp_operational_footprint,
)
from core.services.cluster_lifecycle_lock import (
    ClusterNotEnabledError,
    ClusterRetiredError,
    acquire_operable_cluster,
    cluster_lifecycle_lock,
)
from core.services.cluster_lifecycle_registry import CLUSTER_REVERSE_RELATIONS
from core.services.cluster_onboarding import disable_cluster
from core.services.cluster_scopes import (
    has_historical_clusters,
    has_managed_clusters,
    historical_clusters,
    managed_clusters,
    provider_acquirable_clusters,
)
from core.services.cluster_state_labels import cluster_degraded_label
from core.services.scan_retention import prune_scan_history
from core.tasks import (
    _reap_orphaned_cluster_operations,
    enqueue_scheduled_scan,
    reap_stale_guest_tasks,
    refresh_storage_catalog_for_cluster,
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

    def test_retired_archive_stays_inside_the_set_based_query_budget(self):
        make_cluster("managed", enabled=False)
        retire_cluster(make_cluster("retired", enabled=False))

        self.assertEqual(
            self._overview_query_count(2),
            4,
            "Splitting the prefetched historical rows must not add an archive query",
        )

    def test_only_retired_archive_skips_managed_credential_and_trust_queries(self):
        retire_cluster(make_cluster("retired", enabled=False))

        self.assertEqual(self._overview_query_count(1), 2)

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
        self.assertIn("retired_clusters", captured)


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


class ProviderAuditActionIntentCoverageTests(TestCase):
    """Every provider action must declare whether an operator or the app caused it.

    The sibling of the relation-registry coverage above, and it exists because the
    failure it guards is silent. A background job classified as operator work stamps
    ``provider_operation`` on its cluster, and since the periodic refreshes touch a
    connection within about a minute of it being added, ``Delete unused connection``
    dies for every connection in the installation while nothing looks broken. That is
    exactly what happened before ``cluster.inventory.bootstrap`` was given its own
    reconstructible reason, and prose in the retirement plan is not what should stand
    between the next background job and a repeat.
    """

    # Service modules that enqueue durable background work. Each one owns a module
    # level ``*_ACTION`` constant naming the audit action it records, and this list
    # is the ratchet: a new such module fails here until somebody adds it and, with
    # it, classifies its action.
    _BACKGROUND_OPERATION_SERVICES = {
        "cluster_inventory_bootstrap.py",
        "storage_catalog_refresh.py",
        "tag_actions.py",
        "tag_inventory_refresh.py",
    }
    # Predates the ``*_ACTION`` constant convention and spells "tag.bulk_operation"
    # as a literal in seven modules. Classified (operator-initiated) and covered by
    # the registry assertions below; exempt only from the constant-shape check.
    _WITHOUT_ACTION_CONSTANT = {"tag_actions.py"}

    def _service_root(self):
        return Path(__file__).resolve().parent / "services"

    def test_a_provider_action_is_operator_initiated_or_machine_initiated(self):
        overlap = CLUSTER_OPERATOR_INITIATED_AUDIT_ACTIONS & CLUSTER_MACHINE_INITIATED_AUDIT_ACTIONS
        self.assertEqual(
            sorted(overlap),
            [],
            "An action cannot be both operator- and machine-initiated; the footprint reason would depend on "
            "iteration order.",
        )
        self.assertEqual(
            CLUSTER_PROVIDER_AUDIT_ACTIONS,
            CLUSTER_OPERATOR_INITIATED_AUDIT_ACTIONS | CLUSTER_MACHINE_INITIATED_AUDIT_ACTIONS,
            "CLUSTER_PROVIDER_AUDIT_ACTIONS is derived from the two intents and must not be assigned directly.",
        )
        both = CLUSTER_PROVIDER_AUDIT_ACTIONS & CLUSTER_CONFIGURATION_AUDIT_ACTIONS
        self.assertEqual(
            sorted(both),
            [],
            "A provider action in the configuration allowlist stamps no footprint at all and blocks nothing.",
        )

    def test_a_machine_initiated_action_stamps_a_reconstructible_reason(self):
        """Otherwise the exception list is decorative: the reason still blocks."""
        for action, reason in sorted(MACHINE_INITIATED_FOOTPRINT_REASONS.items()):
            with self.subTest(action=action):
                self.assertIn(reason, RECONSTRUCTIBLE_FOOTPRINT_REASONS)

    def test_the_declared_intent_is_the_reason_actually_stamped(self):
        """The registries and `record_audit_event` must not be able to disagree."""
        for action in sorted(CLUSTER_OPERATOR_INITIATED_AUDIT_ACTIONS):
            with self.subTest(action=action, intent="operator"):
                cluster = make_cluster(f"op-{abs(hash(action)) % 100000}")
                record_audit_event(action=action, cluster=cluster, username="operator")
                cluster.refresh_from_db()
                self.assertEqual(cluster.operational_footprint_reason, FOOTPRINT_PROVIDER_OPERATION)
                self.assertFalse(unused_connection_deletion_eligibility(cluster).eligible)

        for action, reason in sorted(MACHINE_INITIATED_FOOTPRINT_REASONS.items()):
            with self.subTest(action=action, intent="machine"):
                cluster = make_cluster(f"machine-{abs(hash(action)) % 100000}")
                record_audit_event(action=action, cluster=cluster, username="system")
                cluster.refresh_from_db()
                self.assertEqual(cluster.operational_footprint_reason, reason)
                self.assertTrue(
                    unused_connection_deletion_eligibility(cluster).eligible,
                    "Machine-initiated provider work must not block deleting a connection nobody used.",
                )

    def test_every_background_operation_service_is_accounted_for(self):
        found = {
            path.name
            for path in sorted(self._service_root().glob("*.py"))
            if "async_task(" in path.read_text(encoding="utf-8")
        }
        self.assertEqual(
            sorted(found - self._BACKGROUND_OPERATION_SERVICES),
            [],
            "A service enqueues background work without declaring its audit action's intent. Add it to "
            "_BACKGROUND_OPERATION_SERVICES and classify its action as operator- or machine-initiated.",
        )
        self.assertEqual(
            sorted(self._BACKGROUND_OPERATION_SERVICES - found),
            [],
            "_BACKGROUND_OPERATION_SERVICES names a service that no longer enqueues anything.",
        )

    def test_every_background_operation_action_constant_is_classified(self):
        classified = CLUSTER_PROVIDER_AUDIT_ACTIONS | CLUSTER_CONFIGURATION_AUDIT_ACTIONS
        unclassified = []
        missing_constant = []
        for name in sorted(self._BACKGROUND_OPERATION_SERVICES):
            path = self._service_root() / name
            module = ast.parse(path.read_text(encoding="utf-8"))
            constants = {
                target.id: node.value.value
                for node in module.body
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                for target in node.targets
                if isinstance(target, ast.Name) and target.id.endswith("_ACTION")
            }
            if not constants and name not in self._WITHOUT_ACTION_CONSTANT:
                missing_constant.append(name)
            unclassified.extend(
                f"{name}:{constant} = {value!r}" for constant, value in constants.items() if value not in classified
            )

        self.assertEqual(
            missing_constant,
            [],
            "A background-operation service must name its audit action in a module-level *_ACTION constant, "
            "so the classification registries have something to be checked against.",
        )
        self.assertEqual(
            unclassified,
            [],
            "Background-operation actions absent from every classification registry. Retirement preflight would "
            "not see them and unused-connection deletion would treat them as an unknown operator footprint.",
        )


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

    def test_no_standalone_host_participant_is_reserved(self):
        """5a0B closes R1a's reserved ``standalone_host`` slot.

        The reservation assumed a standalone installation would get its own
        cluster relation. Module 5 settled that it does not: standalone is a
        ``ProxmoxCluster`` domain whose node is an ordinary ``NodeRef``, so the
        slot could only ever be closed, never filled. This asserts the promise is
        gone rather than merely unused -- a reserved name a reader must verify
        against source is exactly the stale-evidence shape Module 5's reviews kept
        catching.
        """
        from core.services import cluster_lifecycle_registry

        # Checked by shape, not by one attribute name: re-adding the mechanism as
        # RESERVED_PARTICIPANTS, or restoring the dataclass alone, is the same
        # promise under a different spelling and must fail here too.
        reservations = [
            name
            for name in dir(cluster_lifecycle_registry)
            if not name.startswith("_") and ("FUTURE" in name.upper() or "RESERV" in name.upper())
        ]
        self.assertEqual(
            reservations,
            [],
            "The future-participant reservation is closed. A new relation is caught by "
            "the reverse-relation exhaustiveness test below, not by holding a name open.",
        )
        self.assertNotIn("standalone_host", CLUSTER_REVERSE_RELATIONS)
        source = Path(cluster_lifecycle_registry.__file__).read_text()
        self.assertNotIn("class FutureParticipant", source)


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


# ---------------------------------------------------------------------------
# R1b: the acquisition barrier.
# ---------------------------------------------------------------------------


def _make_stale_audit(*, action, outcome, cluster=None, details=None, minutes_ago=30):
    """A queued/running audit row older than the reaper threshold.

    ``timestamp`` is ``auto_now_add`` so it cannot be set on create; the row is
    aged with a follow-up update, which is exactly how a genuinely stale row looks.
    """
    event = AuditEvent.objects.create(action=action, outcome=outcome, cluster=cluster, details=details or {})
    AuditEvent.objects.filter(pk=event.pk).update(timestamp=timezone.now() - timedelta(minutes=minutes_ago))
    event.refresh_from_db()
    return event


class AcquireOperableClusterTests(TestCase):
    """The acquisition half of the barrier: retired/disabled state becomes a terminal
    result under the lock, never a provider round-trip against a gone cluster."""

    def test_returns_the_row_locked_cluster_when_operable(self):
        cluster = make_cluster("ok")
        with transaction.atomic():
            locked = acquire_operable_cluster(cluster)
        self.assertEqual(locked.pk, cluster.pk)

    def test_raises_for_a_retired_cluster(self):
        cluster = retire_cluster(make_cluster("gone"))
        with self.assertRaises(ClusterRetiredError):
            with transaction.atomic():
                acquire_operable_cluster(cluster)

    def test_raises_for_a_disabled_cluster_when_enabled_required(self):
        cluster = make_cluster("off", enabled=False)
        with self.assertRaises(ClusterNotEnabledError):
            with transaction.atomic():
                acquire_operable_cluster(cluster)

    def test_a_disabled_cluster_is_reachable_when_enabled_is_not_required(self):
        cluster = make_cluster("off", enabled=False)
        with transaction.atomic():
            locked = acquire_operable_cluster(cluster, require_enabled=False)
        self.assertEqual(locked.pk, cluster.pk)
        # ...but a retired one is refused even then: retirement is terminal.
        retired = retire_cluster(make_cluster("gone"))
        with self.assertRaises(ClusterRetiredError):
            with transaction.atomic():
                acquire_operable_cluster(retired, require_enabled=False)


class AcquireOperableClusterTransactionTests(TransactionTestCase):
    """Separated from the ``TestCase`` above because that class wraps every test in an
    atomic block, which would mask the 'must be inside a transaction' guard."""

    databases = {"default"}

    def test_refuses_to_run_outside_a_transaction(self):
        cluster = make_cluster("ok")
        with self.assertRaises(RuntimeError):
            acquire_operable_cluster(cluster)


class ReaperSelfHealingTests(TestCase):
    """Every queued/running audit row blocks disable via
    ``active_cluster_operation_labels``, so each must have a reaper or a dead worker
    is a permanent lockout. R1b closes the queued gap and adds the catch-all."""

    def test_orphaned_non_guest_running_row_is_reaped(self):
        cluster = make_cluster("c")
        event = _make_stale_audit(action="provider.some.future_op", outcome="running", cluster=cluster)
        reaped = _reap_orphaned_cluster_operations(now=timezone.now())
        event.refresh_from_db()
        self.assertEqual(reaped, 1)
        self.assertEqual(event.outcome, "failed")
        self.assertTrue(event.details.get("retryable"))

    def test_orphaned_queued_row_is_reaped(self):
        event = _make_stale_audit(action="provider.some.future_op", outcome="queued")
        _reap_orphaned_cluster_operations(now=timezone.now())
        event.refresh_from_db()
        self.assertEqual(event.outcome, "failed")

    def test_a_row_with_a_live_broker_task_is_left_alone(self):
        event = _make_stale_audit(
            action="provider.some.future_op", outcome="running", details={"worker_task_id": "live-1"}
        )
        with patch("core.tasks.queued_task_ids", return_value={"live-1"}):
            reaped = _reap_orphaned_cluster_operations(now=timezone.now())
        event.refresh_from_db()
        self.assertEqual(reaped, 0)
        self.assertEqual(event.outcome, "running")

    def test_guest_actions_are_not_touched_by_the_catch_all(self):
        # guest.* is owned by the provider-resolving loop; the catch-all must skip it
        # so a genuinely long-running migrate is not failed without asking Proxmox.
        event = _make_stale_audit(action="guest.power.stop", outcome="running")
        reaped = _reap_orphaned_cluster_operations(now=timezone.now())
        event.refresh_from_db()
        self.assertEqual(reaped, 0)
        self.assertEqual(event.outcome, "running")

    def test_owned_heartbeat_actions_are_not_touched_by_the_catch_all(self):
        from core.services.storage_catalog_refresh import STORAGE_CATALOG_REFRESH_ACTION

        event = _make_stale_audit(action=STORAGE_CATALOG_REFRESH_ACTION, outcome="queued")
        reaped = _reap_orphaned_cluster_operations(now=timezone.now())
        event.refresh_from_db()
        self.assertEqual(reaped, 0)
        self.assertEqual(event.outcome, "queued")

    def test_stale_queued_guest_row_with_no_live_task_is_reaped(self):
        cluster = make_cluster("c")
        event = _make_stale_audit(action="guest.power.stop", outcome="queued", cluster=cluster)
        with patch("core.tasks.queued_task_ids", return_value=set()):
            reap_stale_guest_tasks()
        event.refresh_from_db()
        self.assertEqual(event.outcome, "failed")

    def test_queued_guest_row_with_a_live_task_survives_the_reaper(self):
        cluster = make_cluster("c")
        event = _make_stale_audit(
            action="guest.power.stop", outcome="queued", cluster=cluster, details={"poll_task_id": "live-2"}
        )
        with patch("core.tasks.queued_task_ids", return_value={"live-2"}):
            reap_stale_guest_tasks()
        event.refresh_from_db()
        self.assertEqual(event.outcome, "queued")


class RefreshStorageCatalogRetiredTerminalTests(TestCase):
    """The worker resolves an unmanaged cluster to a terminal result, not the
    unhandled ``DoesNotExist`` retirement would otherwise make ordinary."""

    def test_retired_cluster_refresh_is_a_terminal_skip(self):
        cluster = retire_cluster(make_cluster("gone"))
        result = refresh_storage_catalog_for_cluster(cluster.key)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "cluster_not_operable")

    def test_disabled_cluster_refresh_is_a_terminal_skip(self):
        cluster = make_cluster("off", enabled=False)
        self.assertTrue(refresh_storage_catalog_for_cluster(cluster.key)["skipped"])

    def test_missing_cluster_refresh_is_a_terminal_skip(self):
        self.assertTrue(refresh_storage_catalog_for_cluster("no-such-key")["skipped"])


class ContextProcessorScopeCutoverTests(TestCase):
    """The taskbar's cluster list is the managed set; the archive flag is historical."""

    def _app_settings(self):
        from core.context_processors import app_settings

        request = RequestFactory().get("/")
        return app_settings(request)

    def test_retired_clusters_are_excluded_from_navigation(self):
        make_cluster("live")
        retire_cluster(make_cluster("retired"))
        ctx = self._app_settings()
        self.assertEqual([c.key for c in ctx["app_nav_clusters"]], ["live"])
        self.assertEqual([c.key for c in ctx["app_enabled_clusters"]], ["live"])
        self.assertTrue(ctx["app_has_clusters"])

    def test_only_retired_installation_still_reports_a_history(self):
        only_retired_installation()
        ctx = self._app_settings()
        self.assertEqual(list(ctx["app_nav_clusters"]), [])
        self.assertEqual(list(ctx["app_enabled_clusters"]), [])
        self.assertTrue(ctx["app_has_clusters"])

    def test_disabled_clusters_navigate_but_are_not_write_targets(self):
        """Disabling retains inventory, schedules and history — and must not hide them.

        Navigation was built from `managed_clusters().filter(enabled=True)`, so
        disabling a cluster removed its datastore, Tags and Scheduled Tasks entries
        outright. Verified retirement is gated on disabling first, so an operator
        preparing to retire a cluster lost the ability to browse it and decide.
        """
        make_cluster("live")
        disabled = make_cluster("paused", enabled=False)
        retire_cluster(make_cluster("gone"))

        ctx = self._app_settings()

        self.assertEqual([c.key for c in ctx["app_nav_clusters"]], ["live", "paused"])
        # Write targets — "register this disk as a VM in ..." — stay enabled-only.
        self.assertEqual([c.key for c in ctx["app_enabled_clusters"]], ["live"])
        self.assertTrue(ctx["app_multiple_clusters"])
        self.assertEqual(cluster_degraded_label(disabled), "Disabled")

    def test_quarantine_outranks_disabled_in_the_navigation_label(self):
        cluster = make_cluster("suspect", enabled=False)
        cluster.ingestion_quarantined = True
        cluster.quarantine_reason = "CA mismatch."
        cluster.save(update_fields=["ingestion_quarantined", "quarantine_reason"])

        self.assertEqual(cluster_degraded_label(cluster), "Quarantined")

    def test_empty_installation_reports_no_clusters(self):
        self.assertFalse(self._app_settings()["app_has_clusters"])


@override_settings(APP_REQUIRE_LOGIN=False)
class LegacyRedirectScopeCutoverTests(TestCase):
    """The unscoped-URL redirect routes a retired-only install to its archive, not to
    onboarding, and never treats a retired cluster as an active target."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(username="op")

    def _redirect_response(self):
        from core.views.cluster_scope import legacy_cluster_redirect

        view = legacy_cluster_redirect("core:clusters_overview")
        request = self.factory.get("/legacy/")
        request.user = self.user
        return view(request)

    def test_retired_only_install_lands_on_the_archive_overview(self):
        only_retired_installation()
        response = self._redirect_response()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("core:clusters_overview"))

    def test_empty_install_lands_on_onboarding(self):
        response = self._redirect_response()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("core:cluster_add"))


class ScanAdmissionLockTests(TestCase):
    """The admission decision — 'is a scan active?' and the create it gates — is one
    step, so a second admission cannot slip a duplicate scan past the first."""

    @patch("core.tasks.async_task", return_value="scan-task-1")
    def test_first_admission_creates_exactly_one_scan(self, _async_task):
        scan_id = enqueue_scheduled_scan()
        self.assertIsNotNone(scan_id)
        self.assertEqual(ScanRun.objects.filter(status=ScanRun.Status.QUEUED).count(), 1)

    @patch("core.tasks.async_task", return_value="scan-task-1")
    def test_an_active_scan_blocks_a_new_admission(self, _async_task):
        ScanRun.objects.create(status=ScanRun.Status.QUEUED, progress_message="already active")
        self.assertIsNone(enqueue_scheduled_scan())
        self.assertEqual(ScanRun.objects.count(), 1)


class ScanAdmissionConcurrencyTests(TransactionTestCase):
    """The PostgreSQL proof: concurrent admissions serialise on the installation-wide
    lock and admit exactly one scan. Without the lock they each read 'none active'
    and create their own."""

    databases = {"default"}

    def test_concurrent_admissions_admit_exactly_one_scan(self):
        if connection.vendor != "postgresql":
            self.skipTest("advisory-lock serialisation only holds on PostgreSQL")
        barrier = threading.Barrier(4)
        errors: list[Exception] = []

        def worker():
            try:
                barrier.wait(timeout=10)
                enqueue_scheduled_scan()
            except Exception as exc:  # a silent thread failure would fake a pass
                errors.append(exc)
            finally:
                connection.close()

        with patch("core.tasks.async_task", return_value="scan-task"):
            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(ScanRun.objects.filter(status=ScanRun.Status.QUEUED).count(), 1)


class ClusterLifecycleLockConcurrencyTests(TransactionTestCase):
    """Provider-operation acquisition and disable serialise on the same per-cluster
    lock, and a different cluster is never caught by it."""

    databases = {"default"}

    def test_acquisition_blocks_a_concurrent_disable_of_the_same_cluster(self):
        if connection.vendor != "postgresql":
            self.skipTest("advisory-lock serialisation only holds on PostgreSQL")
        cluster = make_cluster("locked")
        acquired = threading.Event()
        release = threading.Event()

        def holder():
            try:
                with transaction.atomic():
                    acquire_operable_cluster(cluster)
                    acquired.set()
                    release.wait(timeout=10)
            finally:
                connection.close()

        disable_result: dict[str, object] = {}

        def disabler():
            try:
                disable_cluster(cluster)
                disable_result["ok"] = True
            except Exception as exc:
                disable_result["error"] = exc
            finally:
                connection.close()

        holder_thread = threading.Thread(target=holder)
        holder_thread.start()
        self.assertTrue(acquired.wait(timeout=10))

        disabler_thread = threading.Thread(target=disabler)
        disabler_thread.start()
        # The holder keeps its transaction — and the lifecycle lock — open, so the
        # disable cannot proceed until it is released.
        disabler_thread.join(timeout=1)
        self.assertTrue(disabler_thread.is_alive(), "disable must block behind the held lifecycle lock")

        release.set()
        holder_thread.join(timeout=10)
        disabler_thread.join(timeout=10)
        self.assertFalse(disabler_thread.is_alive())
        self.assertEqual(disable_result, {"ok": True})
        cluster.refresh_from_db()
        self.assertFalse(cluster.enabled)

    def test_a_second_cluster_is_not_blocked_by_the_first(self):
        if connection.vendor != "postgresql":
            self.skipTest("advisory-lock serialisation only holds on PostgreSQL")
        held = make_cluster("held")
        other = make_cluster("other")
        acquired = threading.Event()
        release = threading.Event()

        def holder():
            try:
                with transaction.atomic():
                    acquire_operable_cluster(held)
                    acquired.set()
                    release.wait(timeout=10)
            finally:
                connection.close()

        holder_thread = threading.Thread(target=holder)
        holder_thread.start()
        try:
            self.assertTrue(acquired.wait(timeout=10))
            # A different cluster uses a different lock, so this must not block.
            disable_cluster(other)
            other.refresh_from_db()
            self.assertFalse(other.enabled)
        finally:
            release.set()
            holder_thread.join(timeout=10)


class LifecycleLockContextManagerTests(TransactionTestCase):
    """The context managers take transaction-scoped locks, so they require an open
    transaction and release on its end with no explicit unlock to leak."""

    databases = {"default"}

    def test_cluster_lifecycle_lock_is_reentrant_within_one_transaction(self):
        if connection.vendor != "postgresql":
            self.skipTest("advisory locks are a no-op off PostgreSQL")
        cluster = make_cluster("c")
        # pg_advisory_xact_lock is reentrant on the same connection, so nesting the
        # same cluster's lock inside one transaction must not self-deadlock.
        with transaction.atomic():
            with cluster_lifecycle_lock(cluster):
                with cluster_lifecycle_lock(cluster):
                    locked = ProxmoxCluster.objects.select_for_update().get(pk=cluster.pk)
        self.assertEqual(locked.pk, cluster.pk)


# ---------------------------------------------------------------------------
# R4 slice 1: operational-footprint stamping and fail-closed unused-connection
# hard-delete eligibility. No mutation, no UI -- a durable marker and a read.
# ---------------------------------------------------------------------------


def _reload(cluster: ProxmoxCluster) -> ProxmoxCluster:
    return ProxmoxCluster.objects.get(pk=cluster.pk)


class OperationalFootprintStampingTests(TestCase):
    """``operational_footprint_at`` is the durable memory the eligibility check
    leans on, so it must be stamped exactly once, monotonically, at each place a
    cluster first acquires non-configuration footprint -- and never by a mere
    connection/lifecycle event."""

    def test_stamp_is_idempotent_and_monotonic(self):
        cluster = make_cluster("c")
        self.assertTrue(stamp_operational_footprint(cluster, reason=FOOTPRINT_SCAN_OBSERVATION))
        first = _reload(cluster)
        self.assertIsNotNone(first.operational_footprint_at)
        self.assertEqual(first.operational_footprint_reason, FOOTPRINT_SCAN_OBSERVATION)
        # A later background footprint must move neither the timestamp nor the
        # reason: the marker records that footprint was *first* acquired, not last.
        self.assertFalse(stamp_operational_footprint(cluster.pk, reason=FOOTPRINT_GUEST_PROJECTION))
        second = _reload(cluster)
        self.assertEqual(second.operational_footprint_at, first.operational_footprint_at)
        self.assertEqual(second.operational_footprint_reason, FOOTPRINT_SCAN_OBSERVATION)

    def test_an_operator_footprint_upgrades_a_background_reason_in_place(self):
        """Eligibility reads the reason, so first-writer-wins would lose the truth.

        A console session on a connection a background refresh already stamped
        must not hide behind ``scan_observation``: console rows are purged after
        ``CONSOLE_SESSION_RETENTION_HOURS``, and if the reason still claimed the
        footprint was reconstructible, the connection would become hard-deletable
        the moment that purge ran — erasing the only record it was ever used.
        """
        cluster = make_cluster("c")
        self.assertTrue(stamp_operational_footprint(cluster, reason=FOOTPRINT_SCAN_OBSERVATION))
        first = _reload(cluster)

        self.assertFalse(stamp_operational_footprint(first, reason=FOOTPRINT_CONSOLE_SESSION))

        upgraded = _reload(cluster)
        self.assertEqual(upgraded.operational_footprint_at, first.operational_footprint_at)
        self.assertEqual(upgraded.operational_footprint_reason, FOOTPRINT_CONSOLE_SESSION)
        self.assertFalse(unused_connection_deletion_eligibility(upgraded).eligible)

    def test_an_operator_reason_is_never_downgraded_by_a_later_background_stamp(self):
        cluster = make_cluster("c")
        stamp_operational_footprint(cluster, reason=FOOTPRINT_PROVIDER_OPERATION)

        stamp_operational_footprint(_reload(cluster), reason=FOOTPRINT_GUEST_PROJECTION)

        self.assertEqual(_reload(cluster).operational_footprint_reason, FOOTPRINT_PROVIDER_OPERATION)

    def test_missing_cluster_id_is_a_safe_no_op(self):
        self.assertFalse(stamp_operational_footprint(None, reason=FOOTPRINT_SCAN_OBSERVATION))

    def test_provider_operation_audit_event_stamps_footprint(self):
        cluster = make_cluster("c")
        record_audit_event(action="guest.power.start", cluster=cluster, object_type="guest")
        self.assertEqual(_reload(cluster).operational_footprint_reason, FOOTPRINT_PROVIDER_OPERATION)

    def test_configuration_audit_event_does_not_stamp_footprint(self):
        cluster = make_cluster("c")
        record_audit_event(action="cluster.enabled", cluster=cluster, object_type="cluster")
        # A refused verified-retirement attempt is lifecycle, not footprint: an
        # otherwise-unused connection must not be branded operational by trying.
        record_audit_event(action="cluster.retirement_refused", cluster=cluster, object_type="cluster")
        self.assertIsNone(_reload(cluster).operational_footprint_at)

    def test_cluster_free_audit_event_stamps_nothing(self):
        cluster = make_cluster("c")
        record_audit_event(action="guest.power.start", object_type="guest")
        self.assertIsNone(_reload(cluster).operational_footprint_at)

    def test_scan_observation_stamps_footprint(self):
        cluster = make_cluster("c")
        from core.tasks import _record_cluster_observations

        scan = ScanRun.objects.create()
        endpoints = list(cluster.endpoints.all())
        _record_cluster_observations(scan, endpoints, {}, {}, {})
        self.assertEqual(_reload(cluster).operational_footprint_reason, FOOTPRINT_SCAN_OBSERVATION)


class UnusedConnectionEligibilityTests(TestCase):
    """``Delete unused connection`` may run only for a connection proven to carry
    no operational footprint. The check fails closed: any blocking relation row,
    the durable marker, a retired state, or an unclassified relation blocks it."""

    def _blocker_relations(self, cluster) -> set[str]:
        return {b.relation for b in unused_connection_deletion_eligibility(cluster).blockers}

    def test_a_clean_connection_is_eligible(self):
        # make_cluster creates endpoints -- disposable config that must not block.
        cluster = make_cluster("unused")
        result = unused_connection_deletion_eligibility(cluster)
        self.assertTrue(result.eligible)
        self.assertEqual(result.blockers, ())

    def test_operational_footprint_marker_blocks(self):
        cluster = make_cluster("unused")
        stamp_operational_footprint(cluster, reason=FOOTPRINT_PROVIDER_OPERATION)
        self.assertIn(BLOCKER_FOOTPRINT, self._blocker_relations(_reload(cluster)))

    def test_footprint_marker_blocks_even_with_every_relation_empty(self):
        # The whole point of the marker: audit/scan/console retention can empty
        # every blocker relation, and eligibility must still refuse. No relation
        # rows exist here, only the marker.
        cluster = make_cluster("unused")
        stamp_operational_footprint(cluster, reason=FOOTPRINT_CONSOLE_SESSION)
        result = unused_connection_deletion_eligibility(_reload(cluster))
        self.assertFalse(result.eligible)
        self.assertEqual([b.relation for b in result.blockers], [BLOCKER_FOOTPRINT])

    def test_retired_cluster_is_ineligible(self):
        cluster = retire_cluster(make_cluster("gone"))
        self.assertIn(BLOCKER_RETIRED, self._blocker_relations(cluster))

    def test_operational_audit_event_blocks_but_configuration_does_not(self):
        # Build rows directly so the audit-sweep is isolated from footprint
        # stamping (record_audit_event would stamp the marker as well).
        operational = make_cluster("op")
        AuditEvent.objects.create(action="guest.power.start", object_type="guest", cluster=operational)
        self.assertIn("audit_events", self._blocker_relations(operational))

        configured = make_cluster("cfg")
        AuditEvent.objects.create(action="cluster.enabled", object_type="cluster", cluster=configured)
        AuditEvent.objects.create(action="cluster.unused_connection_deleted", object_type="cluster", cluster=configured)
        self.assertTrue(unused_connection_deletion_eligibility(configured).eligible)

    def test_background_written_state_does_not_block(self):
        """The rows a periodic refresh writes are not evidence anybody used the connection.

        Every relation here is rebuilt from Proxmox by the next refresh, and the
        guest/storage refreshes reach a new connection within about a minute. When
        these blocked, ``Delete unused connection`` was reachable for roughly sixty
        seconds after onboarding and never again — a control that cannot be used is
        not a safety property.
        """
        cluster = make_cluster("machine")
        ScanClusterObservation.objects.create(scan_run=ScanRun.objects.create(), cluster=cluster)
        CurrentGuestInventoryState.objects.create(cluster=cluster)
        ClusterStorage.objects.create(cluster=cluster, storage_id="local", storage_type="dir")
        StorageCatalogState.objects.create(cluster=cluster)
        stamp_operational_footprint(cluster, reason=FOOTPRINT_GUEST_PROJECTION)

        self.assertTrue(unused_connection_deletion_eligibility(_reload(cluster)).eligible)

    def test_an_unrecognised_footprint_reason_blocks(self):
        # The reason allowlist fails closed exactly like the relation registry: a
        # new reason code is a gap to classify, not a default-allow.
        cluster = make_cluster("unknown-reason")
        stamp_operational_footprint(cluster, reason="something_new")
        self.assertIn(BLOCKER_FOOTPRINT, self._blocker_relations(_reload(cluster)))

    def test_each_blocking_relation_kind_fails_closed(self):
        console = make_cluster("con")
        ConsoleSession.objects.create(
            cluster=console,
            token_hash="deadbeef",
            target_type="vm",
            target_vmid=100,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        self.assertIn("console_sessions", self._blocker_relations(console))

        _storage, first, _second = shared_storage_with_consumers()
        self.assertIn("storage_consumers", self._blocker_relations(first))

    def test_active_and_soft_deleted_schedules_both_block(self):
        active = make_cluster("sched-a")
        ScheduledAction.objects.create(
            cluster=active,
            name="nightly",
            action_type=ScheduledAction.ActionType.SHUTDOWN,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=100,
        )
        self.assertIn("scheduled_actions", self._blocker_relations(active))

        retired_schedule = make_cluster("sched-b")
        ScheduledAction.objects.create(
            cluster=retired_schedule,
            name="was-nightly",
            action_type=ScheduledAction.ActionType.SHUTDOWN,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=100,
            deleted_at=timezone.now(),
        )
        # Soft-deleted schedules are retained history; their presence still blocks.
        self.assertIn("scheduled_actions", self._blocker_relations(retired_schedule))

    def test_an_unclassified_reverse_relation_blocks(self):
        # Simulate a relation the registry has not yet classified by dropping a
        # known accessor from the classification the service reads. A real future
        # relation added without a registry row must land here, not default-allow.
        cluster = make_cluster("unclassified")
        trimmed = {k: v for k, v in CLUSTER_REVERSE_RELATIONS.items() if k != "endpoints"}
        with patch(
            "core.services.cluster_deletion_eligibility.CLUSTER_REVERSE_RELATIONS",
            trimmed,
        ):
            result = unused_connection_deletion_eligibility(cluster)
        self.assertFalse(result.eligible)
        endpoint_blocker = next(b for b in result.blockers if b.relation == "endpoints")
        self.assertEqual(endpoint_blocker.kind, "unclassified_relation")


def _configured_unused_connection(key: str, *, ca_uuid: str = "") -> ProxmoxCluster:
    """A deliberately-created connection that never acquired operational footprint.

    It carries exactly what the add path leaves behind -- one ``ProxmoxCluster``,
    endpoints, a credential and a transport trust -- and nothing operational, so it
    is the shape ``Delete unused connection`` is scoped for.
    """
    cluster = make_cluster(key, ca_uuid=ca_uuid, ca_fingerprint="AA:BB" if ca_uuid else "")
    ClusterCredential.objects.create(
        cluster=cluster,
        token_id=f"pve-helper@pve!{key}",
        token_secret_sealed="sealed",
        encryption_key_id="key-1",
    )
    ClusterTransportTrust.objects.create(
        cluster=cluster,
        mode=ClusterTransportTrust.Mode.PUBLIC,
        approved_at=timezone.now(),
    )
    return cluster


class ClusterConnectionHardDeleteTests(TestCase):
    """R4 slice 2: the strict unused-connection hard delete. It physically removes
    the row and its disposable configuration, preserves the configuration Audit
    trail by detaching it, and releases the key, CA UUID and endpoint URLs -- but
    only for a connection the eligibility check proves carries no footprint."""

    def setUp(self):
        bootstrap_runtime()
        self.actor = get_user_model().objects.create_user(username="operator")

    def test_eligible_connection_is_deleted_with_its_configuration(self):
        cluster = _configured_unused_connection("unused")
        pk = cluster.pk

        result = delete_unused_cluster_connection(cluster, actor=self.actor)

        self.assertEqual(result.cluster_key, "unused")
        self.assertEqual(result.endpoints_deleted, 1)
        self.assertTrue(result.credential_deleted)
        self.assertTrue(result.trust_deleted)
        self.assertFalse(historical_clusters().filter(pk=pk).exists())
        self.assertFalse(ProxmoxEndpoint.objects.filter(cluster_id=pk).exists())
        self.assertFalse(ClusterCredential.objects.filter(cluster_id=pk).exists())
        self.assertFalse(ClusterTransportTrust.objects.filter(cluster_id=pk).exists())

    def test_background_written_state_is_deleted_with_the_connection(self):
        """The teardown must actually clear what eligibility stopped blocking on.

        Every model here is PROTECT on the cluster, so a relation left behind is
        not a cosmetic leak: the cluster delete would fail and the postcondition
        assertion would refuse the whole transaction.
        """
        cluster = _configured_unused_connection("unused")
        pk = cluster.pk
        scan = ScanRun.objects.create()
        ScanClusterObservation.objects.create(scan_run=scan, cluster=cluster)
        ProxmoxInventory.objects.create(
            scan_run=scan,
            cluster=cluster,
            node="pve1",
            object_type=ProxmoxInventory.ObjectType.VM,
            vmid=100,
        )
        CurrentGuestInventoryState.objects.create(cluster=cluster)
        StorageCatalogState.objects.create(cluster=cluster)
        ClusterStorage.objects.create(cluster=cluster, storage_id="local", storage_type="dir")
        stamp_operational_footprint(cluster, reason=FOOTPRINT_GUEST_PROJECTION)

        result = delete_unused_cluster_connection(_reload(cluster), actor=self.actor)

        self.assertEqual(result.projection_rows_deleted, 5)
        self.assertFalse(historical_clusters().filter(pk=pk).exists())
        self.assertFalse(ScanClusterObservation.objects.filter(cluster_id=pk).exists())
        self.assertFalse(ProxmoxInventory.objects.filter(cluster_id=pk).exists())
        self.assertFalse(CurrentGuestInventoryState.objects.filter(cluster_id=pk).exists())
        self.assertFalse(StorageCatalogState.objects.filter(cluster_id=pk).exists())
        self.assertFalse(ClusterStorage.objects.filter(cluster_id=pk).exists())
        # The scan itself is a global orchestration job and is not the connection's
        # to delete; only its coverage of this cluster goes.
        self.assertTrue(ScanRun.objects.filter(pk=scan.pk).exists())
        event = AuditEvent.objects.get(pk=result.audit_event_id)
        self.assertEqual(event.details["reconstructible_rows_deleted_total"], 5)
        self.assertEqual(event.details["footprint_reason"], FOOTPRINT_GUEST_PROJECTION)

    def test_configuration_audit_is_detached_and_preserved(self):
        cluster = _configured_unused_connection("unused")
        # record_audit_event always stamps the durable key snapshot on a
        # cluster-attached event; build the config trail the same way so it stays
        # discoverable after the relation is detached.
        AuditEvent.objects.create(
            action="cluster.added", object_type="cluster", cluster=cluster, cluster_key_snapshot="unused"
        )
        AuditEvent.objects.create(
            action="cluster.enabled", object_type="cluster", cluster=cluster, cluster_key_snapshot="unused"
        )
        pk = cluster.pk

        result = delete_unused_cluster_connection(cluster, actor=self.actor)

        # No event still points at the removed row (PROTECT would otherwise have
        # refused the delete)...
        self.assertFalse(AuditEvent.objects.filter(cluster_id=pk).exists())
        # ...but the trail survives, discoverable by the durable key snapshot,
        # including the final deletion event itself.
        preserved = AuditEvent.objects.filter(cluster_key_snapshot="unused", cluster__isnull=True)
        self.assertEqual(preserved.count(), 3)
        self.assertTrue(preserved.filter(action="cluster.unused_connection_deleted").exists())
        self.assertGreaterEqual(result.audit_events_detached, 3)

    def test_deletion_records_the_final_event_before_removal(self):
        cluster = _configured_unused_connection("unused")

        result = delete_unused_cluster_connection(cluster, actor=self.actor)

        event = AuditEvent.objects.get(pk=result.audit_event_id)
        self.assertEqual(event.action, "cluster.unused_connection_deleted")
        self.assertEqual(event.cluster_key_snapshot, "unused")
        self.assertIsNone(event.cluster_id)
        self.assertEqual(event.details["endpoint_count"], 1)
        # This connection had no prior configuration events, so only the deletion
        # event itself is detached here; the count is of the pre-existing trail.
        self.assertEqual(event.details["configuration_audit_events_detached"], 0)

    def test_released_key_ca_and_endpoint_url_are_reusable(self):
        cluster = _configured_unused_connection("reused", ca_uuid="dddddddd-dddd-dddd-dddd-dddddddddddd")
        endpoint_url = cluster.endpoints.get().normalized_url

        delete_unused_cluster_connection(cluster, actor=self.actor)

        # The same permanent key, the same physical CA identity and the same
        # endpoint URL all register again with no uniqueness collision.
        replacement = ProxmoxCluster.objects.create(
            key="reused",
            display_name="Re-registered",
            discovered_ca_uuid="dddddddd-dddd-dddd-dddd-dddddddddddd",
        )
        ProxmoxEndpoint.objects.create(cluster=replacement, name="pve1", url=endpoint_url)
        self.assertTrue(managed_clusters().filter(key="reused").exists())

    def test_operational_footprint_marker_blocks_and_commits_nothing(self):
        cluster = _configured_unused_connection("busy")
        stamp_operational_footprint(cluster, reason=FOOTPRINT_PROVIDER_OPERATION)

        with self.assertRaises(ClusterDeletionBlocked) as ctx:
            delete_unused_cluster_connection(cluster, actor=self.actor)

        self.assertEqual(ctx.exception.blocker_relation, BLOCKER_FOOTPRINT)
        self.assertTrue(historical_clusters().filter(pk=cluster.pk).exists())
        self.assertTrue(ClusterCredential.objects.filter(cluster_id=cluster.pk).exists())

    def test_operational_audit_row_blocks_deletion(self):
        cluster = _configured_unused_connection("op")
        # A provider-operation event is operational history; build it directly so
        # it is the only thing under test.
        AuditEvent.objects.create(action="guest.power.start", object_type="guest", cluster=cluster)

        with self.assertRaises(ClusterDeletionBlocked) as ctx:
            delete_unused_cluster_connection(cluster, actor=self.actor)

        self.assertEqual(ctx.exception.blocker_relation, "audit_events")
        self.assertTrue(historical_clusters().filter(pk=cluster.pk).exists())

    def test_retired_cluster_cannot_be_hard_deleted(self):
        cluster = retire_cluster(_configured_unused_connection("gone"))

        # A retired cluster permanently reserves its key; it is outside the managed
        # scope the deletion resolves against.
        with self.assertRaises(ClusterDeletionNotAllowed):
            delete_unused_cluster_connection(cluster, actor=self.actor)
        self.assertTrue(historical_clusters().filter(pk=cluster.pk).exists())

    def test_deleting_the_last_connection_does_not_reimport_bootstrap(self):
        cluster = _configured_unused_connection("only")

        delete_unused_cluster_connection(cluster, actor=self.actor)

        self.assertFalse(has_managed_clusters())
        self.assertFalse(has_historical_clusters())
        # The durable bootstrap marker is what keeps onboarding from re-importing;
        # it survives deleting every cluster.
        self.assertTrue(RuntimeConfigurationState.objects.get().bootstrap_completed)

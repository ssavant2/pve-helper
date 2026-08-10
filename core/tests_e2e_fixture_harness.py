from __future__ import annotations

from collections import Counter
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings

from core.models import (
    AuditEvent,
    ClusterStorage,
    CurrentGuestInventory,
    CurrentGuestInventoryState,
    ProxmoxCluster,
    StorageCatalogState,
)
from scripts.e2e_seed import (
    CLUSTER_FIXTURES,
    FIXTURE_NAMESPACE,
    LONG_NODE_NAME,
    SCENARIO_CLUSTER_KEYS,
    _assert_isolated_database,
    seed_database,
)

E2E_KEYRING = "e2e:ZWVlZWVlZWVlZWVlZWVlZWVlZWVlZWVlZWVlZWVlZWU="


class RepresentativeTopologyManifestTests(SimpleTestCase):
    """5a0C's breadth is a reviewable contract, not incidental seed data."""

    def test_manifest_covers_the_required_topology_and_independent_states(self):
        node_counts = Counter(node.name for fixture in CLUSTER_FIXTURES for node in fixture.nodes)
        states = {fixture.membership for fixture in CLUSTER_FIXTURES} | {
            state for fixture in CLUSTER_FIXTURES for state in fixture.domains.values()
        }

        self.assertGreaterEqual(len(CLUSTER_FIXTURES), 2)
        self.assertGreaterEqual(node_counts["pve1"], 2, "duplicate node names across cluster keys are required")
        self.assertTrue(any(fixture.role == "standalone" for fixture in CLUSTER_FIXTURES))
        self.assertTrue(
            all(len(fixture.nodes) == 1 for fixture in CLUSTER_FIXTURES if fixture.role == "standalone"),
            "a true standalone domain always has exactly one node",
        )
        self.assertTrue(
            any(fixture.role == "corosync" and len(fixture.nodes) == 1 for fixture in CLUSTER_FIXTURES),
            "one-node corosync must not collapse into standalone",
        )
        self.assertTrue(
            any(
                fixture.role == "corosync"
                and len(fixture.nodes) > 1
                and fixture.quorate
                and fixture.membership == "fresh"
                and all(node.online and node.runtime == "fresh" for node in fixture.nodes)
                and all(state == "fresh" for state in fixture.domains.values())
                for fixture in CLUSTER_FIXTURES
            ),
            "healthy multi-node means more than quorate: every node and declared domain must be fresh",
        )
        self.assertTrue(any(not node.online for fixture in CLUSTER_FIXTURES for node in fixture.nodes))
        self.assertTrue(
            any(
                fixture.role == "corosync"
                and len(fixture.nodes) == 2
                and fixture.quorate is False
                and fixture.qdevice is False
                for fixture in CLUSTER_FIXTURES
            )
        )
        self.assertTrue(any(not fixture.enabled for fixture in CLUSTER_FIXTURES))
        self.assertTrue(any(fixture.quarantined for fixture in CLUSTER_FIXTURES))
        self.assertTrue(any(fixture.transition_pending == "corosync" for fixture in CLUSTER_FIXTURES))
        self.assertTrue(any(fixture.external_generation_changed for fixture in CLUSTER_FIXTURES))
        self.assertEqual({"fresh", "partial", "stale", "permission_denied"} - states, set())
        self.assertIn(LONG_NODE_NAME, node_counts)
        self.assertTrue(
            any(
                fixture.membership == "partial" and "partial" not in fixture.domains.values()
                for fixture in CLUSTER_FIXTURES
            ),
            "membership coverage needs an independently partial fixture",
        )
        self.assertTrue(
            any(
                fixture.membership == "fresh"
                and fixture.domains.get("guests") == "partial"
                and all(state != "partial" for domain, state in fixture.domains.items() if domain != "guests")
                for fixture in CLUSTER_FIXTURES
            ),
            "guest coverage needs an independently partial fixture",
        )
        self.assertTrue(
            any(
                fixture.membership == "fresh"
                and fixture.domains.get("storage") == "partial"
                and all(state != "partial" for domain, state in fixture.domains.items() if domain != "storage")
                for fixture in CLUSTER_FIXTURES
            ),
            "storage coverage needs an independently partial fixture",
        )

    def test_zero_and_retired_only_are_first_class_scenarios(self):
        self.assertEqual(SCENARIO_CLUSTER_KEYS["zero"], ())
        self.assertEqual(SCENARIO_CLUSTER_KEYS["retired-only"], ())
        self.assertIn("representative", SCENARIO_CLUSTER_KEYS)


@override_settings(
    PVE_HELPER_ENCRYPTION_KEYS=E2E_KEYRING,
    PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID="e2e",
    PVE_TEST_NETWORK_DISABLED=True,
)
class RepresentativeTopologyMaterializationTests(TestCase):
    def test_reset_refuses_the_django_test_database_even_with_network_disabled(self):
        with patch.dict(
            settings.DATABASES["default"],
            {"ENGINE": "django.db.backends.sqlite3", "NAME": "/tmp/not-the-e2e-database.sqlite3"},
        ):
            with self.assertRaisesRegex(RuntimeError, "only resets its throwaway database"):
                _assert_isolated_database()

    def test_seed_materializes_identity_collisions_density_storage_and_history(self):
        seed_database(reset=False)

        managed = ProxmoxCluster.objects.filter(retired_at__isnull=True)
        self.assertEqual(managed.count(), len(CLUSTER_FIXTURES))
        self.assertTrue(managed.get(key="unused-e2e").enabled is False)
        self.assertTrue(managed.get(key="quarantined-e2e").ingestion_quarantined)

        duplicate_guests = CurrentGuestInventory.objects.filter(object_type="vm", vmid=100).order_by("cluster__key")
        self.assertEqual(
            list(duplicate_guests.values_list("cluster__key", "node")),
            [("e2e", "pve1"), ("standalone-e2e", "pve1")],
        )
        self.assertTrue(CurrentGuestInventory.objects.filter(object_type="ct", vmid=500).count() >= 2)
        self.assertGreaterEqual(CurrentGuestInventory.objects.filter(cluster__key="e2e").count(), 24)
        self.assertEqual(
            set(CurrentGuestInventory.objects.filter(cluster__key="e2e").values_list("ha_state", flat=True)),
            {"", "started"},
        )

        self.assertEqual(
            set(ClusterStorage.objects.filter(cluster__key="e2e").values_list("storage_type", flat=True)),
            {"dir", "nfs", "pbs"},
        )
        self.assertTrue(
            ClusterStorage.objects.filter(
                cluster__key="e2e",
                storage_type="pbs",
                node_states__unreachable=True,
            ).exists()
        )
        guest_partial = CurrentGuestInventoryState.objects.get(cluster__key="guest-partial-e2e")
        self.assertFalse(guest_partial.complete)
        self.assertEqual(guest_partial.endpoints_succeeded, ["guest-partial-pve"])
        self.assertEqual(
            guest_partial.endpoints_attempted,
            ["guest-partial-pve", "guest-unavailable-pve"],
        )
        self.assertGreater(guest_partial.refreshed_at, guest_partial.last_complete_at)
        for key in ("healthy-multi-e2e", "membership-partial-e2e", "storage-partial-e2e"):
            self.assertTrue(
                CurrentGuestInventoryState.objects.get(cluster__key=key).complete,
                f"{key} must materialize its declared fresh guest sibling",
            )
        storage_partial = StorageCatalogState.objects.get(cluster__key="storage-partial-e2e")
        self.assertFalse(storage_partial.metadata_complete)
        self.assertFalse(storage_partial.volume_complete)
        self.assertIsNotNone(storage_partial.metadata_generation)
        self.assertTrue(storage_partial.metadata_errors)
        self.assertTrue(storage_partial.volume_errors)
        for key in ("e2e", "healthy-multi-e2e", "membership-partial-e2e", "guest-partial-e2e"):
            catalog = StorageCatalogState.objects.get(cluster__key=key)
            self.assertTrue(catalog.metadata_complete, f"{key} must materialize fresh storage metadata")
            self.assertTrue(catalog.volume_complete, f"{key} must materialize fresh storage volume coverage")
        standalone_catalog = StorageCatalogState.objects.get(cluster__key="standalone-e2e")
        self.assertTrue(standalone_catalog.metadata_complete)
        self.assertEqual(standalone_catalog.metadata_refreshed_at, standalone_catalog.metadata_last_attempt_at)
        self.assertEqual(standalone_catalog.volume_refreshed_at, standalone_catalog.volume_last_attempt_at)
        self.assertLess(storage_partial.metadata_refreshed_at, storage_partial.metadata_last_attempt_at)
        self.assertEqual(
            managed.filter(display_name="Duplicate partial-coverage fixture").count(),
            2,
            "display names are deliberately non-unique; navigation must use the permanent key",
        )

        primary_fixture = managed.get(key="e2e").details[FIXTURE_NAMESPACE]
        self.assertEqual(
            primary_fixture["domains"],
            {"runtime": "partial", "guests": "partial", "storage": "fresh", "updates": "stale"},
        )
        self.assertNotEqual(
            primary_fixture["external_generation"]["rendered"],
            primary_fixture["external_generation"]["current"],
        )
        transition = managed.get(key="transition-e2e")
        self.assertEqual(transition.details[FIXTURE_NAMESPACE]["transition_pending"], "corosync")
        self.assertEqual(transition.details[FIXTURE_NAMESPACE]["role"], "standalone")
        self.assertEqual(transition.details[FIXTURE_NAMESPACE]["membership"], "stale")
        self.assertNotIn("membership", transition.details[FIXTURE_NAMESPACE]["domains"])
        self.assertTrue(transition.ingestion_quarantined)
        self.assertFalse(ProxmoxCluster.objects.filter(key="transition-old-e2e").exists())
        history = AuditEvent.objects.get(action="cluster.topology_transition_detected")
        self.assertEqual(history.cluster, transition)
        self.assertEqual(history.cluster_key_snapshot, transition.key)
        self.assertEqual(history.details["registered_role"], "standalone")
        self.assertEqual(history.details["pending_role"], "corosync")
        self.assertTrue(history.details["operator_confirmation_required"])

    def test_retired_only_scenario_has_history_but_no_managed_cluster(self):
        seed_database(scenario="retired-only", reset=False)

        self.assertFalse(ProxmoxCluster.objects.filter(retired_at__isnull=True).exists())
        self.assertEqual(ProxmoxCluster.objects.filter(retired_at__isnull=False).count(), 1)
        self.assertTrue(AuditEvent.objects.filter(cluster_key_snapshot="retired-e2e").exists())

    def test_zero_scenario_is_really_empty(self):
        seed_database(scenario="zero", reset=False)

        self.assertFalse(ProxmoxCluster.objects.exists())


class E2EHarnessSourceInvariantTests(SimpleTestCase):
    def _read(self, relative: str) -> str:
        return (Path(settings.BASE_DIR) / relative).read_text()

    def test_compose_calls_the_reviewable_seed_instead_of_an_inline_shell_program(self):
        compose = self._read("docker-compose.tools.yml")

        self.assertIn(
            "python scripts/e2e_seed.py --scenario $${E2E_SEED_SCENARIO:-representative}",
            compose,
        )
        self.assertNotIn("python manage.py shell -c", compose)
        self.assertNotIn("CurrentGuestInventory.objects.create", compose)

    def test_the_production_runtime_source_does_not_copy_the_seed(self):
        dockerfile = self._read("Dockerfile")
        runtime_source = dockerfile.split("FROM busybox:", 1)[1].split("FROM base AS runtime", 1)[0]

        self.assertIn("COPY scripts/worker_healthcheck.py scripts/worker_healthcheck.py", runtime_source)
        self.assertNotIn("e2e_seed", runtime_source)
        self.assertNotRegex(runtime_source, r"(?m)^COPY\s+scripts/?\s+scripts/?$")

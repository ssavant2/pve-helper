from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import (
    AuditEvent,
    ClusterCredential,
    ClusterTransportTrust,
    ProxmoxCluster,
    RuntimeConfigurationState,
)
from core.services.cluster_deletion_eligibility import unused_connection_deletion_eligibility
from core.services.cluster_footprint import FOOTPRINT_INVENTORY_BOOTSTRAP
from core.services.cluster_identity import ObservedClusterIdentity
from core.services.cluster_inventory_bootstrap import (
    CLUSTER_INVENTORY_BOOTSTRAP_ACTION,
    ClusterInventoryBootstrapAlreadyActive,
    ClusterInventoryBootstrapQueueError,
    execute_cluster_inventory_bootstrap,
    queue_cluster_inventory_bootstrap,
)
from core.services.cluster_onboarding import ClusterCandidate, VerifiedConnection
from core.services.cluster_trust import TRUST_PUBLIC, InspectedCertificate
from core.services.recent_tasks import recent_task_page

TEST_KEY = base64.b64encode(b"v" * 32).decode()

_SERVICE = "core.services.cluster_inventory_bootstrap"


def _catalog_state(*, metadata_complete=True, volume_complete=True, metadata_errors=None, volume_errors=None):
    return SimpleNamespace(
        metadata_complete=metadata_complete,
        volume_complete=volume_complete,
        metadata_errors=metadata_errors or {},
        volume_errors=volume_errors or {},
    )


class ClusterInventoryBootstrapServiceTests(TestCase):
    def setUp(self):
        self.cluster = ProxmoxCluster.objects.create(key="alpha", display_name="Alpha", enabled=True)
        # The worker resolves through `provider_acquirable_clusters`, so the
        # connection needs the credential and trust a live call would use. The
        # sealed secret is never opened here: every provider call is patched.
        ClusterCredential.objects.create(
            cluster=self.cluster,
            token_id="pve-helper@pve!pve-helper",
            token_secret_sealed="sealed",
            encryption_key_id="test",
        )
        ClusterTransportTrust.objects.create(cluster=self.cluster, mode=ClusterTransportTrust.Mode.PUBLIC)

    def test_the_record_exists_before_the_job_is_enqueued(self):
        """The row is the durable operation; the queue only carries its id."""
        seen: dict[str, object] = {}

        def _capture(_path, event_id, **_kwargs):
            event = AuditEvent.objects.get(pk=event_id)
            seen["outcome"] = event.outcome
            seen["stage"] = event.details["stage"]
            return "task-1"

        with patch(f"{_SERVICE}.async_task", side_effect=_capture):
            event, task_id = queue_cluster_inventory_bootstrap(cluster=self.cluster)

        self.assertEqual(seen, {"outcome": "queued", "stage": "queued"})
        self.assertEqual(task_id, "task-1")
        event.refresh_from_db()
        self.assertEqual(event.details["worker_task_id"], "task-1")
        self.assertEqual(event.details["cluster_key"], "alpha")

    def test_an_enqueue_failure_is_a_terminal_row_rather_than_a_lost_job(self):
        with patch(f"{_SERVICE}.async_task", side_effect=RuntimeError("broker down")):
            with self.assertRaises(ClusterInventoryBootstrapQueueError):
                queue_cluster_inventory_bootstrap(cluster=self.cluster)

        event = AuditEvent.objects.get(action=CLUSTER_INVENTORY_BOOTSTRAP_ACTION)
        self.assertEqual(event.outcome, "failed")
        self.assertEqual(event.details["stage"], "enqueue failed")
        self.assertNotIn("broker down", str(event.details))

    def test_a_second_bootstrap_is_refused_while_one_is_active(self):
        with patch(f"{_SERVICE}.async_task", return_value="task-1"):
            queue_cluster_inventory_bootstrap(cluster=self.cluster)
            with self.assertRaises(ClusterInventoryBootstrapAlreadyActive):
                queue_cluster_inventory_bootstrap(cluster=self.cluster)

        self.assertEqual(AuditEvent.objects.filter(action=CLUSTER_INVENTORY_BOOTSTRAP_ACTION).count(), 1)

    def test_the_worker_walks_guests_storage_and_tags_to_success(self):
        with patch(f"{_SERVICE}.async_task", return_value="task-1"):
            event, _ = queue_cluster_inventory_bootstrap(cluster=self.cluster)

        with (
            patch(
                f"{_SERVICE}.fetch_verified_guest_inventory",
                return_value=SimpleNamespace(guests=[object(), object()], errors=[]),
            ),
            patch(f"{_SERVICE}.reconcile_live_guest_inventory", return_value=SimpleNamespace(complete=True)),
            patch(f"{_SERVICE}.refresh_storage_catalog", return_value=_catalog_state()),
            patch(f"{_SERVICE}.refresh_registered_tags", return_value=(["prod", "test"], "")),
        ):
            execute_cluster_inventory_bootstrap(event.id)

        event.refresh_from_db()
        self.assertEqual(event.outcome, "success")
        self.assertEqual(event.details["stage"], "completed")
        self.assertEqual(event.details["guests"], 2)
        self.assertEqual(event.details["tags"], 2)
        self.assertIn("finished_at", event.details)

    def test_incomplete_coverage_completes_with_a_warning_naming_the_nodes(self):
        with patch(f"{_SERVICE}.async_task", return_value="task-1"):
            event, _ = queue_cluster_inventory_bootstrap(cluster=self.cluster)

        with (
            patch(
                f"{_SERVICE}.fetch_verified_guest_inventory",
                return_value=SimpleNamespace(guests=[], errors=[]),
            ),
            patch(f"{_SERVICE}.reconcile_live_guest_inventory", return_value=SimpleNamespace(complete=True)),
            patch(
                f"{_SERVICE}.refresh_storage_catalog",
                return_value=_catalog_state(volume_complete=False, volume_errors={"pve2": "timeout"}),
            ),
            patch(f"{_SERVICE}.refresh_registered_tags", return_value=([], "")),
        ):
            execute_cluster_inventory_bootstrap(event.id)

        event.refresh_from_db()
        self.assertEqual(event.outcome, "warning")
        self.assertEqual(event.details["incomplete_nodes"], ["pve2"])

    def test_a_provider_failure_is_terminal_and_never_leaks_the_exception(self):
        with patch(f"{_SERVICE}.async_task", return_value="task-1"):
            event, _ = queue_cluster_inventory_bootstrap(cluster=self.cluster)

        with patch(
            f"{_SERVICE}.fetch_verified_guest_inventory",
            side_effect=RuntimeError("token 12345 rejected by pve201"),
        ):
            execute_cluster_inventory_bootstrap(event.id)

        event.refresh_from_db()
        self.assertEqual(event.outcome, "failed")
        self.assertEqual(event.details["stage"], "failed")
        self.assertNotIn("token 12345", str(event.details))

    def test_a_connection_disabled_before_the_worker_runs_fails_the_row(self):
        """Not a crash and not a silent no-op: the operator asked, so it answers."""
        with patch(f"{_SERVICE}.async_task", return_value="task-1"):
            event, _ = queue_cluster_inventory_bootstrap(cluster=self.cluster)
        ProxmoxCluster.objects.filter(pk=self.cluster.pk).update(enabled=False)

        execute_cluster_inventory_bootstrap(event.id)

        event.refresh_from_db()
        self.assertEqual(event.outcome, "failed")
        self.assertIn("no longer be contacted", event.details["error"])

    def test_the_first_inventory_never_blocks_deleting_an_unused_connection(self):
        """The whole point of the reconstructible reason.

        Stamping this operator-grade would make every connection undeletable the
        instant it was added, which is the failure `Delete unused connection` was
        rescued from in the first place.
        """
        with patch(f"{_SERVICE}.async_task", return_value="task-1"):
            queue_cluster_inventory_bootstrap(cluster=self.cluster)

        self.cluster.refresh_from_db()
        self.assertEqual(self.cluster.operational_footprint_reason, FOOTPRINT_INVENTORY_BOOTSTRAP)
        self.assertIsNotNone(self.cluster.operational_footprint_at)
        self.assertTrue(unused_connection_deletion_eligibility(self.cluster).eligible)

    def test_it_reports_itself_in_recent_tasks(self):
        with patch(f"{_SERVICE}.async_task", return_value="task-1"):
            queue_cluster_inventory_bootstrap(cluster=self.cluster)

        rows = recent_task_page().tasks
        row = next(row for row in rows if row["action"] == CLUSTER_INVENTORY_BOOTSTRAP_ACTION)
        self.assertEqual(row["name"], "Add host/cluster to inventory")
        self.assertEqual(row["target"], "Alpha")
        self.assertEqual(row["status"], "Queued")
        self.assertFalse(row["cancelable"])


@override_settings(
    APP_REQUIRE_LOGIN=False,
    PVE_HELPER_ENCRYPTION_KEYS=f"test:{TEST_KEY}",
    PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID="test",
)
class ClusterAddQueuesFirstInventoryTests(TestCase):
    def setUp(self):
        RuntimeConfigurationState.objects.create(bootstrap_completed=True, identity_contract_version=1)
        self.certificate = InspectedCertificate(
            subject="CN=pve201.example.test",
            issuer="CN=Example CA",
            sha256_fingerprint="abc123",
        )
        self.identity = ObservedClusterIdentity(
            ca_uuid="22222222-2222-2222-2222-222222222222",
            ca_fingerprint="AA:22",
        )
        self.candidate = ClusterCandidate(
            key="clusterb",
            display_name="Cluster B",
            endpoint_url="https://pve201.example.test:8006",
            endpoint_name="pve201",
            trust_mode=TRUST_PUBLIC,
            token_id="pve-helper@pve!pve-helper",
            token_secret="never-render-this",
        )
        self.verified = VerifiedConnection(
            certificate=self.certificate,
            identity=self.identity,
            node_names=("pve201",),
            version="9.2.4",
            discovered_name="Cluster B",
            administrator_privileges=("VM.Audit", "VM.PowerMgmt"),
        )

    def _confirm(self):
        with patch("core.views.clusters.connections.inspect_transport", return_value=self.certificate):
            inspection = (
                self.client.post(
                    reverse("core:cluster_add"),
                    {
                        "action": "inspect",
                        "display_name": self.candidate.display_name,
                        "cluster_key": self.candidate.key,
                        "endpoint_url": self.candidate.endpoint_url,
                        "endpoint_name": self.candidate.endpoint_name,
                    },
                )
                .context["trust_form"]["inspection"]
                .value()
            )
        with patch(
            "core.views.clusters.connections.verify_new_cluster",
            return_value=(self.candidate, self.verified),
        ):
            token = (
                self.client.post(
                    reverse("core:cluster_add"),
                    {
                        "action": "verify",
                        "inspection": inspection,
                        "trust_mode": TRUST_PUBLIC,
                        "ca_pem": "",
                        "token_id": self.candidate.token_id,
                        "token_secret": self.candidate.token_secret,
                        "confirm_certificate": "on",
                    },
                )
                .context["confirm_form"]["candidate"]
                .value()
            )
            return self.client.post(
                reverse("core:cluster_add"),
                {"action": "confirm", "candidate": token, "confirm_identity": "on"},
            )

    def test_adding_a_connection_starts_its_first_inventory(self):
        with patch("core.services.cluster_inventory_bootstrap.async_task", return_value="task-1"):
            response = self._confirm()

        cluster = ProxmoxCluster.objects.get(key="clusterb")
        self.assertRedirects(
            response,
            reverse("core:cluster_connection", kwargs={"cluster_key": cluster.key}),
            fetch_redirect_response=False,
        )
        event = AuditEvent.objects.get(action=CLUSTER_INVENTORY_BOOTSTRAP_ACTION)
        self.assertEqual(event.outcome, "queued")
        self.assertEqual(event.cluster_id, cluster.pk)
        self.assertEqual(event.details["display_name"], "Cluster B")

    def test_a_dead_queue_does_not_undo_a_verified_connection(self):
        """The connection is persisted and verified; the queue is a separate fault."""
        with patch(
            "core.services.cluster_inventory_bootstrap.async_task",
            side_effect=RuntimeError("broker down"),
        ):
            response = self._confirm()

        cluster = ProxmoxCluster.objects.get(key="clusterb")
        self.assertRedirects(
            response,
            reverse("core:cluster_connection", kwargs={"cluster_key": cluster.key}),
            fetch_redirect_response=False,
        )
        self.assertEqual(AuditEvent.objects.get(action=CLUSTER_INVENTORY_BOOTSTRAP_ACTION).outcome, "failed")

from __future__ import annotations

import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    AuditEvent,
    ClusterStorage,
    ClusterStorageMount,
    ClusterStorageNodeState,
    ProxmoxCluster,
    ProxmoxStorageConsumer,
    ScanRun,
    StorageMount,
)


class StorageConsumerReleaseViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="operator", password="unused")
        self.client.force_login(self.user)
        self.cluster = ProxmoxCluster.objects.create(
            key="release-me",
            display_name="Release Me",
            enabled=False,
        )
        self.other_cluster = ProxmoxCluster.objects.create(
            key="keep-me",
            display_name="Keep Me",
            enabled=True,
        )
        self.mount_a = self._mount(
            self.cluster,
            storage_id="shared-a",
            storage_name="Shared Alpha",
            mount_key=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        )
        self.mount_b = self._mount(
            self.cluster,
            storage_id="shared-b",
            storage_name="Shared Beta",
            mount_key=uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        )
        observed_at = timezone.now().replace(microsecond=0)
        self.consumer_a = ProxmoxStorageConsumer.objects.create(
            storage=self.mount_a,
            cluster=self.cluster,
            expected_node_name="pve-a",
            last_successful_inventory_scan=observed_at,
            last_gate_status="ok",
        )
        self.consumer_b = ProxmoxStorageConsumer.objects.create(
            storage=self.mount_b,
            cluster=self.cluster,
            expected_node_name="pve-b",
            last_gate_status="unavailable",
        )
        self.other_consumer = ProxmoxStorageConsumer.objects.create(
            storage=self.mount_a,
            cluster=self.other_cluster,
            expected_node_name="pve-other",
            last_successful_inventory_scan=observed_at,
            last_gate_status="ok",
        )
        self.summary_url = reverse(
            "core:api_storage_summary",
            kwargs={"cluster_key": self.cluster.key, "storage": "shared-a"},
        )
        self.release_url = reverse(
            "core:release_cluster_storage_consumers",
            kwargs={"cluster_key": self.cluster.key},
        )

    def _mount(self, cluster, *, storage_id: str, storage_name: str, mount_key: uuid.UUID):
        definition = ClusterStorage.objects.create(
            cluster=cluster,
            storage_id=storage_id,
            storage_type="nfs",
            shared=True,
            present=True,
            content=["images"],
            config={"storage": storage_id},
        )
        ClusterStorageNodeState.objects.create(
            cluster_storage=definition,
            node="pve-a",
            present=True,
            active=True,
            enabled=True,
        )
        mount = StorageMount.objects.create(
            storage_id=storage_id,
            mount_key=mount_key,
            display_name=storage_name,
            path=f"/storages/{storage_id}",
            relative_path=storage_id,
        )
        ClusterStorageMount.objects.create(
            cluster_storage=definition,
            mount=mount,
            scope=ClusterStorageMount.Scope.SHARED,
        )
        return mount

    def _confirmation(self) -> str:
        response = self.client.get(self.summary_url)
        self.assertEqual(response.status_code, 200)
        return response.context["consumer_release"].confirmation

    def _post(self, confirmation: str, **overrides):
        payload = {
            "confirmation": confirmation,
            "confirm_release": "yes",
            "next": self.summary_url,
            **overrides,
        }
        return self.client.post(self.release_url, payload, follow=True)

    def test_summary_lists_the_exact_cluster_qualified_release_impact_and_consequence(self):
        response = self.client.get(self.summary_url)

        self.assertContains(response, "Release all consumers for Release Me")
        self.assertContains(response, "Shared Alpha")
        self.assertContains(response, "shared-a")
        self.assertContains(response, "pve-a")
        self.assertContains(response, "Shared Beta")
        self.assertContains(response, "shared-b")
        self.assertContains(response, "pve-b")
        rendered_observation = timezone.localtime(self.consumer_a.last_successful_inventory_scan)
        self.assertContains(response, rendered_observation.strftime("%Y-%m-%d %H:%M:%S"))
        self.assertContains(response, "Never")
        self.assertContains(response, "Consumers belonging to other managed clusters")
        self.assertContains(response, "Run retirement preflight again")
        self.assertContains(response, "Keep Me")
        self.assertContains(response, "pve-other")

        confirmation_body = (
            response.content.decode().split("<template data-confirm-body>", 1)[1].split("</template>", 1)[0]
        )
        self.assertNotIn("Keep Me", confirmation_body)
        self.assertNotIn("pve-other", confirmation_body)

    def test_release_is_post_only_and_requires_the_dialog_acknowledgement(self):
        self.assertEqual(self.client.get(self.release_url).status_code, 405)
        confirmation = self._confirmation()

        response = self._post(confirmation, confirm_release="")

        self.assertContains(response, "Review the exact storage consumer list")
        self.assertEqual(ProxmoxStorageConsumer.objects.filter(cluster=self.cluster).count(), 2)
        self.assertFalse(AuditEvent.objects.filter(action="storage.consumers.released").exists())

    def test_release_deletes_only_the_confirmed_clusters_consumers_without_provider_calls(self):
        confirmation = self._confirmation()

        with patch("core.services.cluster_resolver.client_for_endpoint") as provider_client:
            response = self._post(confirmation)

        self.assertEqual(response.status_code, 200)
        provider_client.assert_not_called()
        self.assertFalse(ProxmoxStorageConsumer.objects.filter(cluster=self.cluster).exists())
        self.assertTrue(ProxmoxStorageConsumer.objects.filter(pk=self.other_consumer.pk).exists())
        event = AuditEvent.objects.get(action="storage.consumers.released")
        self.assertEqual(event.cluster, self.cluster)
        self.assertEqual(event.cluster_key_snapshot, self.cluster.key)
        self.assertEqual(event.username, self.user.username)
        self.assertEqual(event.object_type, "cluster_storage_consumers")
        self.assertEqual(event.object_id, self.cluster.key)
        self.assertEqual(event.details["consumer_count"], 2)
        self.assertEqual(
            event.details["consumer_refs"],
            [
                f"mr1:{self.mount_a.mount_key}@nr1:{self.cluster.key}:pve-a",
                f"mr1:{self.mount_b.mount_key}@nr1:{self.cluster.key}:pve-b",
            ],
        )
        self.assertTrue(event.details["explicit_operator_resolution"])
        self.assertContains(response, "Keep Me")
        self.assertNotContains(response, "Release all consumers for Release Me")

    def test_a_changed_relationship_set_is_not_released_from_stale_confirmation(self):
        confirmation = self._confirmation()
        ProxmoxStorageConsumer.objects.create(
            storage=self.mount_b,
            cluster=self.cluster,
            expected_node_name="pve-new",
        )

        response = self._post(confirmation)

        self.assertContains(response, "relationships changed after they were shown")
        self.assertEqual(ProxmoxStorageConsumer.objects.filter(cluster=self.cluster).count(), 3)
        self.assertFalse(AuditEvent.objects.filter(action="storage.consumers.released").exists())

    def test_confirmation_is_bound_to_the_operator(self):
        confirmation = self._confirmation()
        other_user = get_user_model().objects.create_user(username="other-operator", password="unused")
        self.client.force_login(other_user)

        response = self._post(confirmation)

        self.assertContains(response, "belongs to another cluster or operator")
        self.assertEqual(ProxmoxStorageConsumer.objects.filter(cluster=self.cluster).count(), 2)

    def test_an_active_scan_blocks_release_without_partial_mutation(self):
        confirmation = self._confirmation()
        ScanRun.objects.create(status=ScanRun.Status.RUNNING, progress_message="Scanning")

        response = self._post(confirmation)

        self.assertContains(response, "while a storage scan is queued or running")
        self.assertEqual(ProxmoxStorageConsumer.objects.filter(cluster=self.cluster).count(), 2)
        self.assertFalse(AuditEvent.objects.filter(action="storage.consumers.released").exists())

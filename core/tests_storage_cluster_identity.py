from unittest.mock import patch

from django.test import TestCase

from core.models import ProxmoxCluster
from core.services.cluster_resolver import ClusterReadResult
from core.services.file_actions import ReferencedObject
from core.services.storage_actions import StorageActionError, _live_guest_status


class StorageFileGuestIdentityTests(TestCase):
    def setUp(self):
        self.cluster_a = ProxmoxCluster.objects.create(
            key="a", display_name="A", enabled=True
        )
        self.cluster_b = ProxmoxCluster.objects.create(
            key="b", display_name="B", enabled=True
        )

    def test_live_status_uses_the_referenced_cluster_when_vmid_and_node_overlap(self):
        guest = ReferencedObject(
            cluster_key="b",
            object_type="vm",
            vmid=500,
            name="overlap",
            node="pve1",
            status="stopped",
        )
        result = ClusterReadResult(
            cluster_key="b",
            value="stopped",
            answering_endpoint="b1",
            attempted=(),
            complete=True,
        )

        with patch(
            "core.services.storage_actions.cluster_wide_read", return_value=result
        ) as read:
            self.assertEqual(_live_guest_status(guest), "stopped")

        self.assertEqual(read.call_args.args[0], self.cluster_b)
        self.assertEqual(read.call_args.kwargs["operation"], "storage_file_guest_status")

    def test_unqualified_historical_reference_fails_closed(self):
        guest = ReferencedObject(
            cluster_key="",
            object_type="vm",
            vmid=500,
            name="legacy",
            node="pve1",
            status="stopped",
        )

        with (
            patch("core.services.storage_actions.cluster_wide_read") as read,
            self.assertRaises(StorageActionError),
        ):
            _live_guest_status(guest)

        read.assert_not_called()

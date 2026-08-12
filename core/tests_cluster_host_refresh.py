from __future__ import annotations

from datetime import timedelta
from threading import Event, Thread
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, ProxmoxCluster
from core.services.cluster_host_refresh import (
    CLUSTER_HOST_REFRESH_ACTION,
    HOST_REFRESH_SCOPE_MEMBERSHIP,
    HOST_REFRESH_SCOPE_NODE_RUNTIME,
    ClusterHostRefreshAlreadyActive,
    ClusterHostRefreshQueueError,
    ClusterHostRefreshRetryError,
    execute_cluster_host_refresh,
    queue_cluster_host_refresh,
    retry_cluster_host_refresh,
)
from core.services.cluster_membership import MembershipRefreshResult
from core.services.cluster_node_runtime import NodeRuntimeResult
from core.services.cluster_state_identity import cluster_advisory_lock_id
from core.services.host_projection_singleflight import HOST_PROJECTION_REFRESH_LOCK_ID
from core.services.recent_tasks import recent_task_page
from core.tasks import reap_stale_guest_tasks


def _cluster(key: str = "clusterhq") -> ProxmoxCluster:
    return ProxmoxCluster.objects.create(key=key, display_name=key.upper(), enabled=True)


class ClusterHostRefreshQueueTests(TestCase):
    def setUp(self):
        self.cluster = _cluster()

    @patch("core.services.cluster_host_refresh.async_task", return_value="worker-1")
    def test_row_exists_before_enqueue_and_carries_exact_membership_scope(self, enqueue):
        seen = {}

        def inspect_row(*args, **kwargs):
            event_id = args[1]
            seen["row"] = AuditEvent.objects.get(pk=event_id).outcome
            return "worker-1"

        enqueue.side_effect = inspect_row
        event, task_id = queue_cluster_host_refresh(cluster=self.cluster, scope=HOST_REFRESH_SCOPE_MEMBERSHIP)

        self.assertEqual(seen["row"], "queued")
        self.assertEqual(task_id, "worker-1")
        self.assertEqual(event.details["scope"], HOST_REFRESH_SCOPE_MEMBERSHIP)
        self.assertEqual(event.details["node_ref"], "")
        self.assertEqual(event.details["worker_task_id"], "worker-1")

    @patch("core.services.cluster_host_refresh.async_task", return_value="worker-1")
    def test_runtime_scope_persists_a_cluster_qualified_node_ref(self, _enqueue):
        event, _ = queue_cluster_host_refresh(
            cluster=self.cluster,
            scope=HOST_REFRESH_SCOPE_NODE_RUNTIME,
            node_name="pve1",
        )

        self.assertEqual(event.object_id, "nr1:clusterhq:pve1")
        self.assertEqual(event.details["node_ref"], "nr1:clusterhq:pve1")

    @patch("core.services.cluster_host_refresh.async_task", return_value="worker-1")
    def test_scope_validation_rejects_fanout_and_bare_runtime_target(self, enqueue):
        with self.assertRaises(ClusterHostRefreshQueueError):
            queue_cluster_host_refresh(cluster=self.cluster, scope="maintenance")
        with self.assertRaises(ClusterHostRefreshQueueError):
            queue_cluster_host_refresh(cluster=self.cluster, scope=HOST_REFRESH_SCOPE_NODE_RUNTIME)
        with self.assertRaises(ClusterHostRefreshQueueError):
            queue_cluster_host_refresh(
                cluster=self.cluster,
                scope=HOST_REFRESH_SCOPE_MEMBERSHIP,
                node_name="pve1",
            )

        enqueue.assert_not_called()
        self.assertFalse(AuditEvent.objects.filter(action=CLUSTER_HOST_REFRESH_ACTION).exists())

    @patch("core.services.cluster_host_refresh.async_task", side_effect=RuntimeError("broker secret"))
    def test_enqueue_failure_is_retained_and_retryable_without_raw_error(self, _enqueue):
        with self.assertRaises(ClusterHostRefreshQueueError):
            queue_cluster_host_refresh(cluster=self.cluster, scope=HOST_REFRESH_SCOPE_MEMBERSHIP)

        event = AuditEvent.objects.get(action=CLUSTER_HOST_REFRESH_ACTION)
        self.assertEqual(event.outcome, "failed")
        self.assertTrue(event.details["retryable"])
        self.assertNotIn("broker secret", str(event.details))

    @patch("core.services.cluster_host_refresh.async_task", return_value="worker-1")
    def test_duplicate_click_is_refused_for_the_exact_active_scope(self, enqueue):
        queue_cluster_host_refresh(cluster=self.cluster, scope=HOST_REFRESH_SCOPE_MEMBERSHIP)

        with self.assertRaises(ClusterHostRefreshAlreadyActive):
            queue_cluster_host_refresh(cluster=self.cluster, scope=HOST_REFRESH_SCOPE_MEMBERSHIP)

        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(AuditEvent.objects.filter(action=CLUSTER_HOST_REFRESH_ACTION).count(), 1)

    @patch("core.services.cluster_host_refresh.async_task", return_value="worker-1")
    def test_disabled_or_retired_cluster_gets_no_row_and_no_worker(self, enqueue):
        self.cluster.enabled = False
        self.cluster.save(update_fields=["enabled"])
        with self.assertRaises(ClusterHostRefreshQueueError):
            queue_cluster_host_refresh(cluster=self.cluster, scope=HOST_REFRESH_SCOPE_MEMBERSHIP)

        self.cluster.enabled = False
        self.cluster.retired_at = timezone.now()
        self.cluster.retirement_mode = ProxmoxCluster.RetirementMode.FORCED
        self.cluster.save(update_fields=["enabled", "retired_at", "retirement_mode"])
        with self.assertRaises(ClusterHostRefreshQueueError):
            queue_cluster_host_refresh(cluster=self.cluster, scope=HOST_REFRESH_SCOPE_MEMBERSHIP)

        enqueue.assert_not_called()
        self.assertFalse(AuditEvent.objects.filter(action=CLUSTER_HOST_REFRESH_ACTION).exists())


class ClusterHostRefreshWorkerTests(TestCase):
    def setUp(self):
        self.cluster = _cluster()

    def _queued(self, *, scope=HOST_REFRESH_SCOPE_MEMBERSHIP, node_name=""):
        with patch("core.services.cluster_host_refresh.async_task", return_value="worker-1"):
            return queue_cluster_host_refresh(cluster=self.cluster, scope=scope, node_name=node_name)[0]

    @patch("core.services.cluster_host_refresh.refresh_cluster_membership")
    def test_membership_calls_one_publisher_and_finishes_the_same_row(self, refresh):
        refresh.return_value = MembershipRefreshResult("clusterhq", 7, True, "")
        event = self._queued()

        execute_cluster_host_refresh(event.id, 0)

        event.refresh_from_db()
        refresh.assert_called_once_with(self.cluster)
        self.assertEqual(event.outcome, "success")
        self.assertEqual(event.details["generation"], 7)
        self.assertIn("heartbeat_at", event.details)
        self.assertIn("finished_at", event.details)

    @patch("core.services.cluster_host_refresh.refresh_node_runtime")
    def test_runtime_calls_only_the_exact_per_node_seam(self, refresh):
        refresh.return_value = NodeRuntimeResult("pve1", True, "", 9, based_on_generation=8)
        event = self._queued(scope=HOST_REFRESH_SCOPE_NODE_RUNTIME, node_name="pve1")

        execute_cluster_host_refresh(event.id, 0)

        event.refresh_from_db()
        refresh.assert_called_once_with(self.cluster, "pve1")
        self.assertEqual(event.outcome, "success")
        self.assertEqual(event.details["based_on_generation"], 8)

    @patch("core.services.cluster_host_refresh.refresh_cluster_membership")
    def test_incomplete_publication_fails_with_stable_code_and_is_retryable(self, refresh):
        refresh.return_value = MembershipRefreshResult("clusterhq", 3, False, "provider_timeout")
        event = self._queued()

        execute_cluster_host_refresh(event.id, 0)

        event.refresh_from_db()
        self.assertEqual(event.outcome, "failed")
        self.assertEqual(event.details["coverage_error"], "provider_timeout")
        self.assertTrue(event.details["retryable"])

    @patch("core.services.cluster_host_refresh.refresh_cluster_membership", side_effect=RuntimeError("token secret"))
    def test_unhandled_failure_exposes_only_a_public_message(self, _refresh):
        event = self._queued()

        execute_cluster_host_refresh(event.id, 0)

        event.refresh_from_db()
        self.assertEqual(event.outcome, "failed")
        self.assertNotIn("token secret", str(event.details))
        self.assertTrue(event.details["retryable"])

    @patch("core.services.cluster_host_refresh.refresh_cluster_membership")
    def test_old_attempt_is_a_noop_after_retry(self, refresh):
        event = self._queued()
        event.outcome = "failed"
        event.details = {**event.details, "retryable": True}
        event.save(update_fields=["outcome", "details"])
        with patch("core.services.cluster_host_refresh.async_task", return_value="worker-2"):
            retry_cluster_host_refresh(event.id)

        execute_cluster_host_refresh(event.id, 0)

        event.refresh_from_db()
        refresh.assert_not_called()
        self.assertEqual(event.outcome, "queued")
        self.assertEqual(event.details["attempt"], 1)

    @patch("core.services.cluster_host_refresh.refresh_cluster_membership")
    def test_retired_or_disabled_after_queue_is_terminal_without_provider_call(self, refresh):
        retired = self._queued()
        ProxmoxCluster.objects.filter(pk=self.cluster.pk).update(
            enabled=False,
            retired_at=timezone.now(),
            retirement_mode=ProxmoxCluster.RetirementMode.FORCED,
        )
        execute_cluster_host_refresh(retired.id, 0)
        retired.refresh_from_db()
        self.assertEqual(retired.outcome, "failed")
        self.assertFalse(retired.details["retryable"])

        other = _cluster("clusterb")
        with patch("core.services.cluster_host_refresh.async_task", return_value="worker-2"):
            disabled, _ = queue_cluster_host_refresh(cluster=other, scope=HOST_REFRESH_SCOPE_MEMBERSHIP)
        ProxmoxCluster.objects.filter(pk=other.pk).update(enabled=False)
        execute_cluster_host_refresh(disabled.id, 0)
        disabled.refresh_from_db()
        self.assertEqual(disabled.outcome, "failed")
        self.assertFalse(disabled.details["retryable"])
        refresh.assert_not_called()

    @patch("core.services.cluster_host_refresh.refresh_cluster_membership")
    def test_busy_shared_singleflight_is_terminal_retryable_without_provider_call(self, refresh):
        event = self._queued()
        with patch("core.services.cluster_host_refresh.host_projection_refresh_lock") as lock:
            lock.return_value.__enter__.return_value = False
            execute_cluster_host_refresh(event.id, 0)

        event.refresh_from_db()
        self.assertEqual(event.outcome, "failed")
        self.assertEqual(event.details["stage"], "blocked")
        self.assertTrue(event.details["retryable"])
        refresh.assert_not_called()


class ClusterHostRefreshRetryAndReaperTests(TestCase):
    def setUp(self):
        self.cluster = _cluster()
        with patch("core.services.cluster_host_refresh.async_task", return_value="worker-1"):
            self.event, _ = queue_cluster_host_refresh(cluster=self.cluster, scope=HOST_REFRESH_SCOPE_MEMBERSHIP)

    def _fail(self):
        self.event.outcome = "failed"
        self.event.details = {**self.event.details, "stage": "interrupted", "retryable": True}
        self.event.save(update_fields=["outcome", "details"])

    @patch("core.services.cluster_host_refresh.async_task", return_value="worker-2")
    def test_retry_reuses_identity_and_increments_attempt(self, enqueue):
        self._fail()

        task_id = retry_cluster_host_refresh(self.event.id)

        self.event.refresh_from_db()
        self.assertEqual(task_id, "worker-2")
        self.assertEqual(self.event.outcome, "queued")
        self.assertEqual(self.event.details["attempt"], 1)
        self.assertEqual(AuditEvent.objects.filter(action=CLUSTER_HOST_REFRESH_ACTION).count(), 1)
        self.assertEqual(enqueue.call_args.args[1:], (self.event.id, 1))

    def test_retry_refuses_a_non_retryable_row(self):
        self.event.outcome = "failed"
        self.event.details = {**self.event.details, "retryable": False}
        self.event.save(update_fields=["outcome", "details"])

        with self.assertRaises(ClusterHostRefreshRetryError):
            retry_cluster_host_refresh(self.event.id)

    def test_reaper_resolves_lost_queued_and_running_rows_as_retryable(self):
        old = (timezone.now() - timedelta(minutes=30)).isoformat()
        self.event.details = {**self.event.details, "queued_at": old, "worker_task_id": "lost-worker"}
        self.event.save(update_fields=["details"])

        result = reap_stale_guest_tasks()

        self.event.refresh_from_db()
        self.assertEqual(result["interrupted_host_projection_refreshes"], 1)
        self.assertEqual(self.event.outcome, "failed")
        self.assertTrue(self.event.details["retryable"])

        with patch("core.services.cluster_host_refresh.async_task", return_value="worker-2"):
            running, _ = queue_cluster_host_refresh(
                cluster=self.cluster, scope=HOST_REFRESH_SCOPE_NODE_RUNTIME, node_name="pve1"
            )
        running.outcome = "running"
        running.details = {**running.details, "heartbeat_at": old, "worker_task_id": "lost-running"}
        running.save(update_fields=["outcome", "details"])

        result = reap_stale_guest_tasks()
        running.refresh_from_db()
        self.assertEqual(result["interrupted_host_projection_refreshes"], 1)
        self.assertEqual(running.outcome, "failed")
        self.assertTrue(running.details["retryable"])

    @patch("core.tasks.queued_task_ids", return_value={"worker-1"})
    def test_reaper_keeps_a_stale_row_whose_task_is_still_in_the_broker(self, _queued):
        self.event.details = {
            **self.event.details,
            "queued_at": (timezone.now() - timedelta(minutes=30)).isoformat(),
        }
        self.event.save(update_fields=["details"])

        result = reap_stale_guest_tasks()

        self.event.refresh_from_db()
        self.assertEqual(result["interrupted_host_projection_refreshes"], 0)
        self.assertEqual(self.event.outcome, "queued")

    def test_fresh_heartbeat_is_not_reaped(self):
        self.event.outcome = "running"
        self.event.details = {**self.event.details, "heartbeat_at": timezone.now().isoformat()}
        self.event.save(update_fields=["outcome", "details"])

        result = reap_stale_guest_tasks()

        self.event.refresh_from_db()
        self.assertEqual(result["interrupted_host_projection_refreshes"], 0)
        self.assertEqual(self.event.outcome, "running")

    @patch("core.services.cluster_host_refresh.async_task", return_value="worker-2")
    def test_recent_tasks_exposes_retry_and_the_shared_retry_endpoint(self, _enqueue):
        self._fail()
        task = next(task for task in recent_task_page(limit=20).tasks if task["action"] == CLUSTER_HOST_REFRESH_ACTION)
        self.assertEqual(task["id"], f"host_projection:{self.event.id}")
        self.assertTrue(task["retryable"])

        user = get_user_model().objects.create_user(username="operator", password="pw")
        self.client.force_login(user)
        response = self.client.post(reverse("core:retry_recent_task"), {"task_id": task["id"]})

        self.assertEqual(response.status_code, 202)
        self.event.refresh_from_db()
        self.assertEqual(self.event.details["attempt"], 1)


class ClusterHostRefreshSingleFlightTests(TransactionTestCase):
    def test_running_and_heartbeat_commit_before_the_provider_call(self):
        cluster = _cluster()
        with patch("core.services.cluster_host_refresh.async_task", return_value="worker-1"):
            event, _ = queue_cluster_host_refresh(cluster=cluster, scope=HOST_REFRESH_SCOPE_MEMBERSHIP)
        observed = []

        def observe_from_another_connection(_cluster):
            def read_row():
                try:
                    current = AuditEvent.objects.get(pk=event.pk)
                    observed.append((current.outcome, "heartbeat_at" in current.details))
                finally:
                    close_old_connections()

            reader = Thread(target=read_row)
            reader.start()
            reader.join(timeout=10)
            return MembershipRefreshResult("clusterhq", 4, True, "")

        with patch(
            "core.services.cluster_host_refresh.refresh_cluster_membership", side_effect=observe_from_another_connection
        ):
            execute_cluster_host_refresh(event.id, 0)

        self.assertEqual(observed, [("running", True)])
        event.refresh_from_db()
        self.assertEqual(event.outcome, "success")

    def test_worker_death_leaves_the_committed_running_row_for_the_reaper(self):
        cluster = _cluster()
        with patch("core.services.cluster_host_refresh.async_task", return_value="worker-1"):
            event, _ = queue_cluster_host_refresh(cluster=cluster, scope=HOST_REFRESH_SCOPE_MEMBERSHIP)

        with patch("core.services.cluster_host_refresh.refresh_cluster_membership", side_effect=SystemExit("dead")):
            with self.assertRaises(SystemExit):
                execute_cluster_host_refresh(event.id, 0)

        event.refresh_from_db()
        self.assertEqual(event.outcome, "running")
        self.assertIn("heartbeat_at", event.details)

    def test_manual_refresh_uses_the_same_lock_as_periodic_refresh(self):
        cluster = _cluster()
        with patch("core.services.cluster_host_refresh.async_task", return_value="worker-1"):
            event, _ = queue_cluster_host_refresh(cluster=cluster, scope=HOST_REFRESH_SCOPE_MEMBERSHIP)
        lock_id = cluster_advisory_lock_id(HOST_PROJECTION_REFRESH_LOCK_ID, cluster)
        held = Event()
        release = Event()

        def hold_periodic_lock():
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_lock(%s)", [lock_id])
                held.set()
                release.wait(timeout=10)
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])
            finally:
                close_old_connections()

        holder = Thread(target=hold_periodic_lock)
        holder.start()
        try:
            self.assertTrue(held.wait(timeout=10))
            with patch("core.services.cluster_host_refresh.refresh_cluster_membership") as refresh:
                execute_cluster_host_refresh(event.id, 0)
        finally:
            release.set()
            holder.join(timeout=10)

        event.refresh_from_db()
        self.assertEqual(event.outcome, "failed")
        self.assertEqual(event.details["stage"], "blocked")
        refresh.assert_not_called()

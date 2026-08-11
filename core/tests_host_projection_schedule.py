from __future__ import annotations

from threading import Event, Thread
from unittest.mock import patch

from django.conf import settings
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase
from django_q.models import Schedule

from core.models import ProxmoxCluster, ProxmoxEndpoint
from core.services.cluster_membership import MembershipRefreshResult
from core.services.cluster_node_runtime import NodeRuntimeSweepResult
from core.services.cluster_state_identity import cluster_advisory_lock_id
from core.services.host_projection_refresh_schedule import (
    HOST_PROJECTION_REFRESH_FUNC,
    HOST_PROJECTION_REFRESH_SCHEDULE_NAME,
    ensure_host_projection_refresh_schedule,
)
from core.tasks import HOST_PROJECTION_REFRESH_LOCK_ID, refresh_cluster_host_projection


def _cluster(key: str) -> ProxmoxCluster:
    cluster = ProxmoxCluster.objects.create(key=key, display_name=key)
    ProxmoxEndpoint.objects.create(cluster=cluster, name=f"{key}-one", url=f"https://{key}.test:8006")
    return cluster


class HostProjectionScheduleTests(TestCase):
    def test_schedule_is_registered_at_the_configured_cadence(self):
        ensure_host_projection_refresh_schedule()

        schedule = Schedule.objects.get(name=HOST_PROJECTION_REFRESH_SCHEDULE_NAME)
        self.assertEqual(schedule.func, HOST_PROJECTION_REFRESH_FUNC)
        self.assertEqual(schedule.minutes, settings.HOST_PROJECTION_REFRESH_INTERVAL_MINUTES)
        self.assertEqual(schedule.repeats, -1)

    def test_registration_is_idempotent(self):
        ensure_host_projection_refresh_schedule()
        ensure_host_projection_refresh_schedule()

        self.assertEqual(Schedule.objects.filter(name=HOST_PROJECTION_REFRESH_SCHEDULE_NAME).count(), 1)


class HostProjectionOrchestrationTests(TestCase):
    def setUp(self):
        self.cluster = _cluster("clusterhq")

    def _run(self, *, membership=None, runtime=None):
        membership = membership or MembershipRefreshResult("clusterhq", 1, True, "")
        runtime = runtime or NodeRuntimeSweepResult("clusterhq", True, "")
        with patch("core.tasks.refresh_cluster_membership", return_value=membership) as membership_mock:
            with patch("core.tasks.refresh_cluster_node_runtime", return_value=runtime) as runtime_mock:
                result = refresh_cluster_host_projection()
        return result, membership_mock, runtime_mock

    def test_membership_runs_before_runtime(self):
        order: list[str] = []
        with patch(
            "core.tasks.refresh_cluster_membership",
            side_effect=lambda c: order.append("membership") or MembershipRefreshResult("clusterhq", 1, True, ""),
        ):
            with patch(
                "core.tasks.refresh_cluster_node_runtime",
                side_effect=lambda c: order.append("runtime") or NodeRuntimeSweepResult("clusterhq", True, ""),
            ):
                refresh_cluster_host_projection()

        self.assertEqual(order, ["membership", "runtime"])

    def test_a_failed_membership_refresh_does_not_skip_runtime(self):
        """Refusing runtime on a membership failure would turn one failed call
        into a blackout of node facts that are still current."""
        _, _, runtime_mock = self._run(membership=MembershipRefreshResult("clusterhq", 1, False, "provider_timeout"))

        self.assertEqual(runtime_mock.call_count, 1)

    def test_a_raising_domain_does_not_stop_the_other(self):
        with patch("core.tasks.refresh_cluster_membership", side_effect=RuntimeError("boom")):
            with patch(
                "core.tasks.refresh_cluster_node_runtime",
                return_value=NodeRuntimeSweepResult("clusterhq", True, ""),
            ) as runtime_mock:
                result = refresh_cluster_host_projection()

        self.assertEqual(runtime_mock.call_count, 1)
        self.assertEqual(result["clusters"][0]["membership_error"], "unhandled")

    def test_one_cluster_failure_never_stops_a_sibling(self):
        _cluster("clusterb")

        def membership_for(cluster):
            if cluster.key == "clusterhq":
                raise RuntimeError("boom")
            return MembershipRefreshResult(cluster.key, 1, True, "")

        with patch("core.tasks.refresh_cluster_membership", side_effect=membership_for):
            with patch(
                "core.tasks.refresh_cluster_node_runtime",
                return_value=NodeRuntimeSweepResult("x", True, ""),
            ):
                result = refresh_cluster_host_projection()

        keys = {row["cluster_key"] for row in result["clusters"]}
        self.assertEqual(keys, {"clusterhq", "clusterb"})
        self.assertFalse(any(row.get("skipped") for row in result["clusters"]))

    def test_a_disabled_cluster_is_not_walked(self):
        ProxmoxCluster.objects.filter(pk=self.cluster.pk).update(enabled=False)

        result, membership_mock, _ = self._run()

        self.assertEqual(result["clusters"], [])
        self.assertEqual(membership_mock.call_count, 0)


class HostProjectionSingleFlightTests(TransactionTestCase):
    """Single-flight is load-bearing: 5a1C's sweep can outlast the cadence, and
    stacked cycles would exhaust the worker pool.

    This needs a genuinely separate connection. PostgreSQL advisory locks are
    re-entrant within one session, so a test that takes the lock on the same
    connection the task uses proves nothing -- `pg_try_advisory_lock` succeeds
    and the skip path is never exercised.
    """

    def test_a_cluster_held_by_another_process_is_skipped_not_queued(self):
        cluster = _cluster("clusterhq")
        lock_id = cluster_advisory_lock_id(HOST_PROJECTION_REFRESH_LOCK_ID, cluster)
        held = Event()
        release = Event()

        def hold_the_lock():
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_lock(%s)", [lock_id])
                held.set()
                release.wait(timeout=10)
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])
            finally:
                close_old_connections()

        holder = Thread(target=hold_the_lock)
        holder.start()
        try:
            self.assertTrue(held.wait(timeout=10))
            with patch("core.tasks.refresh_cluster_membership") as membership_mock:
                with patch("core.tasks.refresh_cluster_node_runtime") as runtime_mock:
                    result = refresh_cluster_host_projection()
        finally:
            release.set()
            holder.join(timeout=10)

        self.assertTrue(result["clusters"][0]["skipped"])
        self.assertEqual(result["clusters"][0]["reason"], "refresh already running")
        self.assertEqual(membership_mock.call_count, 0)
        self.assertEqual(runtime_mock.call_count, 0)

    def test_the_lock_is_released_for_the_next_cycle(self):
        cluster = _cluster("clusterb")

        with patch(
            "core.tasks.refresh_cluster_membership", return_value=MembershipRefreshResult("clusterb", 1, True, "")
        ):
            with patch(
                "core.tasks.refresh_cluster_node_runtime", return_value=NodeRuntimeSweepResult("clusterb", True, "")
            ):
                refresh_cluster_host_projection()
                second = refresh_cluster_host_projection()

        self.assertFalse(second["clusters"][0]["skipped"])
        self.assertEqual(
            ProxmoxCluster.objects.filter(key=cluster.key).count(),
            1,
            "sanity: the fixture cluster is the one that was walked",
        )

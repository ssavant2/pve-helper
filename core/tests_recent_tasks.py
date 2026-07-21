"""The Recent Tasks index must agree with composing the whole window in Python.

`recent_task_page` orders and slices five heterogeneous sources in SQL and then
hydrates only the rows it kept. That is worth doing — the old version built a
display dict for every operation in the last hour to show five of them, on every
page load, every dialog fragment and every 10 s poll from every open tab — but it
splits one algorithm across two languages, and the halves can drift silently.

So the tests here are mostly one test: for a corpus that exercises every source
and every question shape, the page, the total and the pending-question count must
equal what a naive compose-everything-then-sort reference produces. The reference
is deliberately written out longhand rather than imported, because a shared helper
would drift along with the thing it is supposed to pin.

The corpus is built to break the obvious shortcuts:

* a catalog refresh whose `details["started_at"]` is far later than its
  `timestamp`, so ordering by the natural column puts it in the wrong place;
* a scan and a scheduled run that sort on a fallback column rather than their
  primary one;
* an inflate that a later terminal event supersedes, beside one that nothing does;
* force-stop offers that are open, dismissed, reaper-resolved, and missing the
  target they would act on — only the first is a question;
* events attributed by FK, by snapshot string, and not at all.
"""

from __future__ import annotations

from datetime import timedelta

from django.db import connection
from django.db.models import Q
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.context_processors import app_settings
from core.models import (
    AuditEvent,
    ProxmoxCluster,
    ScanRun,
    ScheduledAction,
    ScheduledActionRun,
    StorageMount,
)
from core.services.recent_tasks import (
    BULK_FILE_ACTION,
    FILE_TASK_ACTIONS,
    INFLATE_QUEUED_ACTION,
    INFLATE_TERMINAL_ACTIONS,
    RECENT_TASK_RETENTION_MINUTES,
    STORAGE_CATALOG_REFRESH_ACTION,
    TAG_TASK_ACTIONS,
    _catalog_refresh_task,
    _file_task,
    _guest_task,
    _open_force_stop_question_q,
    _scan_initiators,
    _scan_task,
    _scheduled_action_task,
    _unanswered_question_q,
    recent_task_page,
)


def _reference_page(page: int = 0, limit: int = 5, *, cluster_key: str = ""):
    """Recent Tasks the slow, obvious way: build everything, sort, slice.

    This is the algorithm `recent_task_page` used to be, with one deliberate
    change: pinned questions sort first on *every* page rather than only on page 0.
    The old placement meant a pinned task appeared at the top of page 0 and again
    in its chronological position on page 1, because page 1 sliced a list the
    pinning had never been applied to.
    """
    cutoff = timezone.now() - timedelta(minutes=RECENT_TASK_RETENTION_MINUTES)
    entries: list[tuple[bool, object, int, dict]] = []

    scan_terminal = [ScanRun.Status.COMPLETED, ScanRun.Status.FAILED, ScanRun.Status.CANCELLED]
    scans = list(ScanRun.objects.exclude(Q(status__in=scan_terminal) & Q(finished_at__lte=cutoff)))
    initiators = _scan_initiators(scans)
    for scan in scans:
        entries.append(_entry(_scan_task(scan, initiators.get(str(scan.id), "system")), scan.id))

    for event in _reference_file_events(cutoff):
        entries.append(_entry(_file_task(event), event.id))

    catalog = AuditEvent.objects.filter(action=STORAGE_CATALOG_REFRESH_ACTION).filter(
        Q(timestamp__gte=cutoff) | Q(outcome__in=("queued", "running"))
    )
    for event in catalog:
        entries.append(_entry(_catalog_refresh_task(event), event.id))

    run_terminal = [
        ScheduledActionRun.Status.COMPLETED,
        ScheduledActionRun.Status.FAILED,
        ScheduledActionRun.Status.SKIPPED,
        ScheduledActionRun.Status.MISSED,
        ScheduledActionRun.Status.TIMEOUT,
        ScheduledActionRun.Status.STALE,
        ScheduledActionRun.Status.CANCELLED,
    ]
    runs = ScheduledActionRun.objects.exclude(Q(status__in=run_terminal) & Q(finished_at__lte=cutoff))
    for run in runs:
        entries.append(_entry(_scheduled_action_task(run), run.id))

    guests = AuditEvent.objects.filter(Q(action__startswith="guest.") | Q(action__in=TAG_TASK_ACTIONS)).filter(
        Q(timestamp__gte=cutoff) | (_open_force_stop_question_q() & _unanswered_question_q())
    )
    for event in guests:
        entries.append(_entry(_guest_task(event), event.id))

    if cluster_key:
        entries = [entry for entry in entries if entry[3].get("cluster_key") in {"", cluster_key}]

    entries.sort(key=lambda entry: (entry[0], entry[1], entry[2]), reverse=True)
    offset = page * limit
    return {
        "tasks": [entry[3] for entry in entries[offset : offset + limit]],
        "total": len(entries),
        "questions_pending": sum(1 for entry in entries if entry[0]),
    }


def _entry(task: dict, pk: int) -> tuple[bool, object, int, dict]:
    return (bool(task.get("question")), task.get("started_at") or task.get("sort_at"), pk, task)


def _reference_file_events(cutoff):
    """The file half, including the inflate de-duplication done in Python.

    Kept verbatim from the version that ran in the service, so that the `Exists`
    subquery replacing it has something independent to be wrong against.
    """
    events = list(
        AuditEvent.objects.filter(action__in=FILE_TASK_ACTIONS).filter(
            Q(timestamp__gte=cutoff) | (Q(action=BULK_FILE_ACTION, details__question=True) & _unanswered_question_q())
        )
    )
    terminal = [event for event in events if event.action in INFLATE_TERMINAL_ACTIONS]

    def key(event):
        details = event.details if isinstance(event.details, dict) else {}
        return (
            event.storage_id or details.get("storage_id"),
            event.path or details.get("path") or event.object_id,
            event.target_preallocation or details.get("target_preallocation"),
        )

    return [
        event
        for event in events
        if event.action != INFLATE_QUEUED_ACTION
        or not any(key(other) == key(event) and other.timestamp >= event.timestamp for other in terminal)
    ]


TASK_SOURCE_TABLES = ("core_auditevent", "core_scanrun", "core_scheduledactionrun")


def _task_source_queries(captured) -> list[str]:
    return [
        query["sql"]
        for query in captured.captured_queries
        if any(table in query["sql"] for table in TASK_SOURCE_TABLES)
    ]


class RecentTaskContextLazinessTests(TestCase):
    """A response that never renders a taskbar must not compose one.

    `app_settings` runs for every HTML response, and the guest and storage dialogs
    render their fragments with `render_to_string(..., request=request)` — so they
    inherit this context and used to pay for a task page no fragment displays.
    """

    def test_the_task_page_is_not_composed_until_a_template_asks_for_it(self):
        request = RequestFactory().get("/")

        with CaptureQueriesContext(connection) as composing:
            context = app_settings(request)
        self.assertEqual(_task_source_queries(composing), [])

        with CaptureQueriesContext(connection) as reading:
            self.assertEqual(context["app_recent_tasks_page"].total, 0)
        self.assertNotEqual(_task_source_queries(reading), [])

    def test_the_task_list_resolves_to_the_same_page(self):
        """Laziness must not turn into two independently composed pages."""
        AuditEvent.objects.create(
            action="guest.power.start",
            object_type="guest",
            object_id="vm:1",
            details={"target_type": "vm", "vmid": 1},
        )
        context = app_settings(RequestFactory().get("/"))
        self.assertEqual(
            [task["id"] for task in context["app_recent_tasks"]],
            [task["id"] for task in context["app_recent_tasks_page"].tasks],
        )


class RecentTaskIndexParityTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.cluster_a = ProxmoxCluster.objects.create(key="alpha", display_name="Alpha", enabled=True)
        self.cluster_b = ProxmoxCluster.objects.create(key="beta", display_name="Beta", enabled=True)
        self.storage = StorageMount.objects.create(
            storage_id="nfs-vm", display_name="nfs-vm", relative_path="nfs-vm", backend_identity="nfs-vm"
        )
        self._build_scans()
        self._build_scheduled_runs()
        self._build_catalog_refreshes()
        self._build_file_events()
        self._build_guest_events()

    # --- corpus ---------------------------------------------------------------

    def _ago(self, minutes: int):
        return self.now - timedelta(minutes=minutes)

    def _scan(self, *, status, started_at=None, finished_at=None, created_minutes: int):
        scan = ScanRun.objects.create(status=status, started_at=started_at, finished_at=finished_at)
        ScanRun.objects.filter(pk=scan.pk).update(created_at=self._ago(created_minutes))
        scan.refresh_from_db()
        return scan

    def _build_scans(self):
        # Sorts on `started_at`, which is 40 minutes away from `created_at`.
        self._scan(status=ScanRun.Status.RUNNING, started_at=self._ago(7), created_minutes=47)
        # Sorts on `created_at` because it never started.
        self._scan(status=ScanRun.Status.QUEUED, created_minutes=13)
        self._scan(
            status=ScanRun.Status.COMPLETED,
            started_at=self._ago(23),
            finished_at=self._ago(21),
            created_minutes=24,
        )
        # Terminal and outside the window: invisible to both implementations.
        self._scan(
            status=ScanRun.Status.COMPLETED,
            started_at=self._ago(400),
            finished_at=self._ago(399),
            created_minutes=401,
        )

    def _build_scheduled_runs(self):
        action = ScheduledAction.objects.create(
            name="Night shutdown",
            action_type=ScheduledAction.ActionType.SHUTDOWN,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=500,
            cluster=self.cluster_a,
        )
        neutral = ScheduledAction.objects.create(
            name="Legacy start",
            action_type=ScheduledAction.ActionType.START,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=501,
        )
        # Queued: sorts on `created_at`, the last fallback.
        run = ScheduledActionRun.objects.create(
            scheduled_action=action,
            planned_for=self._ago(11),
            occurrence_key="queued-1",
            status=ScheduledActionRun.Status.QUEUED,
        )
        ScheduledActionRun.objects.filter(pk=run.pk).update(created_at=self._ago(11))
        # Completed: sorts on `started_at`, not on the much later `finished_at`
        # and not on the much earlier `created_at` it sat queued from.
        done = ScheduledActionRun.objects.create(
            scheduled_action=neutral,
            planned_for=self._ago(30),
            occurrence_key="done-1",
            status=ScheduledActionRun.Status.COMPLETED,
            started_at=self._ago(29),
            finished_at=self._ago(2),
        )
        ScheduledActionRun.objects.filter(pk=done.pk).update(created_at=self._ago(58))
        # Skipped without ever starting: the only row whose sort key is the middle
        # fallback, `finished_at`.
        skipped = ScheduledActionRun.objects.create(
            scheduled_action=action,
            planned_for=self._ago(56),
            occurrence_key="skipped-1",
            status=ScheduledActionRun.Status.SKIPPED,
            finished_at=self._ago(5),
        )
        ScheduledActionRun.objects.filter(pk=skipped.pk).update(created_at=self._ago(57))
        ScheduledActionRun.objects.create(
            scheduled_action=action,
            planned_for=self._ago(500),
            occurrence_key="old-1",
            status=ScheduledActionRun.Status.COMPLETED,
            started_at=self._ago(500),
            finished_at=self._ago(499),
        )

    def _audit(self, *, action, minutes_ago, outcome="success", details=None, cluster=None, snapshot="", **fields):
        event = AuditEvent.objects.create(
            action=action,
            outcome=outcome,
            details=details or {},
            cluster=cluster,
            cluster_key_snapshot=snapshot,
            **fields,
        )
        AuditEvent.objects.filter(pk=event.pk).update(timestamp=self._ago(minutes_ago))
        event.refresh_from_db()
        return event

    def _build_catalog_refreshes(self):
        # The row that makes ordering by `timestamp` wrong: pressed 50 minutes ago,
        # picked up by the worker 2 minutes ago, so it belongs near the top.
        self._audit(
            action=STORAGE_CATALOG_REFRESH_ACTION,
            minutes_ago=50,
            outcome="running",
            cluster=self.cluster_a,
            details={"cluster_key": "alpha", "started_at": self._ago(2).isoformat(), "stage": "listing volumes"},
        )
        self._audit(
            action=STORAGE_CATALOG_REFRESH_ACTION,
            minutes_ago=17,
            outcome="success",
            cluster=self.cluster_b,
            details={"cluster_key": "beta", "stage": "completed"},
        )

    def _build_file_events(self):
        self._audit(
            action="file.uploaded",
            minutes_ago=9,
            cluster=self.cluster_a,
            details={"storage_id": "nfs-vm", "path": "iso/debian.iso"},
        )
        # Attributed by snapshot only — the FK is gone, the string is not.
        self._audit(
            action="file.renamed",
            minutes_ago=19,
            snapshot="beta",
            details={"storage_id": "nfs-vm", "path": "images/x.qcow2"},
        )
        # Cluster-neutral: visible under every scope.
        self._audit(action="file.folder_created", minutes_ago=27, details={"storage_id": "nfs-vm", "path": "backups"})
        # Superseded inflate: queued, then finished. Only the terminal row shows.
        self._audit(
            action=INFLATE_QUEUED_ACTION,
            minutes_ago=15,
            outcome="queued",
            cluster=self.cluster_a,
            details={"storage_id": "nfs-vm", "path": "images/inflate-me.qcow2", "target_preallocation": "full"},
        )
        self._audit(
            action="file.inflated",
            minutes_ago=6,
            cluster=self.cluster_a,
            details={"storage_id": "nfs-vm", "path": "images/inflate-me.qcow2", "target_preallocation": "full"},
        )
        # Still queued: nothing has answered it, so it stays.
        self._audit(
            action=INFLATE_QUEUED_ACTION,
            minutes_ago=12,
            outcome="queued",
            cluster=self.cluster_a,
            details={"storage_id": "nfs-vm", "path": "images/still-going.qcow2", "target_preallocation": "metadata"},
        )
        # A bulk operation that raised no question at all. `details` has no
        # `question` key, so `details__question=True` is SQL NULL rather than
        # false — and Postgres sorts NULL *first* under `DESC`, which would pin
        # the one row in the corpus that is not a question.
        self._audit(
            action=BULK_FILE_ACTION,
            minutes_ago=55,
            cluster=self.cluster_a,
            object_id="quiet",
            details={"operation": "move", "summary": "3 of 3 moved", "storage_id": "nfs-vm"},
        )
        # An open bulk question, and one that has been answered.
        self._audit(
            action=BULK_FILE_ACTION,
            minutes_ago=44,
            outcome="warning",
            cluster=self.cluster_a,
            object_id="trash",
            details={
                "operation": "trash",
                "summary": "2 of 3 moved to trash",
                "question": True,
                "failed": [{"path": "a", "error": "no"}],
                "storage_id": "nfs-vm",
            },
        )
        self._audit(
            action=BULK_FILE_ACTION,
            minutes_ago=41,
            outcome="warning",
            cluster=self.cluster_b,
            object_id="move",
            details={
                "operation": "move",
                "summary": "1 of 2 moved",
                "question": True,
                "question_dismissed": True,
                "storage_id": "nfs-vm",
            },
        )

    def _shutdown(self, *, minutes_ago, extra=None, cluster=None, snapshot=""):
        details = {
            "target_type": "vm",
            "vmid": 106,
            "node": "pve1",
            "name": "web",
            "error": "VM quit/powerdown failed - got timeout",
        }
        details.update(extra or {})
        return self._audit(
            action="guest.power.shutdown",
            minutes_ago=minutes_ago,
            outcome="failed",
            details=details,
            cluster=cluster,
            snapshot=snapshot,
            object_type="guest",
            object_id="vm:106",
        )

    def _build_guest_events(self):
        # Open question.
        self._shutdown(minutes_ago=33, cluster=self.cluster_a)
        # Answered by the operator.
        self._shutdown(minutes_ago=34, cluster=self.cluster_b, extra={"force_stop_dismissed": True})
        # Answered by the reaper.
        self._shutdown(minutes_ago=35, cluster=self.cluster_a, extra={"force_stop_resolved_at": self.now.isoformat()})
        # Timed out, but nothing to force-stop: not a question.
        self._shutdown(minutes_ago=36, cluster=self.cluster_a, extra={"vmid": None, "target_type": ""})
        # A shutdown that failed for a reason force-stop cannot fix.
        self._shutdown(minutes_ago=37, cluster=self.cluster_b, extra={"error": "permission denied"})
        self._audit(
            action="guest.power.start",
            minutes_ago=3,
            cluster=self.cluster_a,
            details={"target_type": "vm", "vmid": 110, "node": "pve1"},
            object_type="guest",
            object_id="vm:110",
        )
        self._audit(
            action="guest.migrate",
            minutes_ago=21,
            snapshot="beta",
            details={"target_type": "vm", "vmid": 111, "target_node": "pve2"},
            object_type="guest",
            object_id="vm:111",
        )
        self._audit(
            action="tag.registered",
            minutes_ago=25,
            details={"new_tag": "prod"},
            object_type="tag",
            object_id="prod",
        )

    # --- the parity assertion -------------------------------------------------

    def _assert_matches_reference(self, page, limit, cluster_key=""):
        actual = recent_task_page(page=page, limit=limit, cluster_key=cluster_key)
        expected = _reference_page(page, limit, cluster_key=cluster_key)
        label = f"page={page} limit={limit} cluster={cluster_key!r}"
        self.assertEqual([task["id"] for task in actual.tasks], [task["id"] for task in expected["tasks"]], label)
        self.assertEqual(actual.total, expected["total"], label)
        self.assertEqual(actual.questions_pending, expected["questions_pending"], label)

    def test_every_page_matches_the_reference_composition(self):
        for limit in (3, 5):
            for page in range(0, 6):
                self._assert_matches_reference(page, limit)

    def test_cluster_scoped_pages_match_the_reference_composition(self):
        for cluster_key in ("alpha", "beta"):
            for page in range(0, 4):
                self._assert_matches_reference(page, 4, cluster_key=cluster_key)

    def test_the_corpus_actually_spans_more_than_one_page(self):
        """A parity test over an empty result proves nothing."""
        page = recent_task_page(limit=5)
        self.assertGreater(page.total, 10)
        self.assertTrue(page.has_next)
        self.assertEqual(len(page.tasks), 5)

    # --- the properties the reference cannot state ----------------------------

    def test_a_pinned_question_appears_once_across_the_whole_pagination(self):
        seen: list[str] = []
        for number in range(0, 8):
            seen.extend(task["id"] for task in recent_task_page(page=number, limit=3).tasks)
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), recent_task_page(limit=3).total)

    def test_open_questions_sort_ahead_of_everything_newer(self):
        page = recent_task_page(limit=5)
        pinned = [task for task in page.tasks if task.get("question")]
        self.assertEqual(len(pinned), page.questions_pending)
        self.assertEqual([task["id"] for task in page.tasks[: len(pinned)]], [task["id"] for task in pinned])

    def test_a_superseded_inflate_is_dropped_and_a_live_one_is_not(self):
        details = [task["details"] for task in recent_task_page(limit=50).tasks]
        self.assertIn("images/still-going.qcow2", details)
        self.assertEqual(details.count("images/inflate-me.qcow2"), 1)

    def test_the_catalog_refresh_sorts_by_the_time_it_started_not_the_time_it_was_queued(self):
        """Ordering by `timestamp` would bury this row 50 minutes down."""
        tasks = recent_task_page(limit=5).tasks
        unpinned = [task for task in tasks if not task.get("question")]
        self.assertIn(STORAGE_CATALOG_REFRESH_ACTION, [task["action"] for task in unpinned[:3]])

    def test_composing_a_page_does_not_get_more_expensive_as_the_window_fills(self):
        """The finding, as an assertion.

        Sixty more events inside the window, all older than the rows the first page
        shows, so the page itself is unchanged. The old implementation built a
        display dict for every one of them; this one must not notice.
        """
        with CaptureQueriesContext(connection) as before:
            baseline = recent_task_page(limit=5)

        for index in range(60):
            self._audit(
                action="guest.power.start",
                minutes_ago=52 + index % 5,
                cluster=self.cluster_a,
                details={"target_type": "vm", "vmid": 200 + index, "node": "pve1"},
                object_type="guest",
                object_id=f"vm:{200 + index}",
            )

        with CaptureQueriesContext(connection) as after:
            crowded = recent_task_page(limit=5)

        self.assertEqual([task["id"] for task in crowded.tasks], [task["id"] for task in baseline.tasks])
        self.assertEqual(crowded.total, baseline.total + 60)
        self.assertEqual(len(after.captured_queries), len(before.captured_queries))

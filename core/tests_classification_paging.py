"""The two classification lists page identically, and neither drops files.

Orphan Finder shipped with no pagination while `classified_files` — same data,
same table shape, three functions away — had all of it. Worse than the missing
control was what the cap did: 200 rows were taken *per storage* and the
concatenation re-sliced to 200, so a storage sorting late lost every one of its
files with nothing on the page saying so. An operator working the queue down saw
200, cleaned them, reloaded, and saw 200 again.

Both views now share `_classified_files_page`, so the assertions below are
written against both surfaces on purpose: the point of the shared helper is that
they cannot drift apart again, and a test that only visits one of them would not
notice if they did.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core.models import FileInventory, ScanRun, StorageMount

PAGE_SIZE = 200
# Enough that the first page cannot hold "alpha" alone. "zulu" sorts after every
# alpha path, which is exactly the position the old double-slice erased.
ALPHA_FILES = 250
ZULU_FILES = 10


class ClassificationPagingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser(
            username="paging", email="paging@example.invalid", password="unused-oidc-only"
        )
        cls.alpha = StorageMount.objects.create(
            storage_id="alpha", display_name="Alpha", path="/mnt/alpha", enabled=True
        )
        cls.zulu = StorageMount.objects.create(storage_id="zulu", display_name="Zulu", path="/mnt/zulu", enabled=True)
        # A fleet-wide scan: target_storage is null, so it is the latest result
        # for both storages at once. That is the pairing the single query has to
        # keep — neither storage id nor scan id alone describes the set.
        cls.scan = ScanRun.objects.create(
            status=ScanRun.Status.COMPLETED,
            filesystem_scan_at=timezone.now(),
            finished_at=timezone.now(),
        )
        rows = []
        for storage, count in ((cls.alpha, ALPHA_FILES), (cls.zulu, ZULU_FILES)):
            for index in range(count):
                rows.append(
                    FileInventory(
                        scan_run=cls.scan,
                        storage=storage,
                        path=f"/{storage.storage_id}/file-{index:04d}.qcow2",
                        classification=FileInventory.Classification.LIKELY_ORPHAN,
                    )
                )
        FileInventory.objects.bulk_create(rows)
        cls.total = ALPHA_FILES + ZULU_FILES

    def setUp(self):
        self.client.force_login(self.user)

    def _page(self, url, page):
        response = self.client.get(url, {"page": page} if page else {})
        self.assertEqual(response.status_code, 200)
        return response

    def test_orphan_finder_reports_the_true_total(self):
        """The count on the page is the count in the database, not the page size."""
        response = self._page(reverse("core:orphan_finder"), 0)
        self.assertEqual(response.context["total"], self.total)
        self.assertEqual(len(response.context["files"]), PAGE_SIZE)
        self.assertTrue(response.context["has_next"])
        self.assertFalse(response.context["has_prev"])
        self.assertContains(response, f"1-{PAGE_SIZE} of {self.total}")

    def test_no_file_is_unreachable_by_paging(self):
        """The union of the pages is every file — the old shape lost a storage."""
        url = reverse("core:orphan_finder")
        seen = []
        for page in (0, 1):
            seen.extend(entry.path for entry in self._page(url, page).context["files"])
        self.assertEqual(len(seen), self.total)
        self.assertEqual(seen, sorted(seen), "Paging must not disturb the display order.")
        self.assertEqual(
            len([path for path in seen if path.startswith("/zulu/")]),
            ZULU_FILES,
            "Every file of the late-sorting storage must be reachable; taking 200 per storage "
            "and re-slicing the concatenation is what silently dropped them.",
        )

    def test_paging_past_the_end_clamps_to_the_last_page(self):
        response = self._page(reverse("core:orphan_finder"), 99)
        self.assertEqual(response.context["page"], 1)
        self.assertFalse(response.context["has_next"])

    def test_a_junk_page_parameter_does_not_500(self):
        response = self.client.get(reverse("core:orphan_finder"), {"page": "../etc"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page"], 0)

    def test_both_classification_lists_page_the_same_way(self):
        """The shared helper is the guarantee; this is what would catch its loss."""
        FileInventory.objects.filter(path__startswith="/zulu/").update(
            classification=FileInventory.Classification.UNKNOWN
        )
        orphans = self._page(reverse("core:orphan_finder"), 0)
        unknown = self._page(
            f"{reverse('core:classified_files')}?classification={FileInventory.Classification.UNKNOWN}", 0
        )
        for key in ("page", "has_prev", "start_index"):
            self.assertEqual(orphans.context[key], unknown.context[key])
        self.assertEqual(orphans.context["total"], ALPHA_FILES)
        self.assertEqual(unknown.context["total"], ZULU_FILES)
        self.assertEqual(unknown.context["end_index"], ZULU_FILES)
        self.assertFalse(unknown.context["has_next"])

    def test_the_file_query_does_not_grow_with_the_number_of_storages(self):
        """One query for the page and one for the count, whatever the fleet size."""
        for index in range(6):
            StorageMount.objects.create(
                storage_id=f"extra{index}",
                display_name=f"Extra {index}",
                path=f"/mnt/extra{index}",
                enabled=True,
            )
        with CaptureQueriesContext(connection) as captured:
            self._page(reverse("core:orphan_finder"), 0)
        file_queries = [query for query in captured.captured_queries if "core_fileinventory" in query["sql"]]
        self.assertEqual(
            len(file_queries),
            2,
            "Expected one count and one page query regardless of storage count; got:\n"
            + "\n".join(query["sql"][:160] for query in file_queries),
        )

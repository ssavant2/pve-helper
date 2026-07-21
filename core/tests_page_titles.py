"""Every page says in the browser tab which page it is.

`base.html` hardcoded `<title>pve-helper</title>` for the whole application, so
four open windows — a console, a datastore, two guest workspaces — were four
identical tabs, bookmarks and history entries. The tell that it was an oversight
rather than a decision: `cluster_scope_required.html` declared a
`{% block title %}` that `base.html` never defined, so the template engine
dropped the line without a word.

That silent-drop is why the title is composed in Python (`browser_title` /
`navigation_context`) and read once in `base.html`, instead of being a block each
template is trusted to override: a page that forgets a title now falls back to
the bare application name, and the first test below fails on exactly that.

The interesting property is not "a title exists" but "the titles tell pages
apart", so both surface tests assert distinctness — between top-level pages, and
between the tabs of two guests, which is the case the operator actually has open
four times.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from core import urls as core_urls
from core.models import CurrentGuestInventory, FileInventory, ProxmoxCluster
from core.views.common import APP_TITLE, NAV_PAGE_TITLES, browser_title, navigation_context

TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
VIEWS_ROOT = Path(__file__).resolve().parent / "views"


def _title_of(response) -> str:
    match = TITLE_RE.search(response.content.decode(response.charset or "utf-8", errors="replace"))
    return match.group(1).strip() if match else ""


def _zero_argument_core_urls() -> dict[str, str]:
    """Every argument-free `core:` URL name mapped to its path.

    Derived from the URLconf for the same reason `tests_navigation_reachability`
    does it: a page added tomorrow is covered without touching this file.
    """
    return {
        f"core:{pattern.name}": reverse(f"core:{pattern.name}")
        for pattern in core_urls.urlpatterns
        if getattr(pattern, "name", None) and not pattern.pattern.regex.groups
    }


class BrowserTitleCompositionTests(TestCase):
    def test_parts_are_joined_most_specific_first(self):
        self.assertEqual(browser_title("web01", "Networks"), f"web01 · Networks · {APP_TITLE}")

    def test_an_empty_part_drops_out(self):
        """So a caller can pass an optional object label unconditionally."""
        self.assertEqual(browser_title("", "Tags", ""), f"Tags · {APP_TITLE}")
        self.assertEqual(browser_title(), APP_TITLE)

    def test_the_section_default_comes_from_the_navigation_key(self):
        self.assertEqual(navigation_context("orphans")["page_title"], f"Orphan Finder · {APP_TITLE}")

    def test_an_explicit_title_replaces_the_section_default(self):
        context = navigation_context("vms", page_title=("VM 500 (web01)", "Console"))
        self.assertEqual(context["page_title"], f"VM 500 (web01) · Console · {APP_TITLE}")
        self.assertEqual(context["active_nav"], "vms", "The sidebar key must survive the override.")

    def test_every_navigation_key_used_by_a_view_has_a_title(self):
        """The mapping is keyed by `active_nav`; a key with no entry is a page
        that would fall back to the bare application name."""
        keys = set()
        for source in VIEWS_ROOT.rglob("*.py"):
            for node in ast.walk(ast.parse(source.read_text())):
                if (
                    isinstance(node, ast.Call)
                    and getattr(node.func, "id", "") == "navigation_context"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    keys.add(node.args[0].value)
        self.assertIn("dashboard", keys, "The navigation_context call scan stopped matching core/views/.")
        self.assertEqual(
            sorted(keys - set(NAV_PAGE_TITLES)),
            [],
            "Navigation keys with no entry in NAV_PAGE_TITLES.",
        )


class TopLevelPageTitleTests(TestCase):
    """The pages an operator keeps open, addressed the way the sidebar does."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser(
            username="titles", email="titles@example.invalid", password="unused-oidc-only"
        )

    def setUp(self):
        self.client = Client(raise_request_exception=False)
        self.client.force_login(self.user)

    def _pages(self) -> dict[str, tuple[str, str]]:
        """`{url name: (view path, title)}` for every zero-argument HTML page."""
        pages = {}
        for name, path in _zero_argument_core_urls().items():
            response = self.client.get(path)
            if response.status_code != 200 or not response.headers.get("Content-Type", "").startswith("text/html"):
                continue  # POST-only action, redirect, JSON endpoint, health probe
            view = response.resolver_match.func
            pages[name] = (f"{view.__module__}.{view.__qualname__}", _title_of(response))
        return pages

    def test_no_page_falls_back_to_the_bare_application_name(self):
        pages = self._pages()
        self.assertIn("core:dashboard", pages, "The page derivation stopped finding pages.")
        untitled = sorted(name for name, (_view, title) in pages.items() if title in ("", APP_TITLE))
        self.assertEqual(untitled, [], "Pages rendering the fallback title instead of naming themselves.")

    def test_a_list_that_takes_its_subject_from_the_query_string_says_so(self):
        """`classified_files` shares the Orphan Finder's navigation key and is
        addressed with a query string, so the URLconf sweep above cannot see it —
        yet these two are the pair most likely to be open side by side."""
        orphans = _title_of(self.client.get(reverse("core:orphan_finder")))
        unknown = _title_of(
            self.client.get(
                reverse("core:classified_files"),
                {"classification": FileInventory.Classification.UNKNOWN},
            )
        )
        self.assertNotEqual(orphans, unknown)
        self.assertEqual(unknown, browser_title(FileInventory.Classification.UNKNOWN.label))

    def test_two_different_pages_never_share_a_title(self):
        """Distinct by view, not by URL: a second route onto the same page is
        the same page, and `storage_mount_register` is exactly that."""
        by_title: dict[str, set[str]] = {}
        for _name, (view, title) in self._pages().items():
            by_title.setdefault(title, set()).add(view)
        collisions = {title: sorted(views) for title, views in by_title.items() if len(views) > 1}
        self.assertEqual(collisions, {}, "Different pages sharing one browser title.")


class GuestWorkspaceTitleTests(TestCase):
    """Four workspaces open on four guests must be four different tabs."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser(
            username="guesttitles", email="guesttitles@example.invalid", password="unused-oidc-only"
        )
        cls.cluster = ProxmoxCluster.objects.create(key="titlefixture", display_name="Titles", enabled=True)
        for object_type, vmid in (("vm", 500), ("ct", 601)):
            CurrentGuestInventory.objects.create(
                cluster=cls.cluster,
                node="pve1",
                object_type=object_type,
                vmid=vmid,
                name=f"{object_type}-{vmid}",
                observed_at=timezone.now(),
            )

    def setUp(self):
        self.client = Client(raise_request_exception=False)
        self.client.force_login(self.user)

    def _title(self, route: str, object_type: str, vmid: int) -> str:
        response = self.client.get(
            reverse(route, args=[self.cluster.key, object_type, vmid]),
        )
        self.assertEqual(response.status_code, 200, f"{route} did not render for {object_type} {vmid}.")
        return _title_of(response)

    def test_the_title_names_the_guest_and_the_tab(self):
        title = self._title("core:guest_networks", "vm", 500)
        self.assertIn("500", title, "The tab must identify which guest it belongs to.")
        self.assertIn("Networks", title, "The tab must identify which tab it is.")
        self.assertTrue(title.endswith(APP_TITLE))

    def test_two_guests_and_two_tabs_are_four_distinct_titles(self):
        titles = [
            self._title(route, object_type, vmid)
            for route in ("core:guest_summary", "core:guest_networks")
            for object_type, vmid in (("vm", 500), ("ct", 601))
        ]
        self.assertEqual(len(set(titles)), 4, f"Guest tabs are not distinguishable: {titles}")

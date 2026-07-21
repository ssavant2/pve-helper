"""Every page must be reachable by clicking, not only by typing its URL.

Round 7 found `/datastores/` shipped with no link from anywhere in the app: the
storage catalog and the host-mount registration were each linked only from the
other, so neither could be reached from the navigation. Several plan reviews and
hundreds of automated tests missed it for one reason — every existing test
visited pages *by URL*. Nothing asserted that a page can be clicked to.

This test walks the app the way an operator does: start at `/`, follow links,
and see where you end up. Any zero-argument URL that renders an HTML page but is
not in the transitive closure is unreachable and fails here.

Deliberately scoped to zero-argument URLs: the failure mode this guards against
is a *top-level surface* that never got a navigation entry. Object-scoped routes
(`/vms/<cluster>/<type>/<vmid>/...`) need fixtures and render in
`tests_guest_page_rendering`. That sentence used to say they were "covered
elsewhere" when they were covered nowhere, which is how a console page that
raised NameError on every load stayed green — do not weaken it back into a
promise no file keeps.

What counts as a page is decided empirically, not from a hand-kept list: GET the
URL and see whether it answers `200 text/html`. POST-only actions (405), legacy
redirects (302), JSON/XHR endpoints and the health probes therefore drop out on
their own, and a new page added to `core/urls.py` is covered the day it is added
without touching this file.
"""

from __future__ import annotations

from collections import deque
from html.parser import HTMLParser
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from core import urls as core_urls

START_PATH = "/"

# Reachable-by-design exceptions. Each entry needs a reason: an unreachable page
# is a product defect unless it is deliberately not a navigation destination.
UNLINKED_BY_DESIGN: dict[str, str] = {
    # Alternate route to the settings page that owns host-mount registration; the
    # canonical entry point is core:settings_storage, which is in the sidebar.
    "core:storage_mount_register": "Duplicate route for the Settings → Storage access page.",
}

# The crawl must not be able to pass by classifying everything as a non-page.
# These are the surfaces that must always render and always be clickable.
REQUIRED_PAGES = frozenset(
    {
        "core:dashboard",
        "core:vms",
        "core:vms_overview",
        "core:clusters_overview",
        "core:orphan_finder",
        # core:scheduled_tasks is absent for the same reason core:tags_overview is:
        # it became cluster-scoped, so it takes an argument and the crawl — which is
        # deliberately zero-argument only — no longer sees it at all.
        "core:audit_log",
        # core:pve_helper_settings is deliberately a redirect to the first settings
        # tab, so it is not a page and is classified out by the crawl.
        "core:settings_storage",
    }
)


class _LinkParser(HTMLParser):
    """Collect link destinations using the browser's HTML parsing rules."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        if tag.casefold() != "a":
            return
        for name, value in attrs:
            if name.casefold() == "href" and value:
                self.hrefs.add(value)
                return


def _zero_argument_core_urls() -> dict[str, str]:
    """Map every argument-free `core:` URL name to its resolved path."""
    names: dict[str, str] = {}
    for pattern in core_urls.urlpatterns:
        name = getattr(pattern, "name", None)
        if not name or pattern.pattern.regex.groups:
            continue
        names[f"core:{name}"] = reverse(f"core:{name}")
    return names


class NavigationReachabilityTests(TestCase):
    """A page nobody can click to is a page that does not exist."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser(
            username="reachability", email="reachability@example.invalid", password="unused-oidc-only"
        )

    def setUp(self):
        # Render errors must surface as a 500 response, not abort the crawl: an
        # empty database is a legitimate first-run state and a page that cannot
        # survive it is a finding of its own, reported by test_pages_render.
        self.client = Client(raise_request_exception=False)
        self.client.force_login(self.user)
        self.urls = _zero_argument_core_urls()
        self.path_to_name = {path: name for name, path in self.urls.items()}

    def _get(self, path: str):
        return self.client.get(path)

    def _is_page(self, response) -> bool:
        return response.status_code == 200 and response.headers.get("Content-Type", "").startswith("text/html")

    def _links(self, response) -> set[str]:
        body = response.content.decode(response.charset or "utf-8", errors="replace")
        parser = _LinkParser()
        parser.feed(body)
        return {urlsplit(href).path for href in parser.hrefs if urlsplit(href).path in self.path_to_name}

    def _crawl(self) -> set[str]:
        """Transitive closure of zero-argument pages reachable from the start page."""
        seen = {START_PATH}
        queue = deque([START_PATH])
        while queue:
            response = self._get(queue.popleft())
            if not self._is_page(response):
                continue
            for path in self._links(response) - seen:
                seen.add(path)
                queue.append(path)
        return seen

    def test_every_page_is_reachable_by_following_links(self):
        pages = {name: path for name, path in self.urls.items() if self._is_page(self._get(path))}
        self.assertGreaterEqual(
            set(pages),
            REQUIRED_PAGES,
            "A surface that must always render did not answer 200 text/html; fix that before trusting this crawl.",
        )

        reached = self._crawl()
        unreachable = sorted(
            f"{name} ({path})" for name, path in pages.items() if path not in reached and name not in UNLINKED_BY_DESIGN
        )
        self.assertEqual(
            unreachable,
            [],
            "These pages render but cannot be reached by following links from "
            f"{START_PATH}. Add a navigation entry, or record the reason in "
            "UNLINKED_BY_DESIGN.",
        )

    def test_required_pages_survive_an_empty_database(self):
        """First-run state is a real state: no clusters, no storage, no scans."""
        statuses = {name: self._get(self.urls[name]).status_code for name in sorted(REQUIRED_PAGES)}
        broken = sorted(f"{name} -> {status}" for name, status in statuses.items() if status != 200)
        self.assertEqual(broken, [], "Pages must render on a first-run empty database.")

    def test_unlinked_exceptions_still_exist(self):
        """Stop the allowlist from outliving the URLs it excuses."""
        stale = sorted(name for name in UNLINKED_BY_DESIGN if name not in self.urls)
        self.assertEqual(stale, [], "UNLINKED_BY_DESIGN names a URL that no longer exists.")

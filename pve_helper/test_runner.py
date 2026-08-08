"""Test runner that gives every test a clean cache.

The application cache is a process-wide LocMem instance (`settings.CACHES`), and
Django resets the database between tests but not the cache. That gap was a real
source of non-determinism rather than a theoretical one: entries live 3, 30 and 60
seconds (`LIVE_GUEST_LOCKS_CACHE_SECONDS`, `LIVE_GUEST_INVENTORY_CACHE_SECONDS`,
`datastore_nav`) against a suite that takes around 165 seconds, so whether a value
written by one test was still live when a later test read it depended on wall-clock
timing and machine load. The same commit produced a green suite or ~39 failures
across runs, which is worse than a consistent failure: a suite that is red one run
in five gets learned as noise, and the next genuine regression is invisible inside
it.

The hook is `_post_teardown` — clear *after* each test — for two reasons. It is the
honest contract ("a test leaves no state behind", the same one Django's database
rollback keeps), and it is the only per-test hook available here: in Django 6
`_pre_setup` is a **classmethod**, and `_setup_and_call` skips it entirely when
`_pre_setup_ran_eagerly` is set, so a pre-test hook hung on it silently does not run
for the first test of a class.

A test that genuinely needs a warm cache warms it inside the test, which several
already do.
"""

from __future__ import annotations

from django.core.cache import caches
from django.test import SimpleTestCase
from django.test.runner import DiscoverRunner


class IsolatedCacheTestRunner(DiscoverRunner):
    """`DiscoverRunner` plus a cache reset after every test."""

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)

        original_post_teardown = SimpleTestCase._post_teardown

        def _post_teardown(test_case):
            try:
                original_post_teardown(test_case)
            finally:
                for cache in caches.all(initialized_only=False):
                    cache.clear()

        self._original_post_teardown = original_post_teardown
        SimpleTestCase._post_teardown = _post_teardown

    def teardown_test_environment(self, **kwargs):
        original = getattr(self, "_original_post_teardown", None)
        if original is not None:
            SimpleTestCase._post_teardown = original
        super().teardown_test_environment(**kwargs)

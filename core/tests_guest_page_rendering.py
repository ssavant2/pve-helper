"""Every object-scoped guest page must render for a VM and for a CT.

`tests_navigation_reachability` deliberately stops at zero-argument URLs and says
object-scoped routes are "covered elsewhere". They were covered nowhere. The
consequence was a console page that raised `NameError: static` on every single
load, and two dialog endpoints that did the same, with a green suite throughout.

The route list is derived from `core/urls.py` rather than hand-kept, so a tab
added tomorrow is covered the day it is added. What each page *contains* is the
business of the module's own tests; this asserts the far cheaper property those
tests all presuppose — that the view runs at all, for both guest kinds, against
a database that has a cluster and a guest but no scan data and no reachable
Proxmox.

Provider calls are blocked by the test settings, so a page that talks to Proxmox
must degrade to a rendered page with an error in it. A view may still answer 502
deliberately when no endpoint is reachable, as long as it says so in the body —
see `_unhandled`, which draws exactly that line.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import urls as core_urls
from core.models import CurrentGuestInventory, ProxmoxCluster

CLUSTER_KEY = "renderfixture"
GUESTS = {"vm": 500, "ct": 601}

# Routes whose kwargs are exactly a guest identity. Anything needing a further
# argument (a snapshot name, a firewall rule position) identifies an object this
# fixture does not create, so it is out of scope here by construction.
_GUEST_KWARGS = {"cluster_key", "object_type", "vmid"}


def _guest_scoped_url_names() -> list[str]:
    names = []
    for pattern in core_urls.urlpatterns:
        name = getattr(pattern, "name", None)
        if not name:
            continue
        groups = set(pattern.pattern.regex.groupindex)
        if groups == _GUEST_KWARGS:
            names.append(f"core:{name}")
    return sorted(names)


@override_settings(APP_REQUIRE_LOGIN=False)
class GuestScopedPageRenderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser(
            username="guestrender", email="guestrender@example.invalid", password="unused-oidc-only"
        )
        cls.cluster = ProxmoxCluster.objects.create(key=CLUSTER_KEY, display_name="Render Fixture", enabled=True)
        for object_type, vmid in GUESTS.items():
            CurrentGuestInventory.objects.create(
                cluster=cls.cluster,
                node="pve1",
                object_type=object_type,
                vmid=vmid,
                name=f"{object_type}-{vmid}",
                observed_at=timezone.now(),
            )

    def setUp(self):
        # The failure this exists to catch is a server error, so the exception
        # must become a 500 response instead of aborting the test run at the
        # first broken route and hiding the rest.
        self.client = Client(raise_request_exception=False)
        self.client.force_login(self.user)

    def test_guest_scoped_routes_are_still_covered(self):
        """Guard the derivation itself: an empty list would pass silently."""
        names = _guest_scoped_url_names()
        self.assertIn("core:guest_console", names)
        self.assertIn("core:guest_summary", names)
        self.assertGreaterEqual(len(names), 20, "The guest route derivation stopped matching core/urls.py.")

    def _unhandled(self, response) -> bool:
        """A 5xx is a defect unless the view chose it and said why.

        `guest_pool_options` answers 502 with a public error message when no
        endpoint can be reached — that is the documented degraded behaviour, not
        a crash. An unhandled exception never carries such a body, and 500 is
        never a status a view here picks on purpose.
        """
        if response.status_code < 500:
            return False
        if response.status_code == 500:
            return True
        if not response.headers.get("Content-Type", "").startswith("application/json"):
            return True
        try:
            return not response.json().get("error")
        except ValueError:
            return True

    def test_every_guest_page_renders_for_both_guest_kinds(self):
        failures = []
        for name in _guest_scoped_url_names():
            for object_type, vmid in GUESTS.items():
                path = reverse(name, kwargs={"cluster_key": CLUSTER_KEY, "object_type": object_type, "vmid": vmid})
                response = self.client.get(path)
                if self._unhandled(response):
                    failures.append(f"{name} [{object_type}] -> {response.status_code} ({path})")
        self.assertEqual(
            failures,
            [],
            "These guest routes raised a server error. A page that cannot reach "
            "Proxmox must still render and say so:\n  " + "\n  ".join(failures),
        )

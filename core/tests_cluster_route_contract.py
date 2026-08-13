"""The canonical cluster/node URL contract, pinned. Module 5 phase 5a0B.

``docs/hosts&clusters.local.md`` settles three URL families and forbids two
aliases. Prose cannot enforce either, and the alias it forbids is the one a
future "standalone hosts deserve their own namespace" change would reach for
first -- which is exactly how a second host identity gets in through routing
after the identity contract closed the model.

The settled families:

* ``clusters/<cluster_key>/…`` for cluster scope **and all node tabs**;
* ``vms/<cluster_key>/<object_type>/<vmid>/…`` for the guest workspace;
* ``storage/<mount_id>/…`` for the installation-scoped file browser, with
  cluster-scoped datastore tabs under ``clusters/<cluster_key>/datastores/…``.

Forbidden: a bare-node route (a node identified without its cluster) and any
``hosts/`` **namespace**. ``Hosts`` is a visual grouping in the tree, never an
identity. It may name a cluster tab -- ``clusters/<cluster_key>/hosts/`` is
addressed through its cluster like every other tab -- but never a routing root
and never a segment that captures an object.

The pre-existing bare-node shims are the deliberate exception: they take a bare
node *in order to refuse it*, answering 409 with the qualified candidates
(``MulticlusterUrlTests.test_legacy_node_url_is_ambiguous_across_same_named_nodes``).
They are allowlisted by **exact name** in :data:`BARE_NODE_REFUSAL_ROUTES`, and a
second assertion strikes stale entries, so the allowlist cannot outlive its call
sites. A ``legacy_`` *prefix* predicate would not do: there are 57 ``legacy_*``
route names, so a genuine bare-node identity route named ``legacy_node_summary``
would pass unnoticed. This is the construction
``MembershipReadInvariantTests`` already uses for the same problem.

**Declared limits**, in the style the view ratchet already uses — this catches the
naive reintroduction, not every form:

* a bare-node route that *reuses* one of the eight allowlisted names passes. Route
  names are deliberately shared here (``datastore_routes`` gives one name two
  shapes), so it is reachable in principle, though not plausibly by accident.
  ``MembershipReadInvariantTests`` has the identical property;
* **the predicates are parameter-*name* dependent, not only syntax dependent.**
  ``<str:node_name>`` escapes all three node tests exactly as ``re_path`` does;
  the names checked are ``node``, ``cluster_key`` and ``object_type``, which are
  the spellings the shipped routes use;
* ``re_path`` escapes both predicates for the same reason: they match the
  ``<str:node>`` and ``<path:…>`` spellings that ``path()`` produces;
* a custom converter that accepts ``/`` escapes the structural test, which names
  only the ``path`` converter.
"""

from __future__ import annotations

import re

from django.test import SimpleTestCase
from django.urls import NoReverseMatch, resolve, reverse

#: The eight shipped shims that accept a bare node in order to refuse it. Every
#: one is a ``legacy_node_redirect`` returning 409 with the cluster-qualified
#: candidates; none renders a node.
BARE_NODE_REFUSAL_ROUTES = frozenset(
    {
        "legacy_storage_api_inventory",
        "legacy_api_storage_summary",
        "legacy_api_storage_monitor",
        "legacy_api_storage_volumes",
        "legacy_api_storage_vms",
        "legacy_api_storage_content",
        "legacy_update_api_storage_content",
        "legacy_api_storage_configure",
    }
)


def _routes() -> list[tuple[str, str]]:
    """Return ``(pattern, name)`` for every route reachable from the root URLconf.

    Walks includes rather than reading ``core.urls.urlpatterns`` directly: a
    ``hosts/`` or bare-node route added to ``pve_helper/urls.py``, or behind an
    ``include()``, is exactly as much a second host identity as one added to
    ``core/urls.py``, and reading one module would not see it.
    """
    from django.urls import get_resolver

    def walk(patterns, prefix=""):
        for entry in patterns:
            pattern = prefix + str(entry.pattern)
            nested = getattr(entry, "url_patterns", None)
            if nested is None:
                yield pattern, getattr(entry, "name", "") or ""
            else:
                yield from walk(nested, pattern)

    return list(walk(get_resolver().url_patterns))


class NodeRouteQualificationTests(SimpleTestCase):
    def _bare_node_routes(self) -> set[str]:
        """Names of routes that take a node without its cluster."""
        return {
            name
            for pattern, name in _routes()
            if "<str:node>" in pattern and not pattern.startswith("clusters/<str:cluster_key>/")
        }

    def test_every_node_route_is_cluster_qualified_or_a_declared_refusal(self):
        offenders = sorted(self._bare_node_routes() - BARE_NODE_REFUSAL_ROUTES)

        self.assertEqual(
            offenders,
            [],
            "A node is identified by NodeRef(cluster_key, node). A route that takes a bare "
            "node name either belongs under clusters/<cluster_key>/ or must be a shim that "
            "refuses it with the qualified candidates -- and then be named in "
            f"BARE_NODE_REFUSAL_ROUTES with that reason: {', '.join(offenders)}",
        )

    def test_the_refusal_allowlist_does_not_outlive_its_call_sites(self):
        stale = sorted(BARE_NODE_REFUSAL_ROUTES - self._bare_node_routes())

        self.assertEqual(
            stale,
            [],
            "These routes no longer take a bare node, so their allowlist entries are "
            "stale and would silently permit a future bare-node route to reuse the "
            f"name. Strike them: {', '.join(stale)}",
        )

    def test_no_hosts_namespace_exists(self):
        """`hosts` may name a cluster *tab*; it may never name an object.

        The rule is about identity, not about the word. `clusters/<key>/hosts/` is
        the Hosts tab of one cluster, addressed through that cluster like every
        other tab. What stays forbidden is `hosts/` as a routing root, or a `hosts/`
        segment that captures an object — either would make "host" a second identity
        alongside the cluster-qualified NodeRef.
        """
        offenders = [
            pattern
            for pattern, _ in _routes()
            if pattern.startswith("hosts/") or re.search(r"/hosts/(?=<|[^/]*/<)", pattern)
        ]
        self.assertEqual(
            offenders,
            [],
            "'Hosts' is a visual grouping in the workspace tree, not an identity. A "
            "standalone node uses the same clusters/<cluster_key>/nodes/<node>/ routes as "
            "a clustered one.",
        )

    def test_the_node_tab_family_lives_under_the_cluster_prefix(self):
        node_routes = [
            pattern for pattern, name in _routes() if "<str:node>" in pattern and name not in BARE_NODE_REFUSAL_ROUTES
        ]
        self.assertTrue(node_routes, "the node tab family must exist to be pinned")
        for pattern in node_routes:
            with self.subTest(pattern=pattern):
                self.assertTrue(pattern.startswith("clusters/<str:cluster_key>/nodes/<str:node>/"))


class ClusterQualifiedReversalTests(SimpleTestCase):
    """Reversal is the half of identity that tests usually skip.

    A model can be perfectly cluster-qualified while two clusters still reverse
    to one URL, and the failure only shows up as the wrong cluster's data.
    """

    def test_the_same_node_name_in_two_clusters_reverses_to_distinct_urls(self):
        first = reverse(
            "core:api_storage_summary",
            kwargs={"cluster_key": "a", "node": "pve1", "storage": "local"},
        )
        second = reverse(
            "core:api_storage_summary",
            kwargs={"cluster_key": "b", "node": "pve1", "storage": "local"},
        )
        self.assertNotEqual(first, second)
        self.assertEqual(first, "/clusters/a/nodes/pve1/datastores/local/summary/")
        self.assertEqual(second, "/clusters/b/nodes/pve1/datastores/local/summary/")

    def test_a_reversed_node_url_resolves_back_to_the_same_scope(self):
        url = reverse(
            "core:api_storage_summary",
            kwargs={"cluster_key": "clusterhq", "node": "pve1", "storage": "local"},
        )
        match = resolve(url)
        self.assertEqual(match.kwargs["cluster_key"], "clusterhq")
        self.assertEqual(match.kwargs["node"], "pve1")

    def test_the_guest_workspace_keeps_its_own_cluster_qualified_family(self):
        url = reverse(
            "core:guest_summary",
            kwargs={"cluster_key": "a", "object_type": "qemu", "vmid": 100},
        )
        self.assertTrue(url.startswith("/vms/a/qemu/100/"), url)

    def test_no_route_lets_a_scope_component_span_path_segments(self):
        # The reversal tests below prove the property for one route. This proves
        # it for every route at once: ``path``/``slug``-style converters that
        # accept "/" must never carry an identity component. Both are needed --
        # a per-route reversal test cannot see a converter swapped on a route it
        # does not name.
        offenders = [
            pattern
            for pattern, _ in _routes()
            for component in ("cluster_key", "node", "object_type")
            if f"<path:{component}>" in pattern
        ]
        self.assertEqual(
            offenders,
            [],
            "A cluster key, node or object type that can contain '/' lets one identity "
            "component forge a deeper route. Keep the str converter.",
        )

    def test_a_cluster_key_cannot_smuggle_a_path_separator(self):
        # ``str`` refuses "/", so a key like "a/nodes/pve1" cannot be reversed
        # into a forged deeper route. Pinned because switching a converter to
        # ``path`` is a one-word edit with no other visible symptom.
        with self.assertRaises(NoReverseMatch):
            reverse(
                "core:api_storage_summary",
                kwargs={"cluster_key": "a/nodes/pve9", "node": "pve1", "storage": "local"},
            )

    def test_a_node_name_cannot_smuggle_a_path_separator(self):
        with self.assertRaises(NoReverseMatch):
            reverse(
                "core:api_storage_summary",
                kwargs={"cluster_key": "a", "node": "pve1/datastores/other", "storage": "local"},
            )

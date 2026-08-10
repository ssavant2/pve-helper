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
``hosts/`` namespace. ``Hosts`` is a visual grouping in the tree, never an
identity or a URL.

The pre-existing ``legacy_*`` shims are the deliberate exception: they take a
bare node *in order to refuse it*, answering 409 with the qualified candidates
(``MulticlusterUrlTests.test_legacy_node_url_is_ambiguous_across_same_named_nodes``).
They are allowlisted by name so a new bare-node route cannot hide among them.
"""

from __future__ import annotations

from django.test import SimpleTestCase
from django.urls import NoReverseMatch, resolve, reverse


def _routes() -> list[tuple[str, str]]:
    """Return ``(pattern, name)`` for every route in the core URLconf."""
    from core.urls import urlpatterns

    return [(str(pattern.pattern), getattr(pattern, "name", "") or "") for pattern in urlpatterns]


class NodeRouteQualificationTests(SimpleTestCase):
    def test_every_node_route_is_cluster_qualified_or_a_legacy_refusal(self):
        offenders = [
            (pattern, name)
            for pattern, name in _routes()
            if "<str:node>" in pattern
            and not pattern.startswith("clusters/<str:cluster_key>/")
            and not name.startswith("legacy_")
        ]
        self.assertEqual(
            offenders,
            [],
            "A node is identified by NodeRef(cluster_key, node). A route that takes a bare "
            "node name either belongs under clusters/<cluster_key>/ or must be a legacy_* "
            "shim that refuses it with the qualified candidates.",
        )

    def test_no_hosts_namespace_exists(self):
        offenders = [pattern for pattern, _ in _routes() if pattern.startswith("hosts/") or "/hosts/" in pattern]
        self.assertEqual(
            offenders,
            [],
            "'Hosts' is a visual grouping in the workspace tree, not an identity. A "
            "standalone node uses the same clusters/<cluster_key>/nodes/<node>/ routes as "
            "a clustered one.",
        )

    def test_the_node_tab_family_lives_under_the_cluster_prefix(self):
        node_routes = [
            pattern for pattern, name in _routes() if "<str:node>" in pattern and not name.startswith("legacy_")
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

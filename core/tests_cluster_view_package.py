from django.test import SimpleTestCase
from django.urls import resolve, reverse

from core import views
from core.views import clusters
from core.views.clusters import connections

PUBLIC_CONNECTION_VIEWS = (
    "cluster_add",
    "cluster_connection",
    "cluster_connection_action",
    "cluster_endpoint_action",
    "cluster_endpoint_add",
    "clusters_overview",
)


class ClusterViewPackageInvariantTests(SimpleTestCase):
    def test_facade_exports_only_url_facing_connections_views(self):
        self.assertEqual(tuple(clusters.__all__), PUBLIC_CONNECTION_VIEWS)
        self.assertFalse(hasattr(clusters, "inspect_transport"))
        self.assertFalse(hasattr(clusters, "cluster_retirement_preflight"))
        self.assertFalse(hasattr(clusters, "_sign"))

    def test_root_view_facade_preserves_callback_identity(self):
        for name in PUBLIC_CONNECTION_VIEWS:
            implementation = getattr(connections, name)
            self.assertIs(getattr(clusters, name), implementation)
            self.assertIs(getattr(views, name), implementation)

    def test_existing_url_names_resolve_to_the_moved_implementations(self):
        routes = {
            "clusters_overview": (),
            "cluster_add": (),
            "cluster_connection": ("cluster-a",),
            "cluster_connection_action": ("cluster-a",),
            "cluster_endpoint_add": ("cluster-a",),
            "cluster_endpoint_action": ("cluster-a", 7),
        }
        for name, args in routes.items():
            match = resolve(reverse(f"core:{name}", args=args))
            self.assertIs(match.func, getattr(connections, name))

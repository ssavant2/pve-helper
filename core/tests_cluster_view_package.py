from django.test import SimpleTestCase
from django.urls import resolve, reverse

from core import views
from core.views import clusters
from core.views.clusters import connections, enrollment, workspace

PUBLIC_CONNECTION_VIEWS = (
    "cluster_add",
    "cluster_connection",
    "cluster_connection_action",
    "cluster_endpoint_action",
    "cluster_endpoint_add",
    "clusters_overview",
)

#: The node-enrollment surface is a second implementation module behind the same
#: facade. The invariant is that the facade exports URL-facing callables only — not
#: that they all come from one file.
PUBLIC_ENROLLMENT_VIEWS = (
    "cluster_enrollment_activate",
    "cluster_node_action",
    "cluster_node_add",
)

#: The Hosts & Clusters workspace shell (5a2A). A third implementation module, the
#: same rule: URL-facing callables only. It deliberately shares the facade with
#: Connections while sharing none of its private helpers -- which is the separation
#: this package was split for in the first place.
PUBLIC_WORKSPACE_VIEWS = (
    "cluster_datastores",
    "cluster_hosts",
    "cluster_summary",
    "cluster_vms",
    "node_datastores",
    "node_summary",
    "node_vms",
)

PUBLIC_CLUSTER_VIEWS = tuple(sorted(PUBLIC_CONNECTION_VIEWS + PUBLIC_ENROLLMENT_VIEWS + PUBLIC_WORKSPACE_VIEWS))


class ClusterViewPackageInvariantTests(SimpleTestCase):
    def test_facade_exports_only_url_facing_connections_views(self):
        self.assertEqual(tuple(clusters.__all__), PUBLIC_CLUSTER_VIEWS)
        self.assertFalse(hasattr(clusters, "inspect_transport"))
        self.assertFalse(hasattr(clusters, "cluster_retirement_preflight"))
        self.assertFalse(hasattr(clusters, "_sign"))

    def test_root_view_facade_preserves_callback_identity(self):
        for module, names in (
            (connections, PUBLIC_CONNECTION_VIEWS),
            (enrollment, PUBLIC_ENROLLMENT_VIEWS),
            (workspace, PUBLIC_WORKSPACE_VIEWS),
        ):
            for name in names:
                implementation = getattr(module, name)
                self.assertIs(getattr(clusters, name), implementation)
                self.assertIs(getattr(views, name), implementation)

    def test_enrollment_url_names_resolve_to_the_enrollment_module(self):
        for name, args in (("cluster_node_add", ("cluster-a",)), ("cluster_node_action", ("cluster-a", "pve1"))):
            match = resolve(reverse(f"core:{name}", args=args))
            self.assertIs(match.func, getattr(enrollment, name))

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

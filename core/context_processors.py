from django.conf import settings
from django.utils import timezone
from django.utils.functional import SimpleLazyObject

from .services.cluster_scopes import has_historical_clusters, managed_clusters
from .services.cluster_state_labels import cluster_degraded_label
from .services.datastore_nav import datastore_nav
from .services.recent_tasks import recent_task_page
from .services.workspace_nav import workspace_nav


def app_settings(request):
    # Lazy because a context processor runs for every HTML response, and plenty of
    # them never render a taskbar: the guest and storage dialogs pass
    # `request=request` to `render_to_string` so their fragments inherit this
    # context, and `base.html` — the only template that reads these — is nowhere in
    # sight. Composing the task page for those responses was pure waste.
    task_page = SimpleLazyObject(recent_task_page)
    # "Has clusters" is the historical decision: an all-retired installation still
    # has a Connections archive to show rather than the first-run onboarding CTA.
    has_configured_clusters = has_historical_clusters()
    # Navigation is the *managed* scope, not the enabled one. Retired clusters are
    # never navigation targets — `managed_clusters()` already excludes them, which is
    # the whole job the extra `enabled=True` filter here used to be credited with.
    # What that filter actually did was take disabled and quarantined clusters with
    # it, deleting from the UI exactly the inventory, schedules and history that
    # disabling promises to retain. It bit hardest in the retirement flow: verified
    # retirement is gated on disabling first, so preparing to retire a cluster
    # removed the ability to browse it and decide. They navigate, and render as
    # degraded.
    nav_clusters = list(managed_clusters().order_by("display_name", "key"))
    # Still the enabled set, because these are *write targets* (register/import a
    # guest into a cluster), not navigation: a disabled cluster cannot accept one.
    enabled_clusters = [cluster for cluster in nav_clusters if cluster.enabled]
    # One entry per cluster that publishes anything, so the sidebar's top level is
    # the cluster rather than a shared/local split the catalog does not have.
    datastore_clusters = []
    for cluster in nav_clusters:
        groups = datastore_nav(cluster=cluster)
        if not groups["shared"] and not groups["nodes"]:
            continue
        datastore_clusters.append(
            {
                "cluster_key": cluster.key,
                "cluster_name": cluster.display_name,
                "degraded": cluster_degraded_label(cluster),
                "shared": groups["shared"],
                "nodes": groups["nodes"],
            }
        )
    return {
        "app_base_url": settings.APP_BASE_URL,
        "app_version": settings.APP_VERSION,
        "app_display_url": request.build_absolute_uri("/").rstrip("/"),
        "app_require_login": settings.APP_REQUIRE_LOGIN,
        "storage_write_enabled": settings.STORAGE_WRITE_ENABLED,
        "storage_upload_max_size_mb": settings.STORAGE_UPLOAD_MAX_SIZE_MB,
        "app_nav_datastore_clusters": datastore_clusters,
        # Lazy for the same reason as the task page: every dialog fragment inherits
        # this context, and almost none of them render the sidebar.
        "app_nav_workspace": SimpleLazyObject(lambda: workspace_nav(nav_clusters)),
        "app_nav_clusters": nav_clusters,
        "app_enabled_clusters": enabled_clusters,
        "app_multiple_clusters": len(nav_clusters) > 1,
        "app_has_clusters": has_configured_clusters,
        "app_recent_tasks": SimpleLazyObject(lambda: task_page.tasks),
        "app_recent_tasks_page": task_page,
        "app_recent_tasks_rendered_at": timezone.now(),
    }

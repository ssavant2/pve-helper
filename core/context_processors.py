from django.conf import settings
from django.utils import timezone
from django.utils.functional import SimpleLazyObject

from .models import ProxmoxCluster
from .services.datastore_nav import datastore_nav
from .services.recent_tasks import recent_task_page


def app_settings(request):
    # Lazy because a context processor runs for every HTML response, and plenty of
    # them never render a taskbar: the guest and storage dialogs pass
    # `request=request` to `render_to_string` so their fragments inherit this
    # context, and `base.html` — the only template that reads these — is nowhere in
    # sight. Composing the task page for those responses was pure waste.
    task_page = SimpleLazyObject(recent_task_page)
    has_configured_clusters = ProxmoxCluster.objects.exists()
    enabled_clusters = list(ProxmoxCluster.objects.filter(enabled=True).order_by("display_name", "key"))
    # One entry per cluster that publishes anything, so the sidebar's top level is
    # the cluster rather than a shared/local split the catalog does not have.
    datastore_clusters = []
    for cluster in enabled_clusters:
        groups = datastore_nav(cluster=cluster)
        if not groups["shared"] and not groups["nodes"]:
            continue
        datastore_clusters.append(
            {
                "cluster_key": cluster.key,
                "cluster_name": cluster.display_name,
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
        "app_enabled_clusters": enabled_clusters,
        "app_multiple_clusters": len(enabled_clusters) > 1,
        "app_has_clusters": has_configured_clusters,
        "app_recent_tasks": SimpleLazyObject(lambda: task_page.tasks),
        "app_recent_tasks_page": task_page,
        "app_recent_tasks_rendered_at": timezone.now(),
    }

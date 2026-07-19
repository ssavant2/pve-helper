from django.conf import settings
from django.utils import timezone

from .models import ProxmoxCluster, StorageMount
from .services.local_datastores import local_datastore_nav
from .services.recent_tasks import recent_task_page


def app_settings(request):
    task_page = recent_task_page()
    has_configured_clusters = ProxmoxCluster.objects.exists()
    enabled_clusters = list(ProxmoxCluster.objects.filter(enabled=True).order_by("display_name", "key"))
    local_datastores = []
    for cluster in enabled_clusters:
        for host in local_datastore_nav(cluster=cluster):
            local_datastores.append(
                {
                    **host,
                    "cluster_key": cluster.key,
                    "cluster_name": cluster.display_name,
                }
            )
    return {
        "app_base_url": settings.APP_BASE_URL,
        "app_version": settings.APP_VERSION,
        "app_display_url": request.build_absolute_uri("/").rstrip("/"),
        "app_require_login": settings.APP_REQUIRE_LOGIN,
        "storage_write_enabled": settings.STORAGE_WRITE_ENABLED,
        "storage_upload_max_size_mb": settings.STORAGE_UPLOAD_MAX_SIZE_MB,
        "app_nav_storages": StorageMount.objects.filter(enabled=True).order_by("display_name"),
        "app_nav_local_datastores": local_datastores,
        "app_enabled_clusters": enabled_clusters,
        "app_multiple_clusters": len(enabled_clusters) > 1,
        "app_has_clusters": has_configured_clusters,
        "app_recent_tasks": task_page.tasks,
        "app_recent_tasks_page": task_page,
        "app_recent_tasks_rendered_at": timezone.now(),
    }

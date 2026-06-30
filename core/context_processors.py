from django.conf import settings
from django.utils import timezone

from .models import StorageMount
from .services.recent_tasks import recent_task_page


def app_settings(request):
    task_page = recent_task_page()
    return {
        "app_base_url": settings.APP_BASE_URL,
        "app_display_url": request.build_absolute_uri("/").rstrip("/"),
        "app_require_login": settings.APP_REQUIRE_LOGIN,
        "storage_write_enabled": settings.STORAGE_WRITE_ENABLED,
        "storage_upload_max_size_mb": settings.STORAGE_UPLOAD_MAX_SIZE_MB,
        "app_nav_storages": StorageMount.objects.filter(enabled=True).order_by("display_name"),
        "app_recent_tasks": task_page.tasks,
        "app_recent_tasks_page": task_page,
        "app_recent_tasks_rendered_at": timezone.now(),
    }

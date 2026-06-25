from django.conf import settings

from .models import StorageMount
from .services.recent_tasks import recent_task_page


def app_settings(_request):
    task_page = recent_task_page()
    return {
        "app_base_url": settings.APP_BASE_URL,
        "app_require_login": settings.APP_REQUIRE_LOGIN,
        "app_nav_storages": StorageMount.objects.filter(enabled=True).order_by("display_name"),
        "app_recent_tasks": task_page.tasks,
        "app_recent_tasks_page": task_page,
    }

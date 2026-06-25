from django.conf import settings

from .models import StorageMount


def app_settings(_request):
    return {
        "app_base_url": settings.APP_BASE_URL,
        "app_require_login": settings.APP_REQUIRE_LOGIN,
        "app_nav_storages": StorageMount.objects.filter(enabled=True).order_by("display_name"),
    }

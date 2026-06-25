from django.conf import settings


def app_settings(_request):
    return {
        "app_base_url": settings.APP_BASE_URL,
        "app_require_login": settings.APP_REQUIRE_LOGIN,
    }

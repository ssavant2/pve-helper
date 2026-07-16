"""ASGI config for pve-helper."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pve_helper.settings")

application = get_asgi_application()

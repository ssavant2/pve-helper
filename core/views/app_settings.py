"""PVE-helper Settings entry point."""

from __future__ import annotations

from .common import (
    app_login_required,
    redirect,
)


@app_login_required
def pve_helper_settings(request):
    return redirect("core:settings_storage")

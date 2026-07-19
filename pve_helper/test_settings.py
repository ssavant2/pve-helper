"""Hermetic settings for the ordinary Django test suite.

Live Proxmox integration tests must opt in explicitly with production-like
settings; they never belong in the default unit/view test command.
"""

from .settings import *  # noqa: F403

PVE_ENDPOINTS = ["https://pve.test.invalid:8006"]
PVE_API_TOKEN_ID = ""
PVE_API_TOKEN_SECRET = ""
PVE_TEST_NETWORK_DISABLED = True
FILE_UPLOAD_TEMP_DIR = "/tmp"

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

# No keyring, deliberately. The development stack's .env holds a real one, so a test
# that sealed a credential without configuring its own key passed here and failed in
# CI, where the checkout has no .env at all — six of them did, and only the release
# tag's run said so. A test that needs to store a secret carries a throwaway key in
# its own override_settings; whether the host happens to have one must not decide
# whether the suite passes.
PVE_HELPER_ENCRYPTION_KEYS = ""
PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID = ""

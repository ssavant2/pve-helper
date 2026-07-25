"""Publishing database-held certificates as files, for the processes that need files.

nginx cannot read the database and never will: it holds no credentials, has no
network path to PostgreSQL, and runs with a read-only root filesystem. OpenSSL, for
its part, takes a CA bundle as a path. So the material decided in the UI has to
become files somewhere both sides can see, and that somewhere is a Compose volume
mounted read-write here and read-only in nginx — the mirror image of the existing
`storage_accel_state` volume, which already carries nginx's answers to the app.

Three files are published:

* `server.crt` — the active HTTPS leaf followed by its chain.
* `server.key` — its private key, unsealed. Written `0600`, and never written at all
  unless HTTPS is actually enabled, so disabling HTTPS removes the key from disk
  rather than leaving it there unused.
* `ca-bundle.pem` — the base bundle plus every stored authority, which is what
  `REQUESTS_CA_BUNDLE` points at.

`state` carries a digest of all three plus the HTTPS decision. nginx's watcher polls
that one small file instead of hashing certificates on a loop, and a digest that has
not changed means there is nothing to reload — which is the normal case every time it
runs.

Writes are atomic: content goes to a temporary name in the same directory and is
renamed over the target. A reload that catches a half-written key is a certificate
error at the worst possible moment, and rename is what makes that impossible.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import ssl
from pathlib import Path

from django.conf import settings
from django.db import DatabaseError

from core.models import CertificateSettings, ManagedCertificate
from core.services.certificates import full_chain_pem, private_key_pem, settings_record

logger = logging.getLogger(__name__)

SERVER_CERTIFICATE_FILENAME = "server.crt"
SERVER_KEY_FILENAME = "server.key"
CA_BUNDLE_FILENAME = "ca-bundle.pem"
STATE_FILENAME = "state"


def state_directory() -> Path:
    return Path(getattr(settings, "PVE_HELPER_CERTIFICATE_STATE_DIR", "") or "/certificate-state")


def base_ca_bundle_path() -> Path | None:
    """The bundle stored authorities are appended to.

    Falling back to OpenSSL's own default file matters more than it looks: without
    it, adding one internal CA in the UI would replace the public trust store rather
    than extend it, and every outbound HTTPS call to a normal host would start
    failing certificate verification the moment an operator used this feature.
    """
    raw = (getattr(settings, "PVE_HELPER_BASE_CA_BUNDLE", "") or "").strip()
    if raw:
        return Path(raw)
    default = ssl.get_default_verify_paths().cafile
    return Path(default) if default else None


def _write_atomic(path: Path, content: str, *, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    os.chmod(temporary, mode)
    temporary.replace(path)


def _remove(path: Path) -> None:
    path.unlink(missing_ok=True)


def composed_ca_bundle() -> str:
    """The base trust bundle followed by every authority stored in the database.

    The base bundle stays first and is never rewritten: it is the deployment's own
    read-only file, and an authority added here should widen outbound trust, not
    replace what the host already established.
    """
    parts: list[str] = []
    base = base_ca_bundle_path()
    if base is not None:
        try:
            parts.append(base.read_text(encoding="utf-8"))
        except OSError:
            # A missing base bundle is a deployment that never had one. The stored
            # authorities are still worth publishing; failing here would take the
            # whole outbound trust file with it.
            logger.warning("Base CA bundle %s could not be read; publishing stored authorities only", base)
    for record in ManagedCertificate.objects.filter(usage=ManagedCertificate.Usage.AUTHORITY).order_by("label", "pk"):
        parts.append(record.certificate_pem)
    joined = "".join(part if part.endswith("\n") else part + "\n" for part in parts if part)
    return joined


def _digest(server_certificate: str, server_key: str, ca_bundle: str, https_enabled: bool) -> str:
    payload = json.dumps(
        {
            "https": https_enabled,
            "certificate": hashlib.sha256(server_certificate.encode("utf-8")).hexdigest(),
            "key": hashlib.sha256(server_key.encode("utf-8")).hexdigest(),
            "ca": hashlib.sha256(ca_bundle.encode("utf-8")).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _active(config: CertificateSettings) -> ManagedCertificate | None:
    if not config.https_enabled or config.active_certificate_id is None:
        return None
    return config.active_certificate


def publish() -> str:
    """Write the current decision to the shared volume and return its state digest.

    Safe to call on every change and on every startup: identical content produces an
    identical digest, and nginx's watcher reloads on a digest change alone.
    """
    directory = state_directory()
    directory.mkdir(parents=True, exist_ok=True)

    config = settings_record()
    record = _active(config)
    server_certificate = full_chain_pem(record) if record else ""
    server_key = private_key_pem(record) if record else ""
    ca_bundle = composed_ca_bundle()

    if server_certificate and server_key:
        _write_atomic(directory / SERVER_CERTIFICATE_FILENAME, server_certificate, mode=0o644)
        _write_atomic(directory / SERVER_KEY_FILENAME, server_key, mode=0o600)
    else:
        # Disabling HTTPS takes the key off disk. Leaving it behind would keep an
        # unused private key readable in a volume for as long as the install lives.
        _remove(directory / SERVER_CERTIFICATE_FILENAME)
        _remove(directory / SERVER_KEY_FILENAME)

    _write_atomic(directory / CA_BUNDLE_FILENAME, ca_bundle, mode=0o644)

    digest = _digest(server_certificate, server_key, ca_bundle, bool(server_certificate and server_key))
    _write_atomic(directory / STATE_FILENAME, f"{digest}\n", mode=0o644)
    return digest


def publish_quietly() -> str:
    """Publish, logging rather than raising when it cannot be done yet.

    Called at application startup, which is the one place that must tolerate both an
    absent volume (a development shell, a test run, `manage.py` on a laptop) and an
    unmigrated database (the boot that is about to run `migrate`). Neither is a
    reason to refuse to start; the next successful publication overwrites whatever
    this one skipped.
    """
    try:
        return publish()
    except OSError:
        logger.warning("Certificate state directory %s is not writable; skipping publication", state_directory())
    except DatabaseError:
        logger.info("Certificate tables are not available yet; skipping publication")
    return ""

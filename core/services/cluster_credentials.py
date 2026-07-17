"""Per-cluster Proxmox API credentials.

Two independent clusters realistically have different tokens. The old global
`PVE_API_TOKEN_ID`/`PVE_API_TOKEN_SECRET` were read from settings on every request,
so every cluster would have shared one identity — and a token that reaches both
clusters turns any cross-cluster mistake into a cross-cluster write.

Secrets are sealed on the way in and only unsealed at the moment a request is
built. Nothing here returns secret material to a caller that is not making a call,
and nothing writes it to logs, audit or the UI.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.models import ClusterCredential, ProxmoxCluster, RuntimeConfigurationState
from core.services.secret_encryption import (
    MissingEncryptionKeyError,
    active_key_id,
    decrypt_secret,
    encrypt_secret,
    key_id_of,
    keyring,
)


# Distinct from the bootstrap and tag-inventory lock ids.
_CREDENTIAL_CUTOVER_LOCK_ID = 0x50564548424F02


class ClusterCredentialError(RuntimeError):
    """A cluster has no usable credential."""


@dataclass(frozen=True)
class ProxmoxCredential:
    """One resolved identity, used to build a request's Authorization header."""

    token_id: str
    token_secret: str

    def authorization_header(self) -> str:
        return f"PVEAPIToken={self.token_id}={self.token_secret}"

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # Never let a secret reach a traceback, log line or debugger transcript.
        return f"ProxmoxCredential(token_id={self.token_id!r}, token_secret=<redacted>)"


def credential_cutover_completed() -> bool:
    state = RuntimeConfigurationState.objects.filter(pk=RuntimeConfigurationState.SINGLETON_PK).first()
    return bool(state and state.credential_cutover_completed_at)


def set_cluster_credential(cluster: ProxmoxCluster, *, token_id: str, token_secret: str) -> ClusterCredential:
    """Store a cluster's token, sealed. Replaces any existing one."""
    token_id = (token_id or "").strip()
    token_secret = (token_secret or "").strip()
    if not token_id or not token_secret:
        raise ClusterCredentialError("Both a token id and a token secret are required.")

    sealed = encrypt_secret(token_secret)
    with transaction.atomic():
        # Replacing an existing credential is a rotation of the token itself, which
        # is worth a timestamp; the first one is not.
        replacing = ClusterCredential.objects.select_for_update().filter(cluster=cluster).exists()
        credential, _created = ClusterCredential.objects.update_or_create(
            cluster=cluster,
            defaults={
                "token_id": token_id,
                "token_secret_sealed": sealed,
                "encryption_key_id": key_id_of(sealed),
                "rotated_at": timezone.now() if replacing else None,
            },
        )
    return credential


def resolve_credential(cluster: ProxmoxCluster) -> ProxmoxCredential:
    """The identity to authenticate to this cluster with.

    Before the credential cutover the legacy global token remains a documented
    single-cluster compatibility input, because only one cluster can be enabled and
    an installation may not have been migrated yet. After the cutover marker exists,
    the settings are never read again: a cluster without a stored credential is a
    configuration error, not a reason to silently fall back to a token that may
    belong to a different cluster.
    """
    stored = ClusterCredential.objects.filter(cluster=cluster).first()
    if stored is not None:
        return ProxmoxCredential(
            token_id=stored.token_id,
            token_secret=decrypt_secret(stored.token_secret_sealed),
        )

    if credential_cutover_completed():
        raise ClusterCredentialError(
            f"Cluster '{cluster.key}' has no stored API credential. Add one; the legacy "
            "global token is no longer read after the credential cutover."
        )

    token_id = (settings.PVE_API_TOKEN_ID or "").strip()
    token_secret = (settings.PVE_API_TOKEN_SECRET or "").strip()
    if not token_id or not token_secret:
        raise ClusterCredentialError(
            f"Cluster '{cluster.key}' has no stored API credential and no legacy token is configured."
        )
    return ProxmoxCredential(token_id=token_id, token_secret=token_secret)


def import_legacy_token(cluster: ProxmoxCluster) -> ClusterCredential | None:
    """Move the legacy global token into sealed storage for the bootstrap cluster.

    The token is sealed on the way in and never persisted in plaintext. Recording the
    cutover is what permanently stops runtime reads of the legacy settings; the
    settings themselves are ignored, not deleted, so a code rollback resumes reading
    them and a re-import stays idempotent.
    """
    token_id = (settings.PVE_API_TOKEN_ID or "").strip()
    token_secret = (settings.PVE_API_TOKEN_SECRET or "").strip()
    if not token_id or not token_secret:
        return None
    if ClusterCredential.objects.filter(cluster=cluster).exists():
        return ClusterCredential.objects.get(cluster=cluster)
    return set_cluster_credential(cluster, token_id=token_id, token_secret=token_secret)


def complete_credential_cutover() -> tuple[bool, str]:
    """Seal the legacy token into the bootstrap cluster and stop reading settings.

    Uses the same contract as bootstrap: one advisory lock, one atomic block, and a
    durable marker written with the imported record so a failed run leaves no marker
    and is safe to retry.

    Reversible by code rollback rather than by undo: the legacy settings are ignored
    from here, not deleted, so older code resumes reading them and a re-import is
    idempotent. Do not delete the legacy secret from the environment until the
    identity contract version 1 boundary has succeeded.
    """
    from core.services.runtime_bootstrap import _advisory_xact_lock

    with transaction.atomic():
        _advisory_xact_lock(_CREDENTIAL_CUTOVER_LOCK_ID)
        state = RuntimeConfigurationState.objects.select_for_update().filter(
            pk=RuntimeConfigurationState.SINGLETON_PK
        ).first()
        if state is None:
            return False, "The installation is not bootstrapped yet."
        if state.credential_cutover_completed_at:
            return False, "The credential cutover has already completed."

        clusters = list(ProxmoxCluster.objects.select_for_update().order_by("key"))
        if len(clusters) != 1:
            return False, (
                f"Expected exactly one cluster to import the legacy token into, found {len(clusters)}."
            )
        cluster = clusters[0]

        # Verify the keyring works before recording a marker that stops the fallback.
        active_key_id()

        credential = import_legacy_token(cluster)
        if credential is None and not ClusterCredential.objects.filter(cluster=cluster).exists():
            return False, (
                f"Cluster '{cluster.key}' has no stored credential and no legacy token is "
                "configured to import. Set a credential first."
            )

        state.credential_cutover_completed_at = timezone.now()
        state.save(update_fields=["credential_cutover_completed_at", "updated_at"])

    return True, f"Credential cutover complete; cluster '{cluster.key}' now uses its own stored token."


def missing_encryption_key_ids() -> list[str]:
    """Key ids named by stored credentials that the keyring does not hold.

    Startup uses this to fail loudly: silently disabling a cluster would make a
    deployment error look like a Proxmox outage.
    """
    available = set(keyring())
    referenced = set(
        ClusterCredential.objects.exclude(encryption_key_id="").values_list(
            "encryption_key_id", flat=True
        )
    )
    return sorted(referenced - available)


def credentials_needing_rotation() -> list[ClusterCredential]:
    """Stored credentials not sealed under the active key."""
    return list(ClusterCredential.objects.exclude(encryption_key_id=active_key_id()))


def rotate_credential(credential: ClusterCredential) -> ClusterCredential:
    """Re-seal one credential under the active key.

    Unsealing with the old key and re-sealing with the current one is what makes a
    compromised key recoverable. The token itself does not change.
    """
    try:
        plaintext = decrypt_secret(credential.token_secret_sealed)
    except MissingEncryptionKeyError:
        raise
    sealed = encrypt_secret(plaintext)
    credential.token_secret_sealed = sealed
    credential.encryption_key_id = key_id_of(sealed)
    credential.rotated_at = timezone.now()
    credential.save(update_fields=["token_secret_sealed", "encryption_key_id", "rotated_at", "updated_at"])
    return credential

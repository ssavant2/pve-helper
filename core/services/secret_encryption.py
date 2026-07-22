"""Versioned authenticated encryption for secrets stored in the database.

Cluster API tokens live in the database, so they are encrypted at rest under a key
that does not. Three properties are deliberate:

*Its own keyring, not SECRET_KEY.* Django's SECRET_KEY is rotated for session and
signing reasons and is often regenerated casually; rotating it must not make every
cluster credential unreadable.

*Every value names the key that sealed it.* A stored secret carries format version,
key id and authenticated ciphertext, so a read knows exactly which key to use and
rotation can run key-by-key without guessing.

*Ambiguity fails loudly.* A referenced key that is absent raises rather than
silently disabling a cluster, because a quietly unreachable cluster looks like a
Proxmox outage and hides a deployment error.

The keyring is the single most load-bearing secret in the deployment: `.env` is
reduced to secret plus database, and every cluster token is sealed under it, so
losing the active key makes all cluster credentials unrecoverable. Backup/escrow of
every key id still referenced by ciphertext, and the missing-key recovery
procedure, are an operational contract owned by the deployment runbook.
"""

from __future__ import annotations

import base64
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings

from core.services.public_errors import PublicMessageError

SECRET_FORMAT_VERSION = "v1"
_KEY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_AES_GCM_KEY_BYTES = 32
_AES_GCM_NONCE_BYTES = 12


class EncryptionConfigurationError(PublicMessageError, RuntimeError):
    """The encryption keyring is missing, malformed or unusable."""


class MissingEncryptionKeyError(EncryptionConfigurationError):
    """A stored secret names a key that the keyring does not contain."""


class SecretDecryptionError(PublicMessageError, RuntimeError):
    """A stored secret could not be authenticated with its named key."""


def _parse_keyring(raw: str) -> dict[str, bytes]:
    keyring: dict[str, bytes] = {}
    for entry in (item.strip() for item in raw.split(",")):
        if not entry:
            continue
        key_id, separator, encoded = entry.partition(":")
        key_id = key_id.strip()
        if not separator:
            raise EncryptionConfigurationError("PVE_HELPER_ENCRYPTION_KEYS entries must be '<key-id>:<base64-key>'.")
        if not _KEY_ID_RE.match(key_id):
            raise EncryptionConfigurationError(
                f"Invalid encryption key id {key_id!r}: use lowercase letters, digits, '-' or '_'."
            )
        try:
            key = base64.b64decode(encoded.strip(), validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise EncryptionConfigurationError(f"Encryption key {key_id!r} is not valid base64.") from exc
        if len(key) != _AES_GCM_KEY_BYTES:
            raise EncryptionConfigurationError(
                f"Encryption key {key_id!r} must be {_AES_GCM_KEY_BYTES} bytes, got {len(key)}."
            )
        if key_id in keyring:
            raise EncryptionConfigurationError(f"Encryption key id {key_id!r} is defined twice.")
        keyring[key_id] = key
    return keyring


def keyring() -> dict[str, bytes]:
    return _parse_keyring(getattr(settings, "PVE_HELPER_ENCRYPTION_KEYS", "") or "")


def active_key_id() -> str:
    """The key id new secrets are sealed under."""
    keys = keyring()
    if not keys:
        raise EncryptionConfigurationError(
            "No encryption keyring is configured. Set PVE_HELPER_ENCRYPTION_KEYS before storing cluster credentials."
        )
    configured = (getattr(settings, "PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID", "") or "").strip()
    if not configured:
        if len(keys) > 1:
            raise EncryptionConfigurationError(
                "PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID must name the key to encrypt with when "
                "the keyring holds more than one key."
            )
        return next(iter(keys))
    if configured not in keys:
        raise EncryptionConfigurationError(
            f"PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID names {configured!r}, which is not in the keyring."
        )
    return configured


def key_id_of(sealed: str) -> str:
    """The key id a stored secret names, without needing that key to be present."""
    version, key_id, _payload = _split(sealed)
    del version
    return key_id


def _split(sealed: str) -> tuple[str, str, str]:
    parts = (sealed or "").split(":", 2)
    if len(parts) != 3:
        raise SecretDecryptionError("Stored secret is malformed.")
    version, key_id, payload = parts
    if version != SECRET_FORMAT_VERSION:
        raise SecretDecryptionError(f"Unsupported stored secret format {version!r}.")
    if not key_id or not payload:
        raise SecretDecryptionError("Stored secret is malformed.")
    return version, key_id, payload


def _aad(key_id: str) -> bytes:
    # Bind the version and key id into the authentication tag so neither can be
    # edited in the stored string to point a value at a different key.
    return f"{SECRET_FORMAT_VERSION}:{key_id}".encode()


def encrypt_secret(plaintext: str) -> str:
    if plaintext == "":
        raise ValueError("Refusing to encrypt an empty secret.")
    key_id = active_key_id()
    key = keyring()[key_id]
    nonce = os.urandom(_AES_GCM_NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), _aad(key_id))
    payload = base64.b64encode(nonce + ciphertext).decode("ascii")
    return f"{SECRET_FORMAT_VERSION}:{key_id}:{payload}"


def decrypt_secret(sealed: str) -> str:
    _version, key_id, payload = _split(sealed)
    keys = keyring()
    if key_id not in keys:
        raise MissingEncryptionKeyError(
            f"Stored secret needs encryption key {key_id!r}, which is not in the keyring. "
            "Restore it from backup/escrow; the secret cannot be read without it."
        )
    try:
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise SecretDecryptionError("Stored secret payload is not valid base64.") from exc
    if len(raw) <= _AES_GCM_NONCE_BYTES:
        raise SecretDecryptionError("Stored secret payload is too short.")
    nonce, ciphertext = raw[:_AES_GCM_NONCE_BYTES], raw[_AES_GCM_NONCE_BYTES:]
    try:
        plaintext = AESGCM(keys[key_id]).decrypt(nonce, ciphertext, _aad(key_id))
    except InvalidTag as exc:
        raise SecretDecryptionError(f"Stored secret failed authentication under key {key_id!r}.") from exc
    return plaintext.decode("utf-8")


def generate_key() -> str:
    """A fresh base64 key, for the runbook's key-creation step."""
    return base64.b64encode(os.urandom(_AES_GCM_KEY_BYTES)).decode("ascii")

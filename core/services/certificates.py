"""Reading, storing and selecting the certificates this installation owns.

Operators do not have a canonical certificate format; they have whatever their CA
handed them. That is a PEM leaf plus a separate key from one issuer, a `fullchain.pem`
from another, a DER-encoded `.cer` from a Windows CA, and a password-protected `.pfx`
from an appliance export. Refusing all but one shape moves the conversion work onto a
person with `openssl` and a shell, which is the step where private keys end up in
`/tmp` and in shell history. Everything is therefore parsed here, in one place, and
the difference between the formats stops at this module's boundary.

Two rules the rest of the code depends on:

*The leaf is identified, not assumed.* A bundle's certificates arrive in no reliable
order — `fullchain.pem` is leaf-first, a PKCS#12 export often is not, and a
concatenation someone assembled by hand can be anything. The leaf is the certificate
that issued nothing else in the bundle, which is a property of the material rather
than of its layout.

*A server certificate without its matching key is rejected at import.* The alternative
is a row that looks selectable in the UI and fails inside nginx at reload, where the
error is a log line in a container the operator was not watching.
"""

from __future__ import annotations

import binascii
from dataclasses import dataclass, field
from datetime import UTC, datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs12
from django.db import transaction
from django.utils import timezone

from core.models import CertificateSettings, ManagedCertificate
from core.services.public_errors import PublicMessageError
from core.services.secret_encryption import decrypt_secret, encrypt_secret

MIN_EXPIRY_WARNING_DAYS = 1
MAX_EXPIRY_WARNING_DAYS = 99
MAX_UPLOAD_BYTES = 256 * 1024

_PEM_CERTIFICATE_MARKER = b"-----BEGIN CERTIFICATE-----"
_PEM_KEY_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN DSA PRIVATE KEY-----",
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
)


class CertificateError(PublicMessageError, ValueError):
    """Uploaded certificate material could not be used."""


@dataclass(frozen=True)
class ParsedMaterial:
    """One leaf, the intermediates below its root, and the key if one was supplied."""

    leaf: x509.Certificate
    chain: list[x509.Certificate] = field(default_factory=list)
    private_key: object | None = None


def settings_record() -> CertificateSettings:
    record, _created = CertificateSettings.objects.get_or_create(pk=CertificateSettings.SINGLETON_PK)
    return record


def expiry_warning_days() -> int:
    """The single threshold every certificate alarm in this installation uses."""
    record = settings_record()
    return int(record.expiry_warning_days)


def expiry_warnings_enabled() -> bool:
    return bool(settings_record().expiry_warning_enabled)


def update_expiry_policy(*, enabled: bool, days: int) -> CertificateSettings:
    if days < MIN_EXPIRY_WARNING_DAYS or days > MAX_EXPIRY_WARNING_DAYS:
        raise CertificateError(
            f"The warning window must be between {MIN_EXPIRY_WARNING_DAYS} and {MAX_EXPIRY_WARNING_DAYS} days."
        )
    record = settings_record()
    record.expiry_warning_enabled = enabled
    record.expiry_warning_days = days
    record.save(update_fields=["expiry_warning_enabled", "expiry_warning_days", "updated_at"])
    return record


# --------------------------------------------------------------------------- parsing


def _load_certificates(blob: bytes) -> list[x509.Certificate]:
    """Every certificate in one uploaded file, whether it is PEM, DER or a bundle."""
    if _PEM_CERTIFICATE_MARKER in blob:
        try:
            return list(x509.load_pem_x509_certificates(blob))
        except ValueError as exc:
            raise CertificateError("A PEM certificate block in the upload could not be read.") from exc
    try:
        return [x509.load_der_x509_certificate(blob)]
    except ValueError:
        return []


def _load_private_key(blob: bytes, password: str) -> object | None:
    """A private key from a PEM or DER file, decrypting it when a password was given."""
    secret = password.encode("utf-8") if password else None
    if any(marker in blob for marker in _PEM_KEY_MARKERS):
        loader = serialization.load_pem_private_key
    else:
        loader = serialization.load_der_private_key
    try:
        return loader(blob, password=secret)
    except TypeError as exc:
        # cryptography raises TypeError for both directions of the password
        # mismatch, and the two need opposite corrections from the operator.
        if secret is None:
            raise CertificateError("That private key is encrypted. Enter its password and try again.") from exc
        raise CertificateError("That private key is not encrypted, so it takes no password.") from exc
    except ValueError:
        return None


def _load_pkcs12(blob: bytes, password: str) -> ParsedMaterial | None:
    """A .pfx/.p12 export, which carries the key, the leaf and usually the chain."""
    secret = password.encode("utf-8") if password else None
    try:
        key, leaf, extra = pkcs12.load_key_and_certificates(blob, secret)
    except ValueError as exc:
        # An empty password and no password are distinct to PKCS#12, and plenty of
        # exports use the former, so both are tried before calling it a failure.
        if secret is None:
            try:
                key, leaf, extra = pkcs12.load_key_and_certificates(blob, b"")
            except ValueError:
                raise CertificateError(
                    "That PKCS#12 file could not be opened. If it is password protected, enter the password."
                ) from exc
        else:
            raise CertificateError("The PKCS#12 password was rejected.") from exc
    if leaf is None:
        raise CertificateError("That PKCS#12 file contains no certificate.")
    return ParsedMaterial(leaf=leaf, chain=list(extra or []), private_key=key)


def _looks_like_pkcs12(blob: bytes) -> bool:
    # PKCS#12 is DER, so it is only worth trying when nothing textual is present.
    return not blob.lstrip().startswith(b"-----") and blob[:1] == b"\x30"


def _order_chain(leaf: x509.Certificate, others: list[x509.Certificate]) -> list[x509.Certificate]:
    """The certificates between the leaf and its root, in the order TLS wants them."""
    remaining = list(others)
    ordered: list[x509.Certificate] = []
    current = leaf
    while remaining:
        issuer = next((candidate for candidate in remaining if candidate.subject == current.issuer), None)
        if issuer is None:
            break
        ordered.append(issuer)
        remaining.remove(issuer)
        if issuer.subject == issuer.issuer:
            break
        current = issuer
    # Anything left is unrelated to this leaf. Dropping it keeps the served chain
    # honest instead of appending certificates a client cannot use.
    return ordered


def _identify_leaf(certificates: list[x509.Certificate]) -> x509.Certificate:
    leaves = [
        certificate
        for certificate in certificates
        if not any(other is not certificate and other.issuer == certificate.subject for other in certificates)
    ]
    if len(leaves) == 1:
        return leaves[0]
    if len(certificates) == 1:
        return certificates[0]
    raise CertificateError(
        "The upload contains several unrelated certificates. Upload one certificate and its chain at a time."
    )


def parse_material(blobs: list[bytes], *, password: str = "") -> ParsedMaterial:
    """One leaf plus chain and key, from any mix of PEM, DER, key and PKCS#12 files."""
    certificates: list[x509.Certificate] = []
    private_key: object | None = None

    for blob in blobs:
        if not blob:
            continue
        if len(blob) > MAX_UPLOAD_BYTES:
            raise CertificateError("Certificate uploads are limited to 256 KiB.")
        if _looks_like_pkcs12(blob):
            found = _load_certificates(blob)
            if not found:
                material = _load_pkcs12(blob, password)
                certificates.append(material.leaf)
                certificates.extend(material.chain)
                private_key = private_key or material.private_key
                continue
            certificates.extend(found)
            continue
        certificates.extend(_load_certificates(blob))
        key = _load_private_key(blob, password)
        if key is not None:
            private_key = key

    if not certificates:
        raise CertificateError("No certificate was found in the upload. Supported: PEM, CER/DER, and PKCS#12.")

    # De-duplicate, because a leaf plus a fullchain covering it is a normal upload.
    unique: list[x509.Certificate] = []
    seen: set[bytes] = set()
    for certificate in certificates:
        der = certificate.public_bytes(serialization.Encoding.DER)
        if der not in seen:
            seen.add(der)
            unique.append(certificate)

    leaf = _identify_leaf(unique)
    chain = _order_chain(leaf, [certificate for certificate in unique if certificate is not leaf])
    return ParsedMaterial(leaf=leaf, chain=chain, private_key=private_key)


def _key_matches_certificate(private_key, certificate: x509.Certificate) -> bool:
    try:
        return private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ) == certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    except AttributeError, ValueError:
        return False


# ------------------------------------------------------------------------- describing


def fingerprint_of(certificate: x509.Certificate) -> str:
    return binascii.hexlify(certificate.fingerprint(hashes.SHA256())).decode("ascii")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _subject_alt_names(certificate: x509.Certificate) -> list[str]:
    try:
        extension = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return []
    names: list[str] = []
    names.extend(extension.value.get_values_for_type(x509.DNSName))
    names.extend(str(address) for address in extension.value.get_values_for_type(x509.IPAddress))
    return names


def _is_certificate_authority(certificate: x509.Certificate) -> bool:
    try:
        constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints)
    except x509.ExtensionNotFound:
        return False
    return bool(constraints.value.ca)


def describe(certificate: x509.Certificate) -> dict[str, object]:
    """The fields the UI shows and the expiry watch reads, from one certificate."""
    return {
        "subject": certificate.subject.rfc4514_string()[:500],
        "issuer": certificate.issuer.rfc4514_string()[:500],
        "serial_number": format(certificate.serial_number, "x")[:80],
        "sha256_fingerprint": fingerprint_of(certificate),
        "subject_alt_names": _subject_alt_names(certificate),
        "not_before": _aware(certificate.not_valid_before_utc),
        "not_after": _aware(certificate.not_valid_after_utc),
        "is_certificate_authority": _is_certificate_authority(certificate),
    }


def days_until_expiry(record: ManagedCertificate, *, now=None) -> int | None:
    if record.not_after is None:
        return None
    return (record.not_after - (now or timezone.now())).days


# --------------------------------------------------------------------------- storing


def _pem(certificate: x509.Certificate) -> str:
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


def import_certificate(
    *,
    usage: str,
    label: str,
    blobs: list[bytes],
    password: str = "",
    source_filename: str = "",
    uploaded_by: str = "",
) -> ManagedCertificate:
    """Store one uploaded certificate, with its chain and key when it needs them."""
    label = label.strip()
    if not label:
        raise CertificateError("Give the certificate a name so it can be recognised later.")
    if usage not in ManagedCertificate.Usage.values:
        raise CertificateError("Unknown certificate usage.")

    material = parse_material(blobs, password=password)
    details = describe(material.leaf)

    key_pem = ""
    if usage == ManagedCertificate.Usage.SERVER:
        if material.private_key is None:
            raise CertificateError(
                "No private key was found. Upload the key alongside the certificate, or a PKCS#12 file containing both."
            )
        if not _key_matches_certificate(material.private_key, material.leaf):
            raise CertificateError("That private key does not belong to that certificate.")
        key_pem = material.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")
    elif material.private_key is not None:
        # A CA upload that carries a key is almost always a misfired export of the
        # CA's own key material. Storing it would put a signing key in this database
        # for no purpose this feature has.
        raise CertificateError(
            "That upload contains a private key. A trusted authority needs the certificate only — "
            "export it without the key."
        )

    fingerprint = str(details["sha256_fingerprint"])
    if ManagedCertificate.objects.filter(usage=usage, sha256_fingerprint=fingerprint).exists():
        raise CertificateError("That certificate is already stored.")

    return ManagedCertificate.objects.create(
        usage=usage,
        label=label[:150],
        certificate_pem=_pem(material.leaf),
        chain_pem="".join(_pem(certificate) for certificate in material.chain),
        private_key_sealed=encrypt_secret(key_pem) if key_pem else "",
        source_filename=source_filename[:255],
        uploaded_by=uploaded_by[:150],
        **details,
    )


def private_key_pem(record: ManagedCertificate) -> str:
    if not record.private_key_sealed:
        return ""
    return decrypt_secret(record.private_key_sealed)


def full_chain_pem(record: ManagedCertificate) -> str:
    return record.certificate_pem + record.chain_pem


def select_https_certificate(record: ManagedCertificate | None, *, enabled: bool) -> CertificateSettings:
    """Choose which stored certificate terminates HTTPS, or turn HTTPS back off."""
    if enabled:
        if record is None:
            raise CertificateError("Choose a certificate before enabling HTTPS.")
        if record.usage != ManagedCertificate.Usage.SERVER:
            raise CertificateError("Only a server certificate can terminate HTTPS.")
        if not record.private_key_sealed:
            raise CertificateError("That certificate has no stored private key.")
        if record.not_after is not None and record.not_after <= timezone.now():
            raise CertificateError("That certificate has expired. Upload a current one before enabling HTTPS.")
    with transaction.atomic():
        config = settings_record()
        config.https_enabled = enabled
        config.active_certificate = record if enabled else None
        config.save(update_fields=["https_enabled", "active_certificate", "updated_at"])
    return config


def certificate_in_use(record: ManagedCertificate) -> bool:
    return settings_record().active_certificate_id == record.pk


def delete_certificate(record: ManagedCertificate) -> None:
    if certificate_in_use(record):
        raise CertificateError("That certificate is serving HTTPS. Select a different one or disable HTTPS first.")
    record.delete()

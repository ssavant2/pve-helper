"""Trust-on-first-use for the syslog destination's TLS certificate.

The forwarder used `ssl.create_default_context()`, i.e. the system trust store and
nothing else. No Compose file sets `SSL_CERT_FILE`/`SSL_CERT_DIR`, and the internal
CA bundle the deployment already mounts is exported as `REQUESTS_CA_BUNDLE`, which
`ssl` does not read. TLS to a self-hosted collector was therefore impossible and
plaintext TCP was the only transport that worked — a security control unusable in
its intended environment, pushing operators to the insecure alternative.

This module replaces the ambient decision with an explicit one, made by a human who
was shown the certificate, in the same shape `cluster_trust` uses for Proxmox
endpoints. The probe that shows it is deliberately unverified — it is SSH's
first-contact problem and has SSH's answer — so it is confined to `inspect_destination`
and can never be reached from the delivery path.
"""

from __future__ import annotations

import os
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from django.utils import timezone

from core.models import LogForwarderTransportTrust
from core.services.certificates import expiry_warning_days
from core.services.public_errors import PublicMessageError

# How much warning an operator gets before the served certificate expires.
PROBE_TIMEOUT_SECONDS = 8.0


class LogForwarderTrustError(PublicMessageError, RuntimeError):
    """A destination could not be inspected, or a trust decision is unusable."""


@dataclass(frozen=True)
class InspectedCertificate:
    """What the destination served, and what could be done with it.

    `system_trusted` answers the question the modal has to answer first: if the
    ambient trust store already verifies this collector, pinning is a choice rather
    than a necessity, and the modal says so instead of implying the operator must
    override something.
    """

    subject: str
    issuer: str
    sha256_fingerprint: str
    not_before: datetime
    not_after: datetime
    certificate_pem: str
    issuer_pem: str
    self_signed: bool
    system_trusted: bool
    verification_error: str

    @property
    def ca_available(self) -> bool:
        return bool(self.issuer_pem)

    @property
    def expires_in_days(self) -> int:
        return (self.not_after - timezone.now()).days


def trust() -> LogForwarderTransportTrust:
    record, _created = LogForwarderTransportTrust.objects.get_or_create(pk=LogForwarderTransportTrust.SINGLETON_PK)
    return record


def trust_applies_to(record: LogForwarderTransportTrust, host: str, port: int) -> bool:
    """Whether an approval was made about *this* destination.

    An approval is a statement about one collector. Re-pointing the forwarder must
    not inherit it, or the operator's "yes, I recognise that certificate" would
    silently become an answer about a host they never saw.
    """
    return record.mode != LogForwarderTransportTrust.Mode.UNSET and record.host == host and record.port == port


def ambient_ca_bundle() -> str:
    """The mounted internal CA bundle, if the deployment configured one.

    `REQUESTS_CA_BUNDLE` is what INSTALL.md and the Compose files already document
    and mount; `SSL_CERT_FILE` is the variable `ssl` itself would honour. Reading
    both means an operator who followed either convention gets the trust they
    already expressed, without a second place to configure.
    """
    for variable in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        path = (os.environ.get(variable) or "").strip()
        if not path:
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                content = handle.read().strip()
        except OSError:
            continue
        if content:
            return content
    return ""


def _base_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def system_trust_context() -> ssl.SSLContext:
    """The default store, plus the mounted internal CA bundle if there is one."""
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    bundle = ambient_ca_bundle()
    if bundle:
        try:
            context.load_verify_locations(cadata=bundle)
        except ssl.SSLError:
            # A malformed mounted bundle must not remove the system store the
            # context already has; the destination simply stays unverified by it.
            pass
    return context


def delivery_context(record: LogForwarderTransportTrust) -> ssl.SSLContext:
    """The context the delivery worker sends Audit events over.

    Raises rather than falling back for an unapproved destination: silently
    downgrading to ambient trust is how the original defect behaved, and an
    operator who has not answered the question has not agreed to anything.
    """
    mode = record.mode
    if mode == LogForwarderTransportTrust.Mode.INSECURE:
        context = _base_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    if mode in {LogForwarderTransportTrust.Mode.PINNED, LogForwarderTransportTrust.Mode.CA}:
        if not record.certificate_pem.strip():
            raise LogForwarderTrustError("The approved syslog certificate is missing; approve the destination again.")
        context = _base_context()
        # A self-signed leaf is its own anchor but carries no CA basic constraint,
        # so hostname verification is the only binding left between the approved
        # certificate and the name we dialled. Keep it on in every verifying mode.
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        try:
            context.load_verify_locations(cadata=record.certificate_pem)
        except ssl.SSLError as exc:
            raise LogForwarderTrustError("The approved syslog certificate is not valid PEM.") from exc
        return context
    raise LogForwarderTrustError(
        "The syslog destination's TLS certificate has not been approved. Open Settings → Log forwarder and approve it."
    )


def assert_pinned_match(record: LogForwarderTransportTrust, der: bytes) -> None:
    """In PINNED mode, the chain verifying is not enough — it must be *this* cert.

    `load_verify_locations` accepts a self-signed leaf as an anchor, which also
    accepts a renewal signed by the same key. PINNED promises the operator sees
    every change, so the fingerprint is compared explicitly.
    """
    if record.mode != LogForwarderTransportTrust.Mode.PINNED:
        return
    presented = x509.load_der_x509_certificate(der).fingerprint(hashes.SHA256()).hex()
    if presented != record.sha256_fingerprint:
        raise LogForwarderTrustError("The syslog destination presented a different certificate than the approved one.")


def inspect_destination(host: str, port: int, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> InspectedCertificate:
    """Connect far enough to see the certificate, and report whether it verifies.

    Two handshakes, on purpose. The unverified one exists only to *show* the
    operator what is being served — it sends nothing and ingests nothing. The
    verified one answers whether an approval is even needed. Doing it in the
    opposite order would make an untrusted collector look like a connection failure.
    """
    host = (host or "").strip()
    if not host:
        raise LogForwarderTrustError("Set a destination host before inspecting its certificate.")
    if port < 1 or port > 65535:
        raise LogForwarderTrustError("Port must be between 1 and 65535.")

    probe = _base_context()
    probe.check_hostname = False
    probe.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as connection:
            with probe.wrap_socket(connection, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
                chain = _chain_der(tls)
    except (OSError, ssl.SSLError) as exc:
        raise LogForwarderTrustError(f"Could not reach {host}:{port} to inspect its certificate.") from exc
    if not der:
        raise LogForwarderTrustError(f"{host}:{port} completed a TLS handshake without presenting a certificate.")

    certificate = x509.load_der_x509_certificate(der)
    issuer_certificate = _issuer_certificate(certificate, chain)
    system_trusted, verification_error = _verifies_with_system_trust(host, port, timeout=timeout)
    return InspectedCertificate(
        subject=certificate.subject.rfc4514_string(),
        issuer=certificate.issuer.rfc4514_string(),
        sha256_fingerprint=certificate.fingerprint(hashes.SHA256()).hex(),
        not_before=certificate.not_valid_before_utc,
        not_after=certificate.not_valid_after_utc,
        certificate_pem=_pem(certificate),
        issuer_pem=_pem(issuer_certificate) if issuer_certificate is not None else "",
        self_signed=certificate.issuer == certificate.subject,
        system_trusted=system_trusted,
        verification_error=verification_error,
    )


def _chain_der(tls: ssl.SSLSocket) -> tuple[bytes, ...]:
    """Every certificate the peer sent, leaf first.

    Needed for CA mode: `getpeercert()` returns only the leaf, and trusting "this
    certificate's issuer" requires the issuer itself. A server that sends a bare
    leaf simply has no CA option, which the modal then reflects.
    """
    try:
        return tuple(tls.get_unverified_chain() or ())
    except AttributeError, ValueError, ssl.SSLError:
        return ()


def _issuer_certificate(leaf: x509.Certificate, chain: tuple[bytes, ...]) -> x509.Certificate | None:
    if leaf.issuer == leaf.subject:
        # Self-signed: the leaf is its own anchor, and CA mode over it means
        # "trust this key to keep issuing for itself" — which is what a renewed
        # self-signed certificate from the same collector looks like.
        return leaf
    for der in chain:
        try:
            candidate = x509.load_der_x509_certificate(der)
        except ValueError:
            continue
        if candidate.subject == leaf.issuer and candidate.fingerprint(hashes.SHA256()) != leaf.fingerprint(
            hashes.SHA256()
        ):
            return candidate
    return None


def _verifies_with_system_trust(host: str, port: int, *, timeout: float) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout) as connection:
            with system_trust_context().wrap_socket(connection, server_hostname=host):
                return True, ""
    except ssl.SSLCertVerificationError as exc:
        return False, str(exc.verify_message or exc.reason or "certificate verification failed")
    except (OSError, ssl.SSLError) as exc:
        return False, exc.__class__.__name__


def _pem(certificate: x509.Certificate) -> str:
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


def approve_destination(
    *,
    mode: str,
    host: str,
    port: int,
    inspection: InspectedCertificate,
    approved_by: str = "",
) -> LogForwarderTransportTrust:
    """Persist one human decision about one destination."""
    if mode not in LogForwarderTransportTrust.Mode.values or mode == LogForwarderTransportTrust.Mode.UNSET:
        raise LogForwarderTrustError("Choose how this certificate should be trusted.")
    if mode == LogForwarderTransportTrust.Mode.CA and not inspection.ca_available:
        raise LogForwarderTrustError(
            "This destination did not send an issuer certificate, so its issuer cannot be trusted. "
            "Pin the certificate instead."
        )

    record = trust()
    record.mode = mode
    record.host = host
    record.port = port
    if mode == LogForwarderTransportTrust.Mode.CA:
        anchor = x509.load_pem_x509_certificate(inspection.issuer_pem.encode("ascii"))
        record.certificate_pem = inspection.issuer_pem
        record.sha256_fingerprint = anchor.fingerprint(hashes.SHA256()).hex()
        record.subject = anchor.subject.rfc4514_string()[:500]
        record.issuer = anchor.issuer.rfc4514_string()[:500]
        record.not_after = anchor.not_valid_after_utc
    elif mode == LogForwarderTransportTrust.Mode.PINNED:
        record.certificate_pem = inspection.certificate_pem
        record.sha256_fingerprint = inspection.sha256_fingerprint
        record.subject = inspection.subject[:500]
        record.issuer = inspection.issuer[:500]
        record.not_after = inspection.not_after
    else:
        record.certificate_pem = ""
        record.sha256_fingerprint = ""
        record.subject = inspection.subject[:500]
        record.issuer = inspection.issuer[:500]
        record.not_after = None
    record.approved_at = timezone.now()
    record.approved_by = approved_by[:150]
    record.last_checked_at = timezone.now()
    record.observed_sha256_fingerprint = inspection.sha256_fingerprint
    record.observed_subject = inspection.subject[:500]
    record.observed_not_after = inspection.not_after
    record.save()
    return record


def reset_trust(host: str = "", port: int = 0) -> LogForwarderTransportTrust:
    """Drop the approval, e.g. because the destination changed."""
    record = trust()
    record.mode = LogForwarderTransportTrust.Mode.UNSET
    record.host = host
    record.port = port
    record.certificate_pem = ""
    record.sha256_fingerprint = ""
    record.subject = ""
    record.issuer = ""
    record.not_after = None
    record.approved_at = None
    record.approved_by = ""
    record.last_checked_at = None
    record.observed_sha256_fingerprint = ""
    record.observed_subject = ""
    record.observed_not_after = None
    record.save()
    return record


def expiry_threshold(now: datetime | None = None) -> datetime:
    """The date past which a certificate counts as expiring.

    Read from the installation-wide policy on Settings -> Certificates rather than
    fixed here, so the syslog destination, the HTTPS certificate and every stored
    authority all warn on the same schedule instead of three that drift apart.
    """
    return (now or timezone.now()) + timedelta(days=expiry_warning_days())


__all__ = [
    "InspectedCertificate",
    "LogForwarderTrustError",
    "ambient_ca_bundle",
    "approve_destination",
    "assert_pinned_match",
    "delivery_context",
    "expiry_threshold",
    "inspect_destination",
    "reset_trust",
    "system_trust_context",
    "trust",
    "trust_applies_to",
]

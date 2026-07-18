"""Per-cluster transport trust, and HTTP client pools keyed by it.

`_shared_http_client()` was a process-wide singleton whose `verify` was baked in at
construction from `PVE_CA_BUNDLE or PVE_VERIFY_TLS`. Trust was therefore fixed for
the whole process, and two clusters with different CAs were impossible by
construction. This replaces it with a small pool of clients, one per distinct trust
profile, so cluster A can trust CA X while cluster B trusts CA Y.

Transport trust answers *which certificate chain the HTTP client accepts*. It is a
separate concept from cluster identity binding — *which Proxmox cluster an already
authenticated endpoint belongs to* — which lives on the cluster (the CA UUID). They
are often the same PVE CA, but not when pveproxy serves a publicly trusted
certificate while the internal cluster CA remains the identity claim. That case is
exactly this deployment: both clusters serve a Let's Encrypt wildcard, so their
transport trust is `public` while their identities differ only in the internal CA.

Pools are keyed by trust profile alone, not by credential: Proxmox API auth is a
per-request header, so a credential rotation needs no pool at all. Only the TLS
trust decision is fixed at connection time and therefore must be pooled.
"""

from __future__ import annotations

import hashlib
import ssl
import threading
from dataclasses import dataclass

import httpx
from django.conf import settings


# Transport trust modes.
TRUST_PUBLIC = "public"      # the system CA store (publicly trusted pveproxy cert)
TRUST_CA_PEM = "ca_pem"      # a specific CA bundle, trusted exclusively
TRUST_INSECURE = "insecure"  # no verification — only the credential-free inspection


class TransportTrustError(RuntimeError):
    """A trust profile could not be turned into a usable TLS decision."""


@dataclass(frozen=True)
class TrustProfile:
    """A hashable description of which certificate chain to accept.

    Frozen so it can key a pool: two endpoints resolving to equal profiles share one
    client and its keep-alive connections, and two different CAs never do.
    """

    mode: str
    ca_pem: str = ""

    def cache_key(self) -> str:
        if self.mode == TRUST_CA_PEM:
            # The CA content, not a row id, so re-approving a changed CA yields a new
            # pool rather than silently reusing the old chain.
            return f"{TRUST_CA_PEM}:{hashlib.sha256(self.ca_pem.encode('utf-8')).hexdigest()}"
        return self.mode

    def build_verify(self):
        """The value handed to httpx's `verify`."""
        if self.mode == TRUST_PUBLIC:
            return True
        if self.mode == TRUST_INSECURE:
            return False
        if self.mode == TRUST_CA_PEM:
            if not self.ca_pem.strip():
                raise TransportTrustError("A ca_pem trust profile needs a CA bundle.")
            # A fresh context that trusts *only* this CA — not the system store on
            # top of it — so "cluster A trusts CA X" means exactly that.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            try:
                context.load_verify_locations(cadata=self.ca_pem)
            except ssl.SSLError as exc:
                raise TransportTrustError(f"The configured CA bundle is not valid PEM: {exc}") from exc
            return context
        raise TransportTrustError(f"Unknown transport trust mode {self.mode!r}.")


def ssl_context_for(profile: TrustProfile) -> "ssl.SSLContext":
    """An ssl.SSLContext for a trust profile, for the WebSocket console upstream.

    The HTTP path hands `verify` to httpx; the console gateway speaks raw
    websockets and needs a context object built the same way, so one cluster's WSS
    trust can never be another's or an ambient global one.
    """
    if profile.mode == TRUST_INSECURE:
        return ssl._create_unverified_context()
    if profile.mode == TRUST_CA_PEM:
        if not profile.ca_pem.strip():
            raise TransportTrustError("A ca_pem trust profile needs a CA bundle.")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        try:
            context.load_verify_locations(cadata=profile.ca_pem)
        except ssl.SSLError as exc:
            raise TransportTrustError(f"The configured CA bundle is not valid PEM: {exc}") from exc
        return context
    return ssl.create_default_context()


def legacy_trust_profile() -> TrustProfile:
    """The compatibility profile from the global TLS settings.

    Used until a cluster has its own stored trust and until the trust cutover. It
    reproduces the old singleton's decision exactly: a CA bundle if configured, else
    verification on/off from `PVE_VERIFY_TLS`.
    """
    bundle = (settings.PVE_CA_BUNDLE or "").strip()
    if bundle:
        try:
            with open(bundle, "r", encoding="utf-8") as handle:
                return TrustProfile(mode=TRUST_CA_PEM, ca_pem=handle.read())
        except OSError as exc:
            raise TransportTrustError(f"PVE_CA_BUNDLE {bundle!r} could not be read: {exc}") from exc
    return TrustProfile(mode=TRUST_PUBLIC if settings.PVE_VERIFY_TLS else TRUST_INSECURE)


def trust_cutover_completed() -> bool:
    from core.models import RuntimeConfigurationState

    state = RuntimeConfigurationState.objects.filter(pk=RuntimeConfigurationState.SINGLETON_PK).first()
    return bool(state and state.trust_cutover_completed_at)


def resolve_trust_profile(cluster) -> TrustProfile:
    """The trust profile to reach this cluster with.

    A stored per-cluster trust wins. Before the trust cutover, a cluster without one
    falls back to the global TLS settings — a documented single-cluster
    compatibility input. After the cutover the ambient fallback is gone: a cluster
    without stored trust is a configuration error, not a reason to borrow a global
    CA decision that may not describe this cluster at all.
    """
    from core.models import ClusterTransportTrust

    stored = ClusterTransportTrust.objects.filter(cluster=cluster).first()
    if stored is not None:
        if stored.mode == ClusterTransportTrust.Mode.CA_PEM:
            return TrustProfile(mode=TRUST_CA_PEM, ca_pem=stored.ca_pem)
        return TrustProfile(mode=TRUST_PUBLIC)

    if trust_cutover_completed():
        raise TransportTrustError(
            f"Cluster '{cluster.key}' has no stored transport trust. Approve its transport; "
            "the global CA/verify settings are no longer read after the trust cutover."
        )
    return legacy_trust_profile()


# Distinct from the bootstrap and credential-cutover lock ids.
_TRUST_CUTOVER_LOCK_ID = 0x50564548424F03

_pool: dict[str, httpx.Client] = {}
_pool_lock = threading.Lock()


class _TestNetworkDisabledClient:
    def request(self, *_args, **_kwargs):
        raise AssertionError(
            "Test attempted an unmocked Proxmox HTTP request. Patch the client or use an explicit integration suite."
        )


def http_client_for(profile: TrustProfile) -> httpx.Client:
    """A pooled client for one trust profile, so connections are reused per profile."""
    if settings.PVE_TEST_NETWORK_DISABLED:
        return _TestNetworkDisabledClient()  # type: ignore[return-value]

    key = profile.cache_key()
    client = _pool.get(key)
    if client is None:
        with _pool_lock:
            client = _pool.get(key)
            if client is None:
                client = httpx.Client(
                    verify=profile.build_verify(),
                    timeout=15,
                    limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
                )
                _pool[key] = client
    return client


@dataclass(frozen=True)
class InspectedCertificate:
    subject: str
    issuer: str
    sha256_fingerprint: str


def inspect_endpoint_certificate(url: str, *, timeout: float = 8.0) -> InspectedCertificate:
    """Look at an endpoint's presented certificate without sending credentials.

    Step one of onboarding: connect only far enough to see the certificate and show
    its fingerprint, so a token is never sent over an unapproved connection. This is
    inspection, not API traffic — it must not send credentials or ingest anything.
    """
    import socket
    import ssl as ssl_module
    from urllib.parse import urlparse

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    parsed = urlparse(url if "//" in url else f"https://{url}")
    host = parsed.hostname
    port = parsed.port or 8006
    if not host:
        raise TransportTrustError(f"{url!r} has no host to inspect.")

    context = ssl_module.create_default_context()
    context.minimum_version = ssl_module.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl_module.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except (OSError, ssl_module.SSLError) as exc:
        raise TransportTrustError(f"Could not reach {host}:{port} to inspect its certificate: {exc}") from exc

    cert = x509.load_der_x509_certificate(der)
    return InspectedCertificate(
        subject=cert.subject.rfc4514_string(),
        issuer=cert.issuer.rfc4514_string(),
        sha256_fingerprint=cert.fingerprint(hashes.SHA256()).hex(),
    )


def approve_cluster_transport(cluster, *, mode: str, ca_pem: str = ""):
    """Persist a cluster's transport trust and invalidate the affected pools.

    Establishing verified TLS is a deliberate step: `public` accepts the system CA
    store (a publicly trusted pveproxy certificate), `ca_pem` trusts exactly the
    supplied internal CA and nothing else.
    """
    from django.utils import timezone

    from core.models import ClusterTransportTrust

    if mode == ClusterTransportTrust.Mode.CA_PEM and not (ca_pem or "").strip():
        raise TransportTrustError("Internal-CA trust needs a CA bundle.")
    trust, _created = ClusterTransportTrust.objects.update_or_create(
        cluster=cluster,
        defaults={
            "mode": mode,
            "ca_pem": ca_pem if mode == ClusterTransportTrust.Mode.CA_PEM else "",
            "approved_at": timezone.now(),
        },
    )
    reset_trust_pools()
    from core.services.cluster_state_identity import invalidate_cluster_cache

    invalidate_cluster_cache(cluster)
    return trust


def complete_trust_cutover() -> tuple[bool, str]:
    """Seal the legacy global TLS decision into the bootstrap cluster's trust.

    The equivalent of the credential cutover for transport: after it, the ambient
    `PVE_CA_BUNDLE`/`PVE_VERIFY_TLS` are no longer read. Same reversibility — they
    are ignored, not deleted, so a code rollback resumes reading them.
    """
    from django.db import transaction

    from core.models import (
        ClusterTransportTrust,
        ProxmoxCluster,
        RuntimeConfigurationState,
    )
    from core.services.runtime_bootstrap import _advisory_xact_lock

    with transaction.atomic():
        _advisory_xact_lock(_TRUST_CUTOVER_LOCK_ID)
        state = RuntimeConfigurationState.objects.select_for_update().filter(
            pk=RuntimeConfigurationState.SINGLETON_PK
        ).first()
        if state is None:
            return False, "The installation is not bootstrapped yet."
        if state.trust_cutover_completed_at:
            return False, "The trust cutover has already completed."
        clusters = list(ProxmoxCluster.objects.select_for_update().order_by("key"))
        if len(clusters) != 1:
            return False, f"Expected exactly one cluster for the trust cutover, found {len(clusters)}."
        cluster = clusters[0]

        legacy = legacy_trust_profile()
        if not ClusterTransportTrust.objects.filter(cluster=cluster).exists():
            if legacy.mode == TRUST_CA_PEM:
                approve_cluster_transport(
                    cluster, mode=ClusterTransportTrust.Mode.CA_PEM, ca_pem=legacy.ca_pem
                )
            else:
                approve_cluster_transport(cluster, mode=ClusterTransportTrust.Mode.PUBLIC)

        from django.utils import timezone

        state.trust_cutover_completed_at = timezone.now()
        state.save(update_fields=["trust_cutover_completed_at", "updated_at"])

    return True, f"Trust cutover complete; cluster '{cluster.key}' uses its own stored transport trust."


def reset_trust_pools() -> None:
    """Close and drop every pooled client.

    A trust change must invalidate the affected connections immediately; there are
    only a handful of profiles, so dropping all of them is simpler than tracking
    which endpoints used which and cannot leave a stale chain in use.
    """
    with _pool_lock:
        clients = list(_pool.values())
        _pool.clear()
    for client in clients:
        try:
            client.close()
        except Exception:  # pragma: no cover - best effort on teardown
            pass

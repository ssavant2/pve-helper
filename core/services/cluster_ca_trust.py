"""Adopting a cluster's own CA as an additional transport anchor.

Proxmox's own client does not verify its nodes against the public CA store. At
`pvecm add` the operator confirms the cluster CA's fingerprint once, and from then
on every node is verified against `pve-root-ca.pem`. A cluster whose nodes are split
— one serving a publicly trusted pveproxy certificate, the rest serving the default
internal one — is therefore ordinary in Proxmox and was inexpressible here: `public`
rejects the internal nodes and `ca_pem` is exclusive and would reject the public one.

This module is the repair, and its whole security argument is *where the CA comes
from*: an endpoint of this same connection whose chain the current profile already
accepts, over the credentialed API. The UUID comparison against the pinned identity
is a sanity check on top of that — a `subject` string is written by whoever holds the
endpoint, so a CA fetched from the failing endpoint would pass it trivially. The
verified channel is the boundary; the UUID only catches fetching the wrong cluster's
CA. Neither check is meaningful without the other, so both are mandatory here.
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes

from core.models import ClusterTransportTrust
from core.services.cluster_credentials import ClusterCredentialError
from core.services.cluster_identity import ClusterIdentityError, ca_uuid_in, extract_root_ca
from core.services.cluster_resolver import client_for_endpoint, enabled_endpoints
from core.services.cluster_trust import (
    TransportTrustError,
    accepted_endpoint_certificate,
    approve_cluster_transport,
    resolve_trust_profile,
)
from core.services.proxmox import ProxmoxAPIError
from core.services.public_errors import PublicMessageError, public_failure


class ClusterCaTrustError(PublicMessageError, RuntimeError):
    """The cluster's own CA could not be adopted as a transport anchor."""


@dataclass(frozen=True)
class AdoptedClusterCA:
    """What was trusted, from where, as the operator was shown it."""

    ca_uuid: str
    subject: str
    fingerprint: str
    not_after: str
    source_endpoint: str

    def as_details(self) -> dict:
        return {
            "ca_uuid": self.ca_uuid,
            "subject": self.subject,
            "fingerprint": self.fingerprint,
            "not_after": self.not_after,
            "source_endpoint": self.source_endpoint,
        }


def _normalized(fingerprint: str) -> str:
    return str(fingerprint or "").replace(":", "").strip().lower()


def _parse_ca(pem: str, endpoint_name: str):
    """The PEM as an actual certificate, refused if it is not one, or not a CA.

    Storing an unparseable bundle is worse than refusing it: the profile is
    connection-wide, and `build_verify()` raises on every later request, so one bad
    `certificates/info` response would take down the endpoint that *was* working
    along with the ones that were not — with no UI to undo it.
    """
    from cryptography import x509

    try:
        certificate = x509.load_pem_x509_certificate(pem.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise ClusterCaTrustError(f"{endpoint_name} returned a cluster CA that is not a valid certificate.") from exc
    try:
        basic_constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound as exc:
        raise ClusterCaTrustError(f"{endpoint_name} returned a certificate that does not declare itself a CA.") from exc
    if not basic_constraints.ca:
        raise ClusterCaTrustError(f"{endpoint_name} returned a leaf certificate, not a CA. Nothing was trusted.")
    return certificate


def _verifying_endpoint(cluster):
    """The first endpoint of this connection whose chain the profile accepts today.

    Not "the first enabled endpoint": a sibling that has itself drifted out of trust
    is exactly as untrustworthy a source as the endpoint that just failed. The
    handshake is asked, not assumed.
    """
    profile = resolve_trust_profile(cluster)
    # Capped like `_first_trust_reference`: one working source is all this needs, and
    # each probe carries a 5 s timeout on a synchronous request. An all-failing
    # connection would otherwise hang the page for endpoints × 5 s.
    for endpoint in list(enabled_endpoints(cluster))[:4]:
        if accepted_endpoint_certificate(endpoint.url, profile).sha256_fingerprint:
            return endpoint
    return None


def adopt_cluster_ca(cluster) -> AdoptedClusterCA:
    """Fetch this cluster's CA over a verified endpoint and trust it additively.

    Raises rather than returning a failure, because every refusal here is a state the
    operator has to act on: nothing to compare against, nothing verified to fetch
    over, or a CA that is not this cluster's.
    """
    if not cluster.discovered_ca_uuid:
        # An unpinned cluster has nothing to check the fetched CA against, and
        # binding the identity from a chain we are in the middle of refusing is
        # precisely the trust-on-first-use this design exists to avoid.
        raise ClusterCaTrustError(
            f"Connection '{cluster.key}' has no pinned Proxmox CA identity yet, so a fetched CA cannot be "
            "checked against anything. Verify one endpoint of this connection first."
        )

    try:
        endpoint = _verifying_endpoint(cluster)
    except TransportTrustError as exc:
        # The raise site owns what it says; the original text goes to the log.
        raise ClusterCaTrustError(
            f"Connection '{cluster.key}' has no usable transport trust to probe its endpoints with."
        ) from exc
    if endpoint is None:
        raise ClusterCaTrustError(
            f"No endpoint of connection '{cluster.key}' currently presents a certificate this connection "
            "accepts, so there is no verified channel to fetch its CA over. Repair one endpoint's "
            "certificate, or approve a CA bundle with 'manage.py approve_cluster_transport'."
        )

    try:
        client = client_for_endpoint(endpoint)
        node = client.discover_node_name(endpoint.name)
        entries = client.get(f"nodes/{node}/certificates/info")
        root_ca = extract_root_ca(entries)
    except (ClusterIdentityError, ClusterCredentialError, ProxmoxAPIError, TransportTrustError) as exc:
        # Everything from resolving the credential to finding the CA entry. Each of
        # these was reachable from an ordinary endpoint response and left the view
        # with an exception it does not catch — a 500 instead of the refusal, and no
        # audit record of the attempt.
        raise ClusterCaTrustError(
            f"{endpoint.name} could not produce its cluster CA: "
            f"{public_failure(exc, operation='cluster_ca_trust.certificates_info').message}"
        ) from exc

    pem = str(root_ca.get("pem") or "").strip()
    if not pem:
        raise ClusterCaTrustError(f"{endpoint.name} reported a cluster CA without its PEM, so nothing can be trusted.")

    certificate = _parse_ca(pem, endpoint.name)
    # Everything the operator is shown, everything stored, and everything compared
    # comes from the certificate itself — never from the sibling JSON fields beside
    # it. Those are written by whoever holds the endpoint and are not bound to the
    # PEM, so a response could otherwise present the fingerprint of one certificate
    # while installing another, and the consent record would attest to neither.
    observed_uuid = ca_uuid_in(certificate.subject.rfc4514_string())
    if not observed_uuid or observed_uuid != cluster.discovered_ca_uuid:
        raise ClusterCaTrustError(
            f"{endpoint.name} offered CA {observed_uuid or 'without a UUID'}, but '{cluster.key}' is pinned to "
            f"{cluster.discovered_ca_uuid}. Nothing was trusted."
        )

    fingerprint = certificate.fingerprint(hashes.SHA256()).hex()
    pinned_fingerprint = _normalized(cluster.discovered_ca_fingerprint)
    if pinned_fingerprint and pinned_fingerprint != fingerprint:
        # Defence in depth rather than the boundary: the pinned fingerprint is
        # refreshed on drift under a matching UUID, so it is not a hard pin. When it
        # is present and disagrees, the UUID alone is not enough to proceed on.
        raise ClusterCaTrustError(
            f"{endpoint.name} offered a CA whose fingerprint is not the one pinned for '{cluster.key}'. "
            "Nothing was trusted."
        )

    adopted = AdoptedClusterCA(
        ca_uuid=observed_uuid,
        subject=certificate.subject.rfc4514_string(),
        fingerprint=fingerprint,
        not_after=certificate.not_valid_after_utc.date().isoformat(),
        source_endpoint=endpoint.name,
    )
    approve_cluster_transport(
        cluster,
        mode=ClusterTransportTrust.Mode.PUBLIC_PLUS_CA,
        ca_pem=pem,
        details=adopted.as_details(),
    )
    return adopted

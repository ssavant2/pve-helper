"""Cluster identity binding: which Proxmox cluster an endpoint really speaks for.

Discovered metadata must not *define* identity — the operator-chosen `cluster_key`
does that — but it must *confirm* it. On connect and refresh, the cluster CA an
endpoint reports is compared against what was pinned for that key. On mismatch the
cluster is quarantined and ingestion stops, because a re-pointed, mistyped or
restored endpoint would otherwise merge a different cluster's guests under an
existing key, which is exactly how a cross-cluster write happens.

The anchor is the cluster CA, not the TLS handshake chain. `cluster/status` is too
weak — it has no UUID, and its `name`/`version` are mutable. The CA is reachable
over the API at `nodes/{node}/certificates/info` as `pve-root-ca.pem`, carrying a
fingerprint and a UUID in its subject OU. Crucially it is fetched from the node
beside whatever certificate pveproxy serves, so it works even when pveproxy serves
a publicly trusted certificate and the cluster CA never appears in the handshake —
verified against this deployment, where both clusters serve a Let's Encrypt
wildcard.

This is trust-on-first-use plus pinning, so a CA rotation and a re-pointed endpoint
look alike from outside and both must stop ingestion until a human confirms which
happened. A standalone node has its own self-generated CA and so gets a UUID of its
own; the model needs no special case.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from django.utils import timezone

from core.services.cluster_resolver import client_for_endpoint, enabled_endpoints
from core.services.proxmox import ProxmoxAPIError
from core.services.public_errors import PublicMessageError, public_failure

# The cluster CA subject looks like:
#   OU=<uuid>,O=PVE Cluster Manager CA
_CA_UUID_RE = re.compile(r"OU\s*=\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")


class ClusterIdentityError(PublicMessageError, RuntimeError):
    """The cluster CA could not be discovered."""


class ClusterIdentityMismatch(PublicMessageError, RuntimeError):
    """An endpoint reported a different cluster CA than the pinned one."""

    def __init__(self, message: str, *, observed_uuid: str, pinned_uuid: str):
        super().__init__(message)
        self.observed_uuid = observed_uuid
        self.pinned_uuid = pinned_uuid


class ClusterIdentityCollision(ClusterIdentityError):
    """The observed physical cluster is already registered under another key."""


@dataclass(frozen=True)
class ObservedClusterIdentity:
    ca_uuid: str
    ca_fingerprint: str


def _assert_ca_uuid_is_unclaimed(cluster, observed: ObservedClusterIdentity) -> None:
    from core.models import ProxmoxCluster

    owner = (
        ProxmoxCluster.objects.filter(
            discovered_ca_uuid=observed.ca_uuid,
        )
        .exclude(pk=cluster.pk)
        .first()
    )
    if owner is not None:
        raise ClusterIdentityCollision(
            f"Cluster CA {observed.ca_uuid} is already registered as '{owner.key}'. "
            "One physical Proxmox cluster cannot be registered under two keys."
        )


def _extract_root_ca(entries) -> dict:
    if not isinstance(entries, list):
        raise ClusterIdentityError("certificates/info returned an unexpected response.")
    for entry in entries:
        if isinstance(entry, dict) and entry.get("filename") == "pve-root-ca.pem":
            return entry
    raise ClusterIdentityError("No pve-root-ca.pem was present in certificates/info.")


def discover_cluster_identity(client, node: str) -> ObservedClusterIdentity:
    """Fetch and parse the cluster CA from one node.

    Uses normal verified transport and the cluster's credential — the credential-free
    inspection is a separate transport-approval step, not this identity read.
    """
    from urllib.parse import quote

    try:
        entries = client.get(f"nodes/{quote(node, safe='')}/certificates/info")
    except ProxmoxAPIError as exc:
        raise ClusterIdentityError(
            f"Could not read certificates/info from {node}: "
            f"{public_failure(exc, operation='cluster_identity.certificates_info').message}"
        ) from exc

    root_ca = _extract_root_ca(entries)
    subject = str(root_ca.get("subject") or "")
    match = _CA_UUID_RE.search(subject)
    if not match:
        raise ClusterIdentityError(f"The cluster CA subject on {node} has no UUID: {subject!r}.")
    fingerprint = str(root_ca.get("fingerprint") or "").strip()
    return ObservedClusterIdentity(ca_uuid=match.group(1).lower(), ca_fingerprint=fingerprint)


def observe_cluster_identity(cluster) -> ObservedClusterIdentity:
    """Discover the CA identity from whichever of the cluster's endpoints answers.

    Fails over across the cluster's enabled endpoints exactly as a read does: the
    cluster CA is the same from any member, so one down endpoint must not prevent
    identity verification while another member is reachable. Raises only when no
    endpoint could produce a CA.
    """
    errors: list[str] = []
    for endpoint in enabled_endpoints(cluster):
        client = client_for_endpoint(endpoint)
        try:
            node = client.discover_node_name(endpoint.name)
            return discover_cluster_identity(client, node)
        except (ClusterIdentityError, ProxmoxAPIError) as exc:
            errors.append(f"{endpoint.name}: {public_failure(exc, operation='cluster_identity.observe').message}")
    raise ClusterIdentityError(
        f"No endpoint of cluster '{cluster.key}' could report its CA: {'; '.join(errors) or 'no endpoints'}"
    )


def verify_or_bind_identity(cluster, observed: ObservedClusterIdentity) -> str:
    """Compare an observed identity against the pinned one, binding on first use.

    Returns one of ``"bound"`` (first approval, now pinned), ``"match"`` (unchanged)
    or raises :class:`ClusterIdentityMismatch` and quarantines the cluster. A
    mismatch is a surfaced state, not a log line: it sets ``ingestion_quarantined``,
    which the read model and health checks expose and which halts ingestion until an
    explicit re-approval.
    """
    _assert_ca_uuid_is_unclaimed(cluster, observed)
    if not cluster.discovered_ca_uuid:
        cluster.discovered_ca_uuid = observed.ca_uuid
        cluster.discovered_ca_fingerprint = observed.ca_fingerprint
        cluster.ingestion_quarantined = False
        cluster.quarantine_reason = ""
        cluster.quarantined_at = None
        cluster.save(
            update_fields=[
                "discovered_ca_uuid",
                "discovered_ca_fingerprint",
                "ingestion_quarantined",
                "quarantine_reason",
                "quarantined_at",
                "updated_at",
            ]
        )
        return "bound"

    if observed.ca_uuid != cluster.discovered_ca_uuid:
        reason = (
            f"Endpoint reported cluster CA {observed.ca_uuid} but '{cluster.key}' is pinned to "
            f"{cluster.discovered_ca_uuid}. Ingestion halted pending re-approval."
        )
        cluster.ingestion_quarantined = True
        cluster.quarantine_reason = reason[:255]
        cluster.quarantined_at = timezone.now()
        cluster.save(update_fields=["ingestion_quarantined", "quarantine_reason", "quarantined_at", "updated_at"])
        raise ClusterIdentityMismatch(reason, observed_uuid=observed.ca_uuid, pinned_uuid=cluster.discovered_ca_uuid)

    # A rotated fingerprint under the same UUID is a legitimate CA renewal; keep the
    # UUID pinned and refresh the anchor. A UUID change is the dangerous case above.
    if observed.ca_fingerprint and observed.ca_fingerprint != cluster.discovered_ca_fingerprint:
        cluster.discovered_ca_fingerprint = observed.ca_fingerprint
        cluster.save(update_fields=["discovered_ca_fingerprint", "updated_at"])
    return "match"


def reapprove_identity(cluster, observed: ObservedClusterIdentity) -> None:
    """Re-pin the currently observed identity and lift quarantine.

    The explicit human confirmation that a CA rotation or an intended re-point
    happened — not a re-point to the wrong cluster.
    """
    _assert_ca_uuid_is_unclaimed(cluster, observed)
    cluster.discovered_ca_uuid = observed.ca_uuid
    cluster.discovered_ca_fingerprint = observed.ca_fingerprint
    cluster.ingestion_quarantined = False
    cluster.quarantine_reason = ""
    cluster.quarantined_at = None
    cluster.save(
        update_fields=[
            "discovered_ca_uuid",
            "discovered_ca_fingerprint",
            "ingestion_quarantined",
            "quarantine_reason",
            "quarantined_at",
            "updated_at",
        ]
    )

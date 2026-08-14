"""Adopting a cluster's own CA as an additional transport anchor (5a1K).

Every test here is about a refusal or about where the CA came from, because that is
the whole security content of the feature: the fetch happens over a channel this
connection already verifies, and the UUID comparison is the sanity check on top.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import TestCase

from core.models import ClusterTransportTrust, ProxmoxCluster, ProxmoxEndpoint
from core.services.cluster_ca_trust import ClusterCaTrustError, adopt_cluster_ca
from core.services.cluster_trust import TRUST_PUBLIC_PLUS_CA, InspectedCertificate, resolve_trust_profile

PINNED = "e4b043e3-0ea5-40f6-8cde-6c9812897ad0"


def _certificate(*, uuid: str = PINNED, ca: bool = True) -> tuple[str, str]:
    """A real CA PEM and its SHA-256, because the service now parses what it stores.

    A string that merely looks like PEM was the wrong fixture: it let the tests pass
    while the code stored a bundle that raises on every later connection.
    """
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "Proxmox Virtual Environment"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, uuid),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "PVE Cluster Manager CA"),
        ]
    )
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        certificate.fingerprint(hashes.SHA256()).hex(),
    )


CA_PEM, CA_FINGERPRINT = _certificate()


def _certificates_info(*, uuid: str = PINNED, pem: str | None = None) -> list[dict]:
    if pem is None:
        pem = CA_PEM if uuid == PINNED else _certificate(uuid=uuid)[0]
    return [
        {
            "filename": "pve-root-ca.pem",
            # Deliberately disagreeing with the PEM beside it: these fields are
            # written by whoever holds the endpoint, and nothing may be read from
            # them. Every assertion below is about the parsed certificate instead.
            "subject": "/CN=Someone Else/OU=00000000-0000-4000-8000-000000000000/O=Not This CA",
            "fingerprint": "AA:BB",
            "notafter": "1800000000",
            "pem": pem,
        },
        {"filename": "pve-ssl.pem", "subject": "/CN=pve201", "pem": "leaf"},
    ]


class AdoptClusterCaTests(TestCase):
    def setUp(self):
        self.cluster = ProxmoxCluster.objects.create(
            key="clusterca", display_name="Cluster CA", discovered_ca_uuid=PINNED
        )
        ClusterTransportTrust.objects.create(cluster=self.cluster, mode=ClusterTransportTrust.Mode.PUBLIC)
        self.endpoint = ProxmoxEndpoint.objects.create(
            cluster=self.cluster, name="pve201", url="https://pve201.example.test:8006", enabled=True
        )

    def _adopt(self, *, verifies=True, entries=None, side_effect=None, client_error=None):
        accepted = InspectedCertificate(
            subject="CN=pve201", issuer="CN=R11", sha256_fingerprint="ab01" if verifies else ""
        )
        client = MagicMock()
        client.discover_node_name.return_value = "pve201"
        if side_effect is not None:
            client.get.side_effect = side_effect
        else:
            client.get.return_value = _certificates_info() if entries is None else entries
        factory = {"side_effect": client_error} if client_error is not None else {"return_value": client}
        with (
            patch("core.services.cluster_ca_trust.accepted_endpoint_certificate", return_value=accepted),
            patch("core.services.cluster_ca_trust.client_for_endpoint", **factory),
        ):
            return adopt_cluster_ca(self.cluster)

    def test_the_ca_is_trusted_additively_with_its_evidence(self):
        adopted = self._adopt()

        stored = ClusterTransportTrust.objects.get(cluster=self.cluster)
        self.assertEqual(stored.mode, ClusterTransportTrust.Mode.PUBLIC_PLUS_CA)
        self.assertEqual(stored.ca_pem, CA_PEM.strip())
        self.assertEqual(stored.details["fingerprint"], CA_FINGERPRINT)
        self.assertEqual(stored.details["ca_uuid"], PINNED)
        self.assertEqual(stored.details["source_endpoint"], "pve201")
        self.assertEqual(adopted.source_endpoint, "pve201")
        # And the connection now resolves to the additive profile, which is the only
        # thing that changes what the HTTP client accepts.
        self.assertEqual(resolve_trust_profile(self.cluster).mode, TRUST_PUBLIC_PLUS_CA)

    def test_an_unpinned_connection_is_refused(self):
        """Without a pin there is nothing to compare the fetched CA against, and
        binding one from an unverified chain is exactly the TOFU this avoids."""

        ProxmoxCluster.objects.filter(pk=self.cluster.pk).update(discovered_ca_uuid="")
        self.cluster.refresh_from_db()

        with self.assertRaises(ClusterCaTrustError):
            self._adopt()
        self.assertEqual(
            ClusterTransportTrust.objects.get(cluster=self.cluster).mode, ClusterTransportTrust.Mode.PUBLIC
        )

    def test_an_unpinned_connection_cannot_be_bound_by_a_ca_without_a_uuid(self):
        """The empty-pin guard's real work, and the reason it is not redundant with
        the UUID comparison: an empty pin equals an unparseable subject, so without
        the guard a CA carrying no identity at all would compare equal and be
        adopted — trust-on-first-use over a channel we cannot check."""

        ProxmoxCluster.objects.filter(pk=self.cluster.pk).update(discovered_ca_uuid="")
        self.cluster.refresh_from_db()

        with self.assertRaises(ClusterCaTrustError):
            self._adopt(entries=_certificates_info(pem=_certificate(uuid="0")[0]))
        self.assertEqual(
            ClusterTransportTrust.objects.get(cluster=self.cluster).mode, ClusterTransportTrust.Mode.PUBLIC
        )

    def test_a_connection_with_no_verifying_endpoint_is_refused(self):
        """A sibling that has itself drifted out of trust is exactly as untrustworthy
        a source as the endpoint that just failed."""

        with self.assertRaises(ClusterCaTrustError):
            self._adopt(verifies=False)
        self.assertEqual(
            ClusterTransportTrust.objects.get(cluster=self.cluster).mode, ClusterTransportTrust.Mode.PUBLIC
        )

    def test_another_clusters_ca_is_refused(self):
        with self.assertRaises(ClusterCaTrustError):
            self._adopt(entries=_certificates_info(uuid="99999999-9999-4999-8999-999999999999"))
        self.assertEqual(
            ClusterTransportTrust.objects.get(cluster=self.cluster).mode, ClusterTransportTrust.Mode.PUBLIC
        )

    def test_a_ca_without_its_pem_is_refused(self):
        with self.assertRaises(ClusterCaTrustError):
            self._adopt(entries=_certificates_info(pem="  "))

    def test_a_provider_failure_is_reported_without_leaking_the_exception(self):
        from core.services.proxmox import ProxmoxAPIError

        with self.assertRaises(ClusterCaTrustError) as caught:
            self._adopt(side_effect=ProxmoxAPIError("token 12345 rejected by upstream"))

        self.assertNotIn("12345", str(caught.exception))

    def test_a_malformed_pem_is_refused_rather_than_stored(self):
        """Storing it is worse than refusing it: the profile is connection-wide, so
        an unparseable bundle makes every later request raise — including on the
        endpoint that was working — and there is no UI to undo that."""

        with self.assertRaises(ClusterCaTrustError):
            self._adopt(entries=_certificates_info(pem="-----BEGIN CERTIFICATE-----\nnope\n-----END CERTIFICATE-----"))
        self.assertEqual(
            ClusterTransportTrust.objects.get(cluster=self.cluster).mode, ClusterTransportTrust.Mode.PUBLIC
        )

    def test_a_leaf_certificate_is_not_accepted_as_a_ca(self):
        with self.assertRaises(ClusterCaTrustError):
            self._adopt(entries=_certificates_info(pem=_certificate(ca=False)[0]))

    def test_the_evidence_comes_from_the_certificate_not_the_json_beside_it(self):
        """The endpoint writes `subject` and `fingerprint` freely, and neither is
        bound to the PEM. Reading them would let a response display one certificate
        while installing another, and the consent record would attest to neither."""

        adopted = self._adopt()

        self.assertEqual(adopted.fingerprint, CA_FINGERPRINT)
        self.assertNotIn("AA:BB", adopted.fingerprint)
        self.assertIn("PVE Cluster Manager CA", adopted.subject)
        self.assertNotIn("Not This CA", adopted.subject)

    def test_a_ca_that_is_not_the_pinned_fingerprint_is_refused(self):
        """Defence in depth on top of the UUID: the pin is refreshed on drift under a
        matching UUID, so it is not a hard pin — but when it is present and disagrees,
        the UUID alone is not enough to proceed on."""

        other_pem, other_fingerprint = _certificate()
        ProxmoxCluster.objects.filter(pk=self.cluster.pk).update(discovered_ca_fingerprint=other_fingerprint)
        self.cluster.refresh_from_db()

        with self.assertRaises(ClusterCaTrustError):
            self._adopt()
        self.assertNotEqual(other_pem, CA_PEM)
        self.assertEqual(
            ClusterTransportTrust.objects.get(cluster=self.cluster).mode, ClusterTransportTrust.Mode.PUBLIC
        )

    def test_a_certificate_list_without_a_root_ca_is_refused_not_raised_raw(self):
        """It reaches the view, which catches only this class — the alternative was a
        500 and no audit record of the attempt."""

        with self.assertRaises(ClusterCaTrustError):
            self._adopt(entries=[{"filename": "pve-ssl.pem", "subject": "/CN=pve201"}])

    def test_an_unreadable_credential_is_refused_not_raised_raw(self):
        from core.services.cluster_credentials import ClusterCredentialError

        with self.assertRaises(ClusterCaTrustError):
            self._adopt(client_error=ClusterCredentialError("no credential is configured"))

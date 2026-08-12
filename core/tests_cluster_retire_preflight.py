"""R3 retirement preflight: selected identity read and signed local evidence."""

from unittest.mock import patch

from django.core import signing
from django.test import TestCase
from django.utils import timezone

from core.models import (
    ClusterCredential,
    ClusterTransportTrust,
    ProxmoxCluster,
    ProxmoxEndpoint,
    ScanRun,
    StorageCatalogState,
)
from core.services.cluster_retirement import (
    ERROR_CODE_PREFLIGHT_IDENTITY_MISMATCH,
    ERROR_CODE_PREFLIGHT_UNREACHABLE,
    RETIREMENT_PREFLIGHT_SALT,
    RetirementPreflightChanged,
    RetirementPreflightEndpointError,
    RetirementPreflightIdentityMismatch,
    RetirementPreflightInvalid,
    RetirementPreflightNotAllowed,
    RetirementPreflightUnavailable,
    cluster_retirement_preflight,
    validate_retirement_preflight,
)
from core.services.proxmox import ProxmoxAPIError, ProxmoxClient
from core.services.public_errors import public_failure

CA_UUID = "11111111-1111-1111-1111-111111111111"
OTHER_CA_UUID = "22222222-2222-2222-2222-222222222222"


class IdentityClient(ProxmoxClient):
    def __init__(self, *, node_count: int = 1, ca_uuid: str = CA_UUID, error: str = ""):
        super().__init__("https://selected.example.test:8006")
        self.node_count = node_count
        self.ca_uuid = ca_uuid
        self.error = error
        self.calls: list[str] = []

    def get(self, path: str, *, timeout=None):
        self.calls.append(path)
        if self.error:
            raise ProxmoxAPIError(self.error)
        if path == "nodes":
            return [{"node": f"pve{index}"} for index in range(1, self.node_count + 1)]
        if path.startswith("nodes/") and path.endswith("/certificates/info"):
            return [
                {
                    "filename": "pve-root-ca.pem",
                    "fingerprint": "AA:11",
                    "subject": (f"/CN=Proxmox Virtual Environment/OU={self.ca_uuid}/O=PVE Cluster Manager CA"),
                }
            ]
        raise AssertionError(f"Unexpected provider path: {path}")


class ClusterRetirementPreflightTests(TestCase):
    def setUp(self):
        self.cluster = ProxmoxCluster.objects.create(
            key="retiring",
            display_name="Retiring",
            enabled=False,
            discovered_ca_uuid=CA_UUID,
            discovered_ca_fingerprint="AA:11",
        )
        self.endpoint = ProxmoxEndpoint.objects.create(
            cluster=self.cluster,
            name="pve1",
            url="https://pve1.retiring.test:8006/",
        )
        self.credential = ClusterCredential.objects.create(
            cluster=self.cluster,
            token_id="retire@pve!helper",
            token_secret_sealed="not-read-by-these-tests",
            encryption_key_id="test",
        )
        self.trust = ClusterTransportTrust.objects.create(
            cluster=self.cluster,
            mode=ClusterTransportTrust.Mode.PUBLIC,
        )

    def _verified(self, client=None):
        client = client or IdentityClient()
        with patch("core.services.cluster_retirement.client_for_endpoint", return_value=client):
            result = cluster_retirement_preflight(
                self.cluster,
                mode=ProxmoxCluster.RetirementMode.VERIFIED,
                endpoint_id=self.endpoint.pk,
            )
        return result, client

    def test_verified_preflight_reads_only_the_selected_endpoint_in_two_calls(self):
        for node_count in (1, 3, 20):
            with self.subTest(node_count=node_count):
                result, client = self._verified(IdentityClient(node_count=node_count))

                self.assertTrue(result.gate_clear)
                self.assertEqual(result.identity_verification, "matched")
                self.assertEqual(
                    client.calls,
                    ["nodes", "nodes/pve1/certificates/info"],
                )
                self.assertTrue(result.confirmation)

    def test_verified_preflight_defaults_to_the_most_recently_healthy_endpoint(self):
        second = ProxmoxEndpoint.objects.create(
            cluster=self.cluster,
            name="pve2",
            url="https://pve2.retiring.test:8006/",
            last_health_status="ok",
            last_successful_scan=timezone.now(),
        )
        client = IdentityClient()
        with patch("core.services.cluster_retirement.client_for_endpoint", return_value=client) as selected:
            result = cluster_retirement_preflight(
                self.cluster,
                mode=ProxmoxCluster.RetirementMode.VERIFIED,
            )

        self.assertEqual(result.endpoint_id, second.pk)
        self.assertEqual(client.calls, ["nodes", "nodes/pve1/certificates/info"])
        self.assertEqual(selected.call_args.args[0].pk, second.pk)

    def test_signed_evidence_binds_the_finalized_fields_and_validates_without_provider_io(self):
        result, _client = self._verified()
        payload = signing.loads(result.confirmation, salt=RETIREMENT_PREFLIGHT_SALT)

        self.assertEqual(
            set(payload),
            {
                "version",
                "cluster_pk",
                "cluster_key",
                "mode",
                "endpoint_id",
                "pinned_ca_uuid",
                "lifecycle_generation",
                "credential_version",
                "trust_version",
                "storage_impact_digest",
                "issued_at",
                "identity_verification",
                "replacement_ca_uuid",
            },
        )
        self.assertEqual(payload["cluster_pk"], self.cluster.pk)
        self.assertEqual(payload["cluster_key"], self.cluster.key)
        self.assertEqual(payload["endpoint_id"], self.endpoint.pk)
        self.assertEqual(payload["pinned_ca_uuid"], CA_UUID)
        self.assertEqual(payload["storage_impact_digest"], result.storage.impact_digest)

        with patch(
            "core.services.cluster_retirement.client_for_endpoint",
            side_effect=AssertionError("validation must not contact Proxmox"),
        ):
            confirmed = validate_retirement_preflight(
                result.confirmation,
                cluster=self.cluster,
                mode=ProxmoxCluster.RetirementMode.VERIFIED,
            )
        self.assertEqual(confirmed.endpoint_id, self.endpoint.pk)
        self.assertEqual(confirmed.storage_impact_digest, result.storage.impact_digest)

    def test_forced_preflight_is_provider_free_and_can_start_while_enabled(self):
        self.cluster.enabled = True
        self.cluster.save(update_fields=["enabled", "updated_at"])

        with (
            patch(
                "core.services.cluster_retirement.client_for_endpoint",
                side_effect=AssertionError("forced preflight must not construct a provider client"),
            ),
            patch(
                "core.services.cluster_retirement.discover_cluster_identity",
                side_effect=AssertionError("forced preflight must not read provider identity"),
            ),
        ):
            result = cluster_retirement_preflight(
                self.cluster,
                mode=ProxmoxCluster.RetirementMode.FORCED,
            )
            confirmed = validate_retirement_preflight(
                result.confirmation,
                cluster=self.cluster,
                mode=ProxmoxCluster.RetirementMode.FORCED,
            )

        self.assertTrue(result.gate_clear)
        self.assertEqual(result.identity_verification, "skipped")
        self.assertIsNone(result.endpoint_id)
        self.assertIsNone(confirmed.endpoint_id)
        self.assertEqual(confirmed.pinned_ca_uuid, "")

    def test_verified_preflight_requires_disabled_cluster_and_selected_owned_endpoint(self):
        self.cluster.enabled = True
        self.cluster.save(update_fields=["enabled", "updated_at"])
        with self.assertRaises(RetirementPreflightNotAllowed):
            cluster_retirement_preflight(
                self.cluster,
                mode=ProxmoxCluster.RetirementMode.VERIFIED,
                endpoint_id=self.endpoint.pk,
            )

        self.cluster.enabled = False
        self.cluster.save(update_fields=["enabled", "updated_at"])
        other = ProxmoxCluster.objects.create(key="other", display_name="Other", enabled=False)
        other_endpoint = ProxmoxEndpoint.objects.create(
            cluster=other,
            name="pve1",
            url="https://pve1.other.test:8006/",
        )
        with self.assertRaises(RetirementPreflightEndpointError):
            cluster_retirement_preflight(
                self.cluster,
                mode=ProxmoxCluster.RetirementMode.VERIFIED,
                endpoint_id=other_endpoint.pk,
            )

    def test_identity_mismatch_has_stable_public_mapping(self):
        with patch(
            "core.services.cluster_retirement.client_for_endpoint",
            return_value=IdentityClient(ca_uuid=OTHER_CA_UUID),
        ):
            with self.assertRaises(RetirementPreflightIdentityMismatch) as raised:
                cluster_retirement_preflight(
                    self.cluster,
                    mode=ProxmoxCluster.RetirementMode.VERIFIED,
                    endpoint_id=self.endpoint.pk,
                )

        failure = public_failure(raised.exception, operation="cluster_retirement.preflight")
        self.assertEqual(failure.code, ERROR_CODE_PREFLIGHT_IDENTITY_MISMATCH)
        self.assertNotIn(OTHER_CA_UUID, failure.message)
        self.assertEqual(raised.exception.observed_uuid, OTHER_CA_UUID)

    def test_unreachable_provider_text_does_not_cross_the_public_boundary(self):
        diagnostic = "500: hostname lookup 'sensitive.internal' failed"
        with patch(
            "core.services.cluster_retirement.client_for_endpoint",
            return_value=IdentityClient(error=diagnostic),
        ):
            with self.assertRaises(RetirementPreflightUnavailable) as raised:
                cluster_retirement_preflight(
                    self.cluster,
                    mode=ProxmoxCluster.RetirementMode.VERIFIED,
                    endpoint_id=self.endpoint.pk,
                )

        failure = public_failure(raised.exception, operation="cluster_retirement.preflight")
        self.assertEqual(failure.code, ERROR_CODE_PREFLIGHT_UNREACHABLE)
        self.assertNotIn("sensitive.internal", failure.message)

    def test_active_global_scan_blocks_both_modes_without_minting_confirmation(self):
        ScanRun.objects.create(status=ScanRun.Status.RUNNING)

        for mode in (
            ProxmoxCluster.RetirementMode.VERIFIED,
            ProxmoxCluster.RetirementMode.FORCED,
        ):
            with self.subTest(mode=mode):
                if mode == ProxmoxCluster.RetirementMode.VERIFIED:
                    result, _client = self._verified()
                else:
                    result = cluster_retirement_preflight(self.cluster, mode=mode)
                self.assertEqual(result.blocker_codes, ("active_scan",))
                self.assertFalse(result.gate_clear)
                self.assertEqual(result.confirmation, "")

    def test_invalid_or_cross_cluster_confirmation_is_rejected(self):
        result, _client = self._verified()
        with self.assertRaises(RetirementPreflightInvalid):
            validate_retirement_preflight(
                result.confirmation + "tampered",
                cluster=self.cluster,
                mode=ProxmoxCluster.RetirementMode.VERIFIED,
            )

        other = ProxmoxCluster.objects.create(key="other", display_name="Other", enabled=False)
        with self.assertRaises(RetirementPreflightChanged):
            validate_retirement_preflight(
                result.confirmation,
                cluster=other,
                mode=ProxmoxCluster.RetirementMode.VERIFIED,
            )

    def test_lifecycle_credential_trust_endpoint_and_storage_changes_are_stale(self):
        cases = ("lifecycle", "credential", "trust", "endpoint", "storage")
        for case in cases:
            with self.subTest(case=case):
                result, _client = self._verified()
                if case == "lifecycle":
                    self.cluster.lifecycle_generation += 1
                    self.cluster.save(update_fields=["lifecycle_generation", "updated_at"])
                elif case == "credential":
                    self.credential.token_id = "rotated@pve!helper"
                    self.credential.save(update_fields=["token_id", "updated_at"])
                elif case == "trust":
                    self.trust.details = {"revision": 2}
                    self.trust.save(update_fields=["details", "updated_at"])
                elif case == "endpoint":
                    self.endpoint.enabled = False
                    self.endpoint.save(update_fields=["enabled", "updated_at"])
                else:
                    StorageCatalogState.objects.create(cluster=self.cluster, metadata_complete=True)

                with self.assertRaises(RetirementPreflightChanged):
                    validate_retirement_preflight(
                        result.confirmation,
                        cluster=self.cluster,
                        mode=ProxmoxCluster.RetirementMode.VERIFIED,
                    )

                if case == "lifecycle":
                    self.cluster.lifecycle_generation -= 1
                    self.cluster.save(update_fields=["lifecycle_generation", "updated_at"])
                elif case == "credential":
                    self.credential.token_id = "retire@pve!helper"
                    self.credential.save(update_fields=["token_id", "updated_at"])
                elif case == "trust":
                    self.trust.details = {}
                    self.trust.save(update_fields=["details", "updated_at"])
                elif case == "endpoint":
                    self.endpoint.enabled = True
                    self.endpoint.save(update_fields=["enabled", "updated_at"])
                else:
                    StorageCatalogState.objects.filter(cluster=self.cluster).delete()

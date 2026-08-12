from __future__ import annotations

import base64
import uuid
from unittest.mock import MagicMock, patch

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from core.models import (
    AuditEvent,
    ClusterMembershipState,
    ClusterNodeState,
    ClusterProjectionCoverage,
    ClusterStorage,
    ClusterStorageMount,
    ClusterTopologyHandoffStorageBinding,
    ProxmoxCluster,
    ProxmoxEndpoint,
    StorageCatalogState,
    StorageMount,
)
from core.services.cluster_credentials import set_cluster_credential
from core.services.cluster_identity import ObservedClusterIdentity
from core.services.cluster_onboarding import ClusterCandidate, VerifiedConnection
from core.services.cluster_retirement import RetirementPreflightInvalid, cluster_handoff_retirement_preflight
from core.services.cluster_topology_handoff import (
    ClusterTopologyHandoffError,
    apply_topology_handoff_storage_bindings,
    complete_topology_handoff,
    confirm_membership_recovery,
    inspect_membership_recovery,
    repair_unreadable_pending_transition,
    topology_handoff_snapshot,
)
from core.services.cluster_topology_role import TopologyRole
from core.services.cluster_trust import TRUST_PUBLIC, InspectedCertificate, approve_cluster_transport

TEST_KEY = base64.b64encode(b"h" * 32).decode()


@override_settings(
    PVE_HELPER_ENCRYPTION_KEYS=f"test:{TEST_KEY}",
    PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID="test",
)
class TopologyHandoffTests(TestCase):
    def setUp(self):
        self.old = ProxmoxCluster.objects.create(
            key="old-standalone",
            display_name="Old standalone",
            enabled=True,
            discovered_ca_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            discovered_ca_fingerprint="AA:AA",
        )
        self.endpoint = ProxmoxEndpoint.objects.create(
            cluster=self.old,
            name="pve1",
            url="https://pve1.example.test:8006",
        )
        approve_cluster_transport(self.old, mode=TRUST_PUBLIC)
        set_cluster_credential(self.old, token_id="helper@pve!helper", token_secret="secret")
        ClusterMembershipState.objects.create(
            cluster=self.old,
            topology_role=TopologyRole.STANDALONE,
            transition_pending=True,
            pending_topology_role=TopologyRole.COROSYNC,
            membership_generation=4,
        )
        ClusterNodeState.objects.create(cluster=self.old, node_name="pve1", present=True, membership_generation=4)
        self.mount = StorageMount.objects.create(
            storage_id="nas",
            display_name="NAS mount",
            path="/mnt/nas",
        )
        definition = ClusterStorage.objects.create(
            cluster=self.old,
            storage_id="nas",
            storage_type="nfs",
            shared=True,
        )
        self.binding = ClusterStorageMount.objects.create(
            cluster_storage=definition,
            mount=self.mount,
            scope=ClusterStorageMount.Scope.SHARED,
            node=None,
        )
        self.candidate = ClusterCandidate(
            key="new-corosync",
            display_name="New corosync",
            endpoint_url=self.endpoint.url,
            endpoint_name=self.endpoint.name,
            trust_mode=TRUST_PUBLIC,
            token_id="helper@pve!helper",
            token_secret="new-secret",
        )
        self.verified = VerifiedConnection(
            certificate=InspectedCertificate(
                subject="CN=pve1.example.test",
                issuer="CN=Example CA",
                sha256_fingerprint="certificate",
            ),
            identity=ObservedClusterIdentity(
                ca_uuid=self.old.discovered_ca_uuid,
                ca_fingerprint=self.old.discovered_ca_fingerprint,
            ),
            node_names=("pve1", "pve2"),
            version="9.2.4",
            discovered_name="Joined cluster",
            administrator_privileges=("Sys.Audit",),
            topology_role=TopologyRole.COROSYNC,
            membership_complete=True,
        )

    def _preflight(self):
        with patch("core.services.cluster_retirement._verify_selected_endpoint"):
            result = cluster_handoff_retirement_preflight(
                self.old,
                endpoint_id=self.endpoint.pk,
                replacement_ca_uuid=self.verified.identity.ca_uuid,
            )
        self.assertTrue(result.gate_clear)
        return result.confirmation

    def test_two_identity_handoff_retires_old_preserves_history_and_starts_new_unknown(self):
        history = AuditEvent.objects.create(
            action="cluster.display_name_changed",
            object_type="cluster",
            object_id=self.old.key,
            cluster=self.old,
            cluster_key_snapshot=self.old.key,
        )
        snapshot = topology_handoff_snapshot(self.old)

        replacement = complete_topology_handoff(
            old_cluster=self.old,
            candidate=self.candidate,
            verified=self.verified,
            expected_snapshot_digest=snapshot.digest,
            selected_storage_binding_ids=(self.binding.pk,),
            retirement_confirmation=self._preflight(),
            actor=None,
        )

        self.old.refresh_from_db()
        history.refresh_from_db()
        self.assertTrue(self.old.is_retired)
        self.assertEqual(self.old.key, "old-standalone")
        self.assertEqual(history.cluster, self.old)
        self.assertEqual(history.cluster_key_snapshot, "old-standalone")
        self.assertTrue(replacement.enabled)
        self.assertEqual(replacement.key, "new-corosync")
        self.assertEqual(replacement.endpoints.get().normalized_url, "https://pve1.example.test:8006")
        self.assertFalse(ClusterMembershipState.objects.filter(cluster=replacement).exists())
        self.assertFalse(ClusterNodeState.objects.filter(cluster=replacement).exists())
        intent = ClusterTopologyHandoffStorageBinding.objects.get(cluster=replacement)
        self.assertEqual(intent.source_cluster_key_snapshot, self.old.key)
        self.assertEqual(intent.status, intent.Status.PENDING)
        self.assertFalse(ClusterStorageMount.objects.filter(cluster_storage__cluster=replacement).exists())
        event = AuditEvent.objects.get(action="cluster.topology_handoff_completed")
        self.assertEqual(event.details["source_cluster_key"], self.old.key)

    def test_stale_snapshot_and_wrong_direction_roll_back_both_identities(self):
        snapshot = topology_handoff_snapshot(self.old)
        cases = (
            ("stale", self.verified, "bad-digest"),
            (
                "wrong direction",
                VerifiedConnection(**{**self.verified.__dict__, "topology_role": TopologyRole.STANDALONE}),
                snapshot.digest,
            ),
        )
        for label, verified, digest in cases:
            with self.subTest(label=label), self.assertRaises(ClusterTopologyHandoffError):
                complete_topology_handoff(
                    old_cluster=self.old,
                    candidate=self.candidate,
                    verified=verified,
                    expected_snapshot_digest=digest,
                    selected_storage_binding_ids=(),
                    retirement_confirmation=self._preflight(),
                    actor=None,
                )
            self.old.refresh_from_db()
            self.assertFalse(self.old.is_retired)
            self.assertTrue(self.old.enabled)
            self.assertFalse(ProxmoxCluster.objects.filter(key=self.candidate.key).exists())

    def test_failure_after_retirement_rolls_back_both_identities(self):
        snapshot = topology_handoff_snapshot(self.old)
        with (
            patch(
                "core.services.cluster_topology_handoff.persist_verified_cluster_configuration",
                side_effect=RuntimeError("injected after retirement"),
            ),
            self.assertRaisesRegex(RuntimeError, "injected after retirement"),
        ):
            complete_topology_handoff(
                old_cluster=self.old,
                candidate=self.candidate,
                verified=self.verified,
                expected_snapshot_digest=snapshot.digest,
                selected_storage_binding_ids=(),
                retirement_confirmation=self._preflight(),
                actor=None,
            )

        self.old.refresh_from_db()
        self.assertFalse(self.old.is_retired)
        self.assertTrue(self.old.enabled)
        self.assertTrue(ProxmoxEndpoint.objects.filter(pk=self.endpoint.pk).exists())
        self.assertFalse(ProxmoxCluster.objects.filter(key=self.candidate.key).exists())

    def test_configuration_change_after_review_makes_the_handoff_stale(self):
        snapshot = topology_handoff_snapshot(self.old)
        credential = self.old.credential
        credential.token_id = "rotated@pve!helper"
        credential.save(update_fields=["token_id", "updated_at"])

        with self.assertRaises(ClusterTopologyHandoffError):
            complete_topology_handoff(
                old_cluster=self.old,
                candidate=self.candidate,
                verified=self.verified,
                expected_snapshot_digest=snapshot.digest,
                selected_storage_binding_ids=(),
                retirement_confirmation=self._preflight(),
                actor=None,
            )

        self.old.refresh_from_db()
        self.assertFalse(self.old.is_retired)
        self.assertTrue(self.old.enabled)
        self.assertFalse(ProxmoxCluster.objects.filter(key=self.candidate.key).exists())

    def test_disappeared_pending_state_after_preflight_rolls_back(self):
        snapshot = topology_handoff_snapshot(self.old)
        confirmation = self._preflight()
        ClusterMembershipState.objects.filter(cluster=self.old).update(
            transition_pending=False,
            pending_topology_role=TopologyRole.UNKNOWN,
        )

        with self.assertRaises(ClusterTopologyHandoffError):
            complete_topology_handoff(
                old_cluster=self.old,
                candidate=self.candidate,
                verified=self.verified,
                expected_snapshot_digest=snapshot.digest,
                selected_storage_binding_ids=(),
                retirement_confirmation=confirmation,
                actor=None,
            )

        self.old.refresh_from_db()
        self.assertFalse(self.old.is_retired)
        self.assertFalse(ProxmoxCluster.objects.filter(key=self.candidate.key).exists())

    def test_operation_started_after_review_rolls_back(self):
        snapshot = topology_handoff_snapshot(self.old)
        confirmation = self._preflight()
        AuditEvent.objects.create(
            action="cluster.host_projection.refresh",
            object_type="cluster",
            object_id=self.old.key,
            outcome="queued",
            cluster=self.old,
            cluster_key_snapshot=self.old.key,
        )

        with self.assertRaisesRegex(ClusterTopologyHandoffError, "Provider work became active"):
            complete_topology_handoff(
                old_cluster=self.old,
                candidate=self.candidate,
                verified=self.verified,
                expected_snapshot_digest=snapshot.digest,
                selected_storage_binding_ids=(),
                retirement_confirmation=confirmation,
                actor=None,
            )

        self.old.refresh_from_db()
        self.assertFalse(self.old.is_retired)
        self.assertFalse(ProxmoxCluster.objects.filter(key=self.candidate.key).exists())

    def test_unreviewed_storage_binding_id_rolls_back(self):
        snapshot = topology_handoff_snapshot(self.old)

        with self.assertRaisesRegex(ClusterTopologyHandoffError, "mapping list changed"):
            complete_topology_handoff(
                old_cluster=self.old,
                candidate=self.candidate,
                verified=self.verified,
                expected_snapshot_digest=snapshot.digest,
                selected_storage_binding_ids=(self.binding.pk + 1000,),
                retirement_confirmation=self._preflight(),
                actor=None,
            )

        self.old.refresh_from_db()
        self.assertFalse(self.old.is_retired)
        self.assertFalse(ProxmoxCluster.objects.filter(key=self.candidate.key).exists())

    def test_handoff_retirement_preflight_requires_the_pending_old_identity(self):
        ClusterMembershipState.objects.filter(cluster=self.old).update(
            transition_pending=False,
            pending_topology_role=TopologyRole.UNKNOWN,
        )

        with self.assertRaises(RetirementPreflightInvalid):
            self._preflight()

    def test_changed_ca_uses_verified_handoff_evidence_without_claiming_old_identity_matched(self):
        changed_identity = VerifiedConnection(
            **{
                **self.verified.__dict__,
                "identity": ObservedClusterIdentity(
                    ca_uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                    ca_fingerprint="BB:BB",
                ),
            }
        )
        snapshot = topology_handoff_snapshot(self.old)
        with patch("core.services.cluster_retirement._verify_selected_endpoint") as old_identity:
            preflight = cluster_handoff_retirement_preflight(
                self.old,
                endpoint_id=self.endpoint.pk,
                replacement_ca_uuid=changed_identity.identity.ca_uuid,
            )
        old_identity.assert_not_called()
        self.assertEqual(preflight.identity_verification, "superseded_by_verified_handoff")

        replacement = complete_topology_handoff(
            old_cluster=self.old,
            candidate=self.candidate,
            verified=changed_identity,
            expected_snapshot_digest=snapshot.digest,
            selected_storage_binding_ids=(),
            retirement_confirmation=preflight.confirmation,
            actor=None,
        )

        self.assertEqual(replacement.discovered_ca_uuid, changed_identity.identity.ca_uuid)
        retired = AuditEvent.objects.get(action="cluster.retired", cluster=self.old)
        self.assertEqual(retired.details["identity_verification"], "superseded_by_verified_handoff")
        self.assertEqual(retired.details["replacement_ca_uuid"], changed_identity.identity.ca_uuid)

    def test_delayed_storage_binding_applies_only_after_exact_replacement_definition(self):
        snapshot = topology_handoff_snapshot(self.old)
        replacement = complete_topology_handoff(
            old_cluster=self.old,
            candidate=self.candidate,
            verified=self.verified,
            expected_snapshot_digest=snapshot.digest,
            selected_storage_binding_ids=(self.binding.pk,),
            retirement_confirmation=self._preflight(),
            actor=None,
        )
        definition = ClusterStorage.objects.create(
            cluster=replacement,
            storage_id="nas",
            storage_type="nfs",
            shared=True,
        )
        generation = uuid.uuid4()
        definition.observed_metadata_generation = generation
        definition.save(update_fields=["observed_metadata_generation"])
        StorageCatalogState.objects.create(
            cluster=replacement,
            metadata_generation=generation,
            metadata_complete=True,
        )

        with transaction.atomic():
            apply_topology_handoff_storage_bindings(replacement, metadata_generation=generation)

        binding = ClusterStorageMount.objects.get(cluster_storage=definition)
        intent = ClusterTopologyHandoffStorageBinding.objects.get(cluster=replacement)
        self.assertEqual(binding.mount, self.mount)
        self.assertEqual(intent.status, intent.Status.APPLIED)
        self.assertIsNotNone(intent.applied_at)

    def test_delayed_storage_binding_refuses_scope_change_without_partial_apply(self):
        second_mount = StorageMount.objects.create(storage_id="local", display_name="Local", path="/mnt/local")
        second_definition = ClusterStorage.objects.create(
            cluster=self.old,
            storage_id="local",
            storage_type="dir",
            shared=False,
        )
        second_binding = ClusterStorageMount.objects.create(
            cluster_storage=second_definition,
            mount=second_mount,
            scope=ClusterStorageMount.Scope.NODE,
            node="pve1",
        )
        snapshot = topology_handoff_snapshot(self.old)
        replacement = complete_topology_handoff(
            old_cluster=self.old,
            candidate=self.candidate,
            verified=self.verified,
            expected_snapshot_digest=snapshot.digest,
            selected_storage_binding_ids=(self.binding.pk, second_binding.pk),
            retirement_confirmation=self._preflight(),
            actor=None,
        )
        generation = uuid.uuid4()
        ClusterStorage.objects.create(
            cluster=replacement,
            storage_id="nas",
            storage_type="nfs",
            shared=False,
            observed_metadata_generation=generation,
        )
        ClusterStorage.objects.create(
            cluster=replacement,
            storage_id="local",
            storage_type="dir",
            shared=False,
            nodes=["pve1"],
            observed_metadata_generation=generation,
        )
        StorageCatalogState.objects.create(
            cluster=replacement,
            metadata_generation=generation,
            metadata_complete=True,
        )

        with transaction.atomic():
            apply_topology_handoff_storage_bindings(replacement, metadata_generation=generation)

        self.assertFalse(ClusterStorageMount.objects.filter(cluster_storage__cluster=replacement).exists())
        self.assertEqual(
            set(
                ClusterTopologyHandoffStorageBinding.objects.filter(cluster=replacement).values_list(
                    "status", flat=True
                )
            ),
            {ClusterTopologyHandoffStorageBinding.Status.REFUSED},
        )

    def test_storage_binding_rejects_a_stale_metadata_generation_without_mutation(self):
        snapshot = topology_handoff_snapshot(self.old)
        replacement = complete_topology_handoff(
            old_cluster=self.old,
            candidate=self.candidate,
            verified=self.verified,
            expected_snapshot_digest=snapshot.digest,
            selected_storage_binding_ids=(self.binding.pk,),
            retirement_confirmation=self._preflight(),
            actor=None,
        )
        current_generation = uuid.uuid4()
        ClusterStorage.objects.create(
            cluster=replacement,
            storage_id="nas",
            storage_type="nfs",
            shared=True,
            observed_metadata_generation=current_generation,
        )
        StorageCatalogState.objects.create(
            cluster=replacement,
            metadata_generation=current_generation,
            metadata_complete=True,
        )

        with transaction.atomic(), self.assertRaises(ClusterTopologyHandoffError):
            apply_topology_handoff_storage_bindings(replacement, metadata_generation=uuid.uuid4())

        intent = ClusterTopologyHandoffStorageBinding.objects.get(cluster=replacement)
        self.assertEqual(intent.status, intent.Status.PENDING)
        self.assertFalse(ClusterStorageMount.objects.filter(cluster_storage__cluster=replacement).exists())

    def test_node_storage_binding_requires_exact_complete_present_node_state(self):
        replacement = ProxmoxCluster.objects.create(key="replacement", display_name="Replacement", enabled=True)
        definition = ClusterStorage.objects.create(
            cluster=replacement,
            storage_id="local",
            storage_type="dir",
            shared=False,
        )
        generation = uuid.uuid4()
        definition.observed_metadata_generation = generation
        definition.save(update_fields=["observed_metadata_generation"])
        StorageCatalogState.objects.create(
            cluster=replacement,
            metadata_generation=generation,
            metadata_complete=True,
        )
        intent = ClusterTopologyHandoffStorageBinding.objects.create(
            cluster=replacement,
            source_cluster_key_snapshot=self.old.key,
            storage_id="local",
            mount=self.mount,
            scope=ClusterStorageMount.Scope.NODE,
            node="pve1",
        )

        with transaction.atomic():
            apply_topology_handoff_storage_bindings(replacement, metadata_generation=generation)

        intent.refresh_from_db()
        self.assertEqual(intent.status, intent.Status.REFUSED)
        self.assertIn("no complete present metadata", intent.refusal_reason)
        self.assertFalse(ClusterStorageMount.objects.filter(cluster_storage=definition).exists())
        event = AuditEvent.objects.get(action="cluster.topology_handoff_storage_refused")
        self.assertEqual(event.details["error_reason"], intent.refusal_reason)

    def test_storage_binding_refuses_concurrent_exact_binding_without_overwrite(self):
        replacement = ProxmoxCluster.objects.create(key="replacement", display_name="Replacement", enabled=True)
        definition = ClusterStorage.objects.create(
            cluster=replacement,
            storage_id="nas",
            storage_type="nfs",
            shared=True,
        )
        generation = uuid.uuid4()
        definition.observed_metadata_generation = generation
        definition.save(update_fields=["observed_metadata_generation"])
        StorageCatalogState.objects.create(
            cluster=replacement,
            metadata_generation=generation,
            metadata_complete=True,
        )
        intent = ClusterTopologyHandoffStorageBinding.objects.create(
            cluster=replacement,
            source_cluster_key_snapshot=self.old.key,
            storage_id="nas",
            mount=self.mount,
            scope=ClusterStorageMount.Scope.SHARED,
        )
        concurrent_mount = StorageMount.objects.create(
            storage_id="other",
            display_name="Concurrent mount",
            path="/mnt/concurrent",
        )
        existing = ClusterStorageMount.objects.create(
            cluster_storage=definition,
            mount=concurrent_mount,
            scope=ClusterStorageMount.Scope.SHARED,
        )

        with transaction.atomic():
            apply_topology_handoff_storage_bindings(replacement, metadata_generation=generation)

        intent.refresh_from_db()
        existing.refresh_from_db()
        self.assertEqual(intent.status, intent.Status.REFUSED)
        self.assertIn("may have changed after hand-off", intent.refusal_reason)
        self.assertEqual(existing.mount, concurrent_mount)

    def test_unreadable_pending_repair_requires_exact_key_and_audits_refusal_and_success(self):
        state = ClusterMembershipState.objects.get(cluster=self.old)
        state.pending_topology_role = "corosync-v2"
        state.save(update_fields=["pending_topology_role"])

        with self.assertRaises(ClusterTopologyHandoffError):
            repair_unreadable_pending_transition(self.old, typed_cluster_key="wrong", actor=None)
        state.refresh_from_db()
        self.assertTrue(state.transition_pending)

        repair_unreadable_pending_transition(self.old, typed_cluster_key=self.old.key, actor=None)

        state.refresh_from_db()
        self.assertFalse(state.transition_pending)
        self.assertEqual(state.pending_topology_role, TopologyRole.UNKNOWN)
        self.assertEqual(
            set(
                AuditEvent.objects.filter(action="cluster.topology_pending_repaired").values_list("outcome", flat=True)
            ),
            {"refused", "success"},
        )

    def test_schema_rejects_incoherent_pending_state(self):
        state = ClusterMembershipState.objects.get(cluster=self.old)
        state.transition_pending = False
        with self.assertRaises(IntegrityError), transaction.atomic():
            state.save(update_fields=["transition_pending"])

    def test_schema_rejects_ambiguous_and_duplicate_storage_intents(self):
        replacement = ProxmoxCluster.objects.create(key="replacement", display_name="Replacement", enabled=False)
        ClusterTopologyHandoffStorageBinding.objects.create(
            cluster=replacement,
            source_cluster_key_snapshot=self.old.key,
            storage_id="nas",
            mount=self.mount,
            scope=ClusterStorageMount.Scope.SHARED,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            ClusterTopologyHandoffStorageBinding.objects.create(
                cluster=replacement,
                source_cluster_key_snapshot=self.old.key,
                storage_id="nas",
                mount=self.mount,
                scope=ClusterStorageMount.Scope.SHARED,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClusterTopologyHandoffStorageBinding.objects.create(
                cluster=replacement,
                source_cluster_key_snapshot=self.old.key,
                storage_id="local",
                mount=self.mount,
                scope=ClusterStorageMount.Scope.NODE,
                node=None,
            )

    def test_observer_recovery_requires_fresh_review_and_atomically_replaces_members(self):
        state = ClusterMembershipState.objects.get(cluster=self.old)
        state.transition_pending = False
        state.pending_topology_role = TopologyRole.UNKNOWN
        state.save(update_fields=["transition_pending", "pending_topology_role"])
        ClusterProjectionCoverage.objects.create(
            cluster=self.old,
            domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP,
            generation=4,
            complete=False,
            error_code="observer_not_a_member",
        )
        payload = [
            {"type": "cluster", "name": "Recovered", "nodes": 2, "quorate": 1},
            {"type": "node", "name": "pve7", "nodeid": 7, "online": 1, "local": 1},
            {"type": "node", "name": "pve8", "nodeid": 8, "online": 1, "local": 0},
        ]
        client = MagicMock()
        client.get.return_value = payload
        with (
            patch("core.services.cluster_topology_handoff.verify_registered_endpoint") as verify_endpoint,
            patch("core.services.cluster_topology_handoff.client_for_endpoint", return_value=client),
        ):
            candidate = inspect_membership_recovery(self.old, endpoint_id=self.endpoint.pk)
            confirm_membership_recovery(
                self.old,
                endpoint_id=self.endpoint.pk,
                expected_digest=candidate.digest,
                actor=None,
            )

        self.assertEqual(candidate.members, ("pve7", "pve8"))
        self.assertEqual(
            set(ClusterNodeState.objects.filter(cluster=self.old, present=True).values_list("node_name", flat=True)),
            {"pve7", "pve8"},
        )
        self.assertFalse(ClusterNodeState.objects.get(cluster=self.old, node_name="pve1").present)
        event = AuditEvent.objects.get(action="cluster.topology_membership_recovered")
        self.assertEqual(event.details["accepted_members"], ["pve7", "pve8"])
        self.assertEqual(client.get.call_count, 2, "confirmation must perform its own fresh status read")
        self.assertEqual(verify_endpoint.call_count, 2, "both review and confirmation must re-verify trust and CA")

    def test_observer_recovery_refuses_a_changed_candidate_set(self):
        state = ClusterMembershipState.objects.get(cluster=self.old)
        state.transition_pending = False
        state.pending_topology_role = TopologyRole.UNKNOWN
        state.save(update_fields=["transition_pending", "pending_topology_role"])
        ClusterProjectionCoverage.objects.create(
            cluster=self.old,
            domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP,
            complete=False,
            error_code="observer_not_a_member",
        )
        first = [
            {"type": "node", "name": "pve7", "nodeid": 7, "online": 1, "local": 1},
        ]
        changed = [
            {"type": "cluster", "name": "Changed", "nodes": 2, "quorate": 1},
            {"type": "node", "name": "pve7", "nodeid": 7, "online": 1, "local": 1},
            {"type": "node", "name": "pve8", "nodeid": 8, "online": 1, "local": 0},
        ]
        client = MagicMock()
        client.get.side_effect = [first, changed]
        with (
            patch("core.services.cluster_topology_handoff.verify_registered_endpoint") as verify_endpoint,
            patch("core.services.cluster_topology_handoff.client_for_endpoint", return_value=client),
        ):
            candidate = inspect_membership_recovery(self.old, endpoint_id=self.endpoint.pk)
            with self.assertRaises(ClusterTopologyHandoffError):
                confirm_membership_recovery(
                    self.old,
                    endpoint_id=self.endpoint.pk,
                    expected_digest=candidate.digest,
                    actor=None,
                )
        self.assertEqual(verify_endpoint.call_count, 2)
        self.assertEqual(
            set(ClusterNodeState.objects.filter(cluster=self.old, present=True).values_list("node_name", flat=True)),
            {"pve1"},
        )

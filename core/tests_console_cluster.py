from __future__ import annotations

import base64

from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import (
    AuditEvent,
    ClusterTransportTrust,
    ConsoleSession,
    ProxmoxCluster,
)
from core.services.cluster_credentials import set_cluster_credential

KEY = f"k1:{base64.b64encode(b'K' * 32).decode()}"


@override_settings(
    PVE_HELPER_ENCRYPTION_KEYS=KEY,
    PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID="k1",
    PVE_API_TOKEN_ID="legacy@pve!legacy",
    PVE_API_TOKEN_SECRET="legacy-secret",
    PVE_VERIFY_TLS=True,
    PVE_CA_BUNDLE="",
)
class ConsoleGatewayClusterTests(TestCase):
    """The gateway resolves each session's cluster credential and WSS trust at
    connect time, so two simultaneous consoles never share identity or trust."""

    def _session(self, cluster, *, ticket="PVEVNC:t"):
        return ConsoleSession.objects.create(
            token_hash=f"hash-{cluster.key}",
            cluster=cluster,
            target_type=ConsoleSession.TargetType.VM,
            target_vmid=500,
            target_node="pve1",
            proxmox_endpoint="https://pve1.example.net:8006",
            proxmox_node="pve1",
            proxmox_port="5900",
            proxmox_ticket=ticket,
            expires_at=timezone.now() + timezone.timedelta(seconds=30),
        )

    def setUp(self):
        super().setUp()
        self.cluster_a = ProxmoxCluster.objects.create(key="a", display_name="A", enabled=True)
        self.cluster_b = ProxmoxCluster.objects.create(key="b", display_name="B", enabled=False)
        set_cluster_credential(self.cluster_a, token_id="a@pve!t", token_secret="secret-a")
        set_cluster_credential(self.cluster_b, token_id="b@pve!t", token_secret="secret-b")

    def test_two_sessions_resolve_their_own_credentials(self):
        from console_app.main import _resolve_cluster_credential_header

        header_a = async_to_sync(_resolve_cluster_credential_header)(self._session(self.cluster_a))
        header_b = async_to_sync(_resolve_cluster_credential_header)(self._session(self.cluster_b))

        self.assertEqual(header_a["Authorization"], "PVEAPIToken=a@pve!t=secret-a")
        self.assertEqual(header_b["Authorization"], "PVEAPIToken=b@pve!t=secret-b")
        self.assertNotEqual(header_a["Authorization"], header_b["Authorization"])

    def test_no_api_token_is_stored_in_the_session_row(self):
        session = self._session(self.cluster_a)

        # The API token is resolved at connect, never persisted; only the short-lived
        # vnc ticket lives on the row, and that is cleared on consume.
        self.assertNotIn("secret-a", session.proxmox_ticket)
        self.assertNotIn("secret-a", str(session.details))
        self.assertNotIn("a@pve!t=secret-a", str(session.__dict__))

    def test_credential_is_resolved_fresh_so_rotation_takes_effect(self):
        from console_app.main import _resolve_cluster_credential_header

        session = self._session(self.cluster_a)
        set_cluster_credential(self.cluster_a, token_id="a@pve!t", token_secret="rotated-a")

        header = async_to_sync(_resolve_cluster_credential_header)(session)

        self.assertEqual(header["Authorization"], "PVEAPIToken=a@pve!t=rotated-a")

    def test_missing_credential_fails_closed(self):
        from console_app.main import _ConsoleAuthError, _resolve_cluster_credential_header

        # A cluster with no credential, after cutover, must not silently fall back to
        # a global token that could belong elsewhere.
        from core.models import ClusterCredential, RuntimeConfigurationState

        ClusterCredential.objects.filter(cluster=self.cluster_b).delete()
        RuntimeConfigurationState.objects.create(
            pk=RuntimeConfigurationState.SINGLETON_PK,
            bootstrap_completed=True,
            credential_cutover_completed_at=timezone.now(),
        )

        with self.assertRaises(_ConsoleAuthError):
            async_to_sync(_resolve_cluster_credential_header)(self._session(self.cluster_b))

    def test_ssl_context_uses_the_clusters_trust_profile(self):
        from console_app.main import _resolve_cluster_ssl_context

        ClusterTransportTrust.objects.create(cluster=self.cluster_a, mode=ClusterTransportTrust.Mode.PUBLIC)

        context = async_to_sync(_resolve_cluster_ssl_context)(
            self._session(self.cluster_a), "wss://pve1.example.net:8006/x"
        )

        # A public profile yields a verifying context; the point is it is built from
        # the cluster, not an ambient global bundle.
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode.name, "CERT_REQUIRED")

    def test_non_wss_needs_no_context(self):
        from console_app.main import _resolve_cluster_ssl_context

        context = async_to_sync(_resolve_cluster_ssl_context)(self._session(self.cluster_a), "ws://plain/x")

        self.assertIsNone(context)

    def test_legacy_session_without_cluster_fails_closed(self):
        from console_app.main import _ConsoleAuthError, _resolve_cluster_credential_header

        legacy = ConsoleSession.objects.create(
            token_hash="legacy",
            cluster=None,
            target_type=ConsoleSession.TargetType.VM,
            target_vmid=1,
            target_node="pve1",
            expires_at=timezone.now() + timezone.timedelta(seconds=30),
        )

        with self.assertRaises(_ConsoleAuthError):
            async_to_sync(_resolve_cluster_credential_header)(legacy)

    def test_console_audit_keeps_same_vmid_separate_per_cluster(self):
        from console_app.main import _audit_session

        session_a = self._session(self.cluster_a)
        session_b = self._session(self.cluster_b)
        ConsoleSession.objects.filter(pk=session_a.pk).update(source_ip="192.0.2.10")

        async_to_sync(_audit_session)(session_a.pk, "guest.console.closed", "success")
        async_to_sync(_audit_session)(session_b.pk, "guest.console.closed", "success")

        events = list(AuditEvent.objects.filter(action="guest.console.closed").order_by("cluster_key_snapshot"))

        self.assertEqual([event.cluster_key_snapshot for event in events], ["a", "b"])
        self.assertEqual([event.object_id for event in events], ["gr1:a:vm:500", "gr1:b:vm:500"])
        self.assertEqual(
            [event.details["guest_ref"] for event in events],
            ["gr1:a:vm:500@pve1", "gr1:b:vm:500@pve1"],
        )
        self.assertEqual(events[0].details["node_ref"], "nr1:a:pve1")
        self.assertEqual(events[0].source_ip, "192.0.2.10")

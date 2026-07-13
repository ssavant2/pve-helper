from __future__ import annotations

from unittest.mock import patch
from types import SimpleNamespace
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, DerivedTagStyle, ProxmoxInventory, ScanRun
from core.services.tag_actions import execute_tag_operation, prepare_tag_operation
from core.services.integration_tokens import issue_token
from core.services.recent_tasks import recent_task_page
from core.tasks import reap_stale_guest_tasks
from core.services.tags import (
    TagValidationError,
    derived_tag_for,
    inventory_rows,
    join_tags,
    parse_color_map,
    parse_registered_tags,
    parse_tag_style,
    parse_tags,
    serialize_color_map,
    serialize_tag_style,
    validate_tag,
)
from core.views.guests._core import _decorate_guest_tag_chips


class TagServiceTests(TestCase):
    def test_parse_join_lowercase_and_dedupe(self):
        self.assertEqual(parse_tags("Prod; web,PROD  db"), ["prod", "web", "db"])
        self.assertEqual(join_tags(["prod", "web", "prod"]), "prod;web")

    def test_reserved_namespace_is_rejected(self):
        with self.assertRaises(TagValidationError):
            validate_tag("pvehelper-vmtype-vm")

    def test_registry_and_color_round_trip_preserves_style_options(self):
        options = {"registered-tags": ["prod", "web"], "tag-style": "shape=full,color-map=prod:112233:ffffff;web:aabbcc"}
        parsed = parse_registered_tags(options)
        self.assertEqual(parsed["prod"].background, "112233")
        style = parse_tag_style(options["tag-style"])
        colors = parse_color_map(style["color-map"])
        self.assertEqual(parse_color_map(serialize_color_map(colors)), colors)
        self.assertEqual(parse_tag_style(serialize_tag_style(style)), style)

    def test_registry_accepts_live_pve_list_and_dict_shapes(self):
        parsed = parse_registered_tags(
            {"registered-tags": ["prod"], "tag-style": {"color-map": "prod:112233:ffffff"}}
        )
        self.assertEqual(parsed["prod"].foreground, "ffffff")

    def test_derived_precedence(self):
        self.assertEqual(derived_tag_for(object_type="vm", is_template=True, is_linked_clone=True), "pvehelper-vmtype-template")
        self.assertEqual(derived_tag_for(object_type="vm", is_linked_clone=True), "pvehelper-vmtype-linked-clone")
        self.assertEqual(derived_tag_for(object_type="ct"), "pvehelper-vmtype-ct")

    def test_inventory_includes_registered_unused_real_and_derived(self):
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        ProxmoxInventory.objects.create(
            scan_run=scan, node="pve1", object_type="vm", vmid=100, name="one",
            config={"tags": "prod"}, derived_type="pvehelper-vmtype-vm",
        )
        registered = parse_registered_tags({"registered-tags": "prod;unused"})
        rows = {row.name: row for row in inventory_rows(scan, registered)}
        self.assertEqual(rows["prod"].guest_count, 1)
        self.assertEqual(rows["unused"].guest_count, 0)
        self.assertTrue(rows["pvehelper-vmtype-vm"].derived)

    @patch("core.services.tag_actions.registered_tags", return_value=({}, ""))
    def test_guest_tag_choices_include_tags_from_the_full_latest_scan(self, _registered):
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        ProxmoxInventory.objects.create(
            scan_run=scan, node="pve1", object_type="vm", vmid=100, name="selected",
            config={"tags": "prod"}, derived_type="pvehelper-vmtype-vm",
        )
        ProxmoxInventory.objects.create(
            scan_run=scan, node="pve2", object_type="vm", vmid=200, name="other",
            config={"tags": "qa"}, derived_type="pvehelper-vmtype-vm",
        )
        selected_row = SimpleNamespace(tags=["prod"], derived_type="pvehelper-vmtype-vm")

        self.assertEqual(_decorate_guest_tag_chips([selected_row]), ["prod", "qa"])

    def test_reserved_real_tag_is_reported_separately_from_derived_membership(self):
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        ProxmoxInventory.objects.create(
            scan_run=scan, node="pve1", object_type="vm", vmid=100,
            config={"tags": "pvehelper-vmtype-ct"}, derived_type="pvehelper-vmtype-vm",
        )
        rows = {row.name: row for row in inventory_rows(scan, {})}
        conflict = rows["pvehelper-vmtype-ct"]
        self.assertTrue(conflict.namespace_conflict)
        self.assertEqual(conflict.guest_count, 0)
        self.assertEqual(len(conflict.conflicting_guests), 1)
        self.assertTrue(conflict.derived)


class TagViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("tag-admin")
        self.client.force_login(self.user)
        self.scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        ProxmoxInventory.objects.create(
            scan_run=self.scan, node="pve1", object_type="vm", vmid=100, name="vm-one",
            config={"tags": "prod"}, derived_type="pvehelper-vmtype-vm",
        )

    @patch("core.views.tags.registered_tags", return_value=({}, ""))
    def test_overview_and_detail_render(self, _registered):
        response = self.client.get(reverse("core:tags_overview"))
        self.assertContains(response, "pvehelper-vmtype-vm")
        response = self.client.get(reverse("core:tag_detail"), {"tag": "prod"})
        self.assertContains(response, "vm-one")

    @patch("core.views.tags.registered_tags", return_value=({}, ""))
    def test_overview_ignores_newer_completed_scans_without_guest_inventory(self, _registered):
        ScanRun.objects.create(status=ScanRun.Status.COMPLETED)

        response = self.client.get(reverse("core:tags_overview"))

        self.assertContains(response, "prod")
        prod = next(row for row in response.context["tag_rows"] if row.name == "prod")
        self.assertEqual(prod.guest_count, 1)

    @patch("core.views.tags.register_tag", return_value=({}, ""))
    def test_create_audits(self, _register):
        response = self.client.post(reverse("core:tag_create"), {"tag": "new-tag", "color": "#112233"})
        self.assertRedirects(response, reverse("core:tags_overview"), fetch_redirect_response=False)
        self.assertTrue(self.user.pve_helper_audit_events.filter(action="tag.registered").exists())

    @patch("core.views.guests.hardware._available_user_tags", return_value=["prod", "qa"])
    def test_guest_tag_options_uses_current_guest_config(self, _available):
        detail = SimpleNamespace(found=True, config={"tags": "prod"})
        with patch("core.views.guests.hardware._resolve_guest_detail", return_value=detail) as resolve:
            response = self.client.get(
                reverse("core:guest_tag_options", args=["vm", 100]),
                {"node": "pve1"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"available_tags": ["prod", "qa"], "assigned_tags": ["prod"]})
        resolve.assert_called_once_with("vm", 100, node="pve1")

    @patch("core.views.tags.register_tag", return_value=({}, ""))
    def test_create_fetch_has_no_message_and_appears_in_recent_tasks(self, _register):
        response = self.client.post(
            reverse("core:tag_create"),
            {"tag": "quiet-tag", "color": "#112233"},
            HTTP_ACCEPT="application/json",
            HTTP_X_REQUESTED_WITH="fetch",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertNotContains(response, "Registered tag")
        task = next(item for item in recent_task_page(limit=20).tasks if item["action"] == "tag.registered")
        self.assertEqual(task["name"], "Create tag")
        self.assertEqual(task["target"], "quiet-tag")
        self.assertIsNone(task["target_guest"])
        audit = self.client.get(reverse("core:audit_log"))
        self.assertContains(audit, "Create tag")
        self.assertNotContains(audit, "tag.registered")

    @patch("core.views.tags.registered_tags", return_value=({}, ""))
    def test_detail_offers_assignment_for_an_unassigned_guest(self, _registered):
        other = ProxmoxInventory.objects.create(
            scan_run=self.scan, node="pve1", object_type="ct", vmid=101, name="ct-two",
            config={}, derived_type="pvehelper-vmtype-ct",
        )
        response = self.client.get(reverse("core:tag_detail"), {"tag": "prod"})
        self.assertContains(response, "Assign objects")
        self.assertContains(response, other.name)

    @patch("core.views.tags.registered_tags", return_value=({}, ""))
    def test_detail_offers_per_guest_tag_removal_for_user_tags(self, _registered):
        response = self.client.get(reverse("core:tag_detail"), {"tag": "prod"})

        self.assertContains(response, 'data-tag-unassign-form')
        self.assertContains(response, 'name="tags_mode" value="remove"')
        self.assertContains(response, 'name="guest" value="vm:100@pve1"')

    def test_derived_tag_color_is_stored_app_side(self):
        response = self.client.post(
            reverse("core:tag_recolor"),
            {"tag": "pvehelper-vmtype-vm", "color": "#123456"},
        )
        self.assertEqual(response.status_code, 302)
        style = DerivedTagStyle.objects.get(tag="pvehelper-vmtype-vm")
        self.assertEqual(style.background, "123456")
        self.assertTrue(self.user.pve_helper_audit_events.filter(action="tag.recolored").exists())


class TagFanoutTests(TestCase):
    def setUp(self):
        self.scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        self.row = ProxmoxInventory.objects.create(
            scan_run=self.scan, node="old-node", object_type="vm", vmid=100, name="vm-one",
            config={"tags": "old;keep"}, derived_type="pvehelper-vmtype-vm",
        )
        self.event = AuditEvent.objects.create(
            username="admin", action="tag.bulk_operation", object_type="tag", object_id="old", outcome="queued"
        )

    @patch("core.services.tag_actions.fetch_live_guest_inventory", return_value=[])
    @patch("core.services.tag_actions.registered_tags", return_value=({}, ""))
    def test_prepare_persists_complete_target_payload(self, _registered, _live):
        self.assertEqual(prepare_tag_operation(self.event, operation="delete", source_tag="old"), "")
        self.event.refresh_from_db()
        self.assertEqual(self.event.details["targets"][0]["vmid"], 100)
        self.assertEqual(self.event.details["succeeded"], [])

    @patch("core.services.tag_actions.unregister_tag", return_value=({}, ""))
    @patch("core.services.tag_actions.fetch_live_guest_inventory")
    @patch("core.services.tag_actions.configured_clients")
    def test_execute_rediscovery_digest_and_cache_update(self, clients, live, _unregister):
        class FakeClient:
            def guest_config(self, **_kwargs):
                return {"tags": "old;keep", "digest": "digest-1"}

            def set_guest_config(self, **kwargs):
                self.written = kwargs

        client = FakeClient()
        clients.return_value = [client]
        live.return_value = [SimpleNamespace(object_type="vm", vmid=100, node="new-node", name="vm-one")]
        self.event.details = {
            "operation": "delete", "source_tag": "old", "new_tag": "",
            "targets": [{"node": "old-node", "object_type": "vm", "vmid": 100, "name": "vm-one"}],
            "succeeded": [], "skipped": [], "failed": [], "username": "admin",
        }
        self.event.save(update_fields=["details"])
        execute_tag_operation(self.event.id)
        self.assertEqual(client.written["node"], "new-node")
        self.assertEqual(client.written["digest"], "digest-1")
        self.assertEqual(client.written["updates"], {"tags": "keep"})
        self.row.refresh_from_db()
        self.assertEqual(self.row.node, "new-node")
        self.assertEqual(self.row.config["tags"], "keep")
        self.assertTrue(AuditEvent.objects.filter(action="tag.deleted", object_id="old").exists())

    def test_stale_running_operation_is_marked_retryable(self):
        self.event.outcome = "running"
        self.event.details = {
            "operation": "delete", "source_tag": "old", "targets": [{"vmid": 100}],
            "heartbeat_at": (timezone.now() - timedelta(minutes=20)).isoformat(),
        }
        self.event.save(update_fields=["outcome", "details"])
        result = reap_stale_guest_tasks()
        self.event.refresh_from_db()
        self.assertEqual(result["interrupted_tag_operations"], 1)
        self.assertEqual(self.event.outcome, "failed")
        self.assertTrue(self.event.details["retryable"])


@override_settings(BACKUP_INTEGRATION_API_ENABLED=True)
class TagApiTests(TestCase):
    def setUp(self):
        _token, self.raw = issue_token("veeam")
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        ProxmoxInventory.objects.create(
            scan_run=scan, node="pve1", object_type="vm", vmid=100, name="template-one",
            status="stopped", config={"tags": "backup-gold"}, derived_type="pvehelper-vmtype-template",
        )

    def auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.raw}"}

    @patch("core.views.tags_api.registered_tags", return_value=({}, ""))
    def test_backup_policy_does_not_filter_template(self, _registered):
        response = self.client.get(reverse("core:api_backup_groups"), secure=True, **self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["groups"]["backup-gold"][0]["type"], "template")

    @patch("core.views.tags_api.registered_tags", return_value=({}, ""))
    def test_virtual_tag_endpoint_and_real_tag_array(self, _registered):
        response = self.client.get(
            reverse("core:api_tag_guests", args=["pvehelper-vmtype-template"]), secure=True, **self.auth()
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["kind"], "derived")
        self.assertEqual(response.json()["guests"][0]["tags"], ["backup-gold"])

    def test_missing_token_is_rejected(self):
        self.assertEqual(self.client.get(reverse("core:api_tags"), secure=True).status_code, 401)

    def test_api_is_get_only_and_session_is_not_authentication(self):
        self.client.force_login(get_user_model().objects.create_user("browser-admin"))
        self.assertEqual(self.client.get(reverse("core:api_tags"), secure=True).status_code, 401)
        self.assertEqual(
            self.client.post(reverse("core:api_tags"), secure=True, **self.auth()).status_code,
            405,
        )

    @override_settings(BACKUP_INTEGRATION_API_ENABLED=False)
    def test_disabled_api_is_404(self):
        self.assertEqual(self.client.get(reverse("core:api_tags"), secure=True, **self.auth()).status_code, 404)

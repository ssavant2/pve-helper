from __future__ import annotations

from unittest.mock import patch
from types import SimpleNamespace
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, ProxmoxInventory, ScanRun
from core.services.tag_actions import execute_tag_operation, prepare_tag_operation
from core.services.integration_tokens import issue_token
from core.services.proxmox import VerifiedGuestInventory, fetch_verified_guest_inventory
from core.services.recent_tasks import recent_task_page
from core.tasks import reap_stale_guest_tasks
from core.services.tags import (
    RegisteredTag,
    inventory_rows,
    join_tags,
    parse_color_map,
    parse_registered_tags,
    parse_tag_style,
    parse_tags,
    serialize_color_map,
    serialize_tag_style,
)
from core.views.guests._core import _decorate_guest_tag_chips, _guest_tab_context
from core.views.tags import _tag_type_label


class TagServiceTests(TestCase):
    def test_parse_join_lowercase_and_dedupe(self):
        self.assertEqual(parse_tags("Prod; web,PROD  db"), ["prod", "web", "db"])
        self.assertEqual(join_tags(["prod", "web", "prod"]), "prod;web")

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

    def test_inventory_includes_registered_unused_and_assigned_tags(self):
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        ProxmoxInventory.objects.create(
            scan_run=scan, node="pve1", object_type="vm", vmid=100, name="one",
            config={"tags": "prod"},
        )
        registered = parse_registered_tags({"registered-tags": "prod;unused"})
        rows = {row.name: row for row in inventory_rows(scan, registered)}
        self.assertEqual(rows["prod"].guest_count, 1)
        self.assertEqual(rows["unused"].guest_count, 0)

    @patch("core.services.tag_actions.registered_tags", return_value=({}, ""))
    def test_guest_tag_choices_include_tags_from_the_full_latest_scan(self, _registered):
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        ProxmoxInventory.objects.create(
            scan_run=scan, node="pve1", object_type="vm", vmid=100, name="selected",
            config={"tags": "prod"},
        )
        ProxmoxInventory.objects.create(
            scan_run=scan, node="pve2", object_type="vm", vmid=200, name="other",
            config={"tags": "qa"},
        )
        selected_row = SimpleNamespace(tags=["prod"])

        self.assertEqual(_decorate_guest_tag_chips([selected_row]), ["prod", "qa"])

    @patch("core.services.proxmox.configured_clients")
    def test_verified_inventory_reports_partial_coverage_without_discarding_guests(self, clients):
        class FakeClient:
            def __init__(self, endpoint, response=None, error=None):
                self.endpoint = endpoint
                self.response = response
                self.error = error

            def get(self, path):
                self.test_path = path
                if self.error:
                    raise self.error
                return self.response

        available = FakeClient(
            "https://pve1:8006",
            [{"node": "pve1", "type": "qemu", "vmid": 100, "name": "one", "tags": "prod"}],
        )
        unavailable = FakeClient("https://pve2:8006", error=RuntimeError("unavailable"))
        clients.return_value = [available, unavailable]

        result = fetch_verified_guest_inventory()

        self.assertFalse(result.complete)
        self.assertEqual(result.guests[0].tags, ("prod",))
        self.assertEqual(result.successful_endpoints, ("https://pve1:8006",))
        self.assertIn("https://pve2:8006", result.errors[0])
        self.assertEqual(available.test_path, "cluster/resources?type=vm")


class TagViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("tag-admin")
        self.client.force_login(self.user)
        self.scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        ProxmoxInventory.objects.create(
            scan_run=self.scan, node="pve1", object_type="vm", vmid=100, name="vm-one",
            config={"tags": "prod"},
        )

    def test_tag_detail_type_labels_are_plain_object_types(self):
        vm = SimpleNamespace(object_type="vm", vmid=100, config={})
        template = SimpleNamespace(object_type="vm", vmid=101, config={"template": "1"})
        clone = SimpleNamespace(object_type="vm", vmid=102, config={})
        ct = SimpleNamespace(object_type="ct", vmid=103, config={})

        self.assertEqual(_tag_type_label(vm, {102}), "vm")
        self.assertEqual(_tag_type_label(template, {102}), "template")
        self.assertEqual(_tag_type_label(clone, {102}), "linked clone")
        self.assertEqual(_tag_type_label(ct, {102}), "ct")

    @patch("core.views.tags.registered_tags", return_value=({}, ""))
    def test_overview_and_detail_render(self, _registered):
        response = self.client.get(reverse("core:tags_overview"))
        self.assertContains(response, "prod")
        response = self.client.get(reverse("core:tag_detail"), {"tag": "prod"})
        self.assertContains(response, "vm-one")

    @patch("core.views.tags.registered_tags", return_value=({}, ""))
    def test_overview_ignores_newer_completed_scans_without_guest_inventory(self, _registered):
        ScanRun.objects.create(status=ScanRun.Status.COMPLETED)

        response = self.client.get(reverse("core:tags_overview"))

        self.assertContains(response, "prod")
        prod = next(row for row in response.context["tag_rows"] if row.name == "prod")
        self.assertEqual(prod.guest_count, 1)

    @patch("core.views.guests._core._apply_workspace_lineage", return_value=[])
    @patch("core.views.guests._core._guest_rows", return_value=([], False, None))
    @patch(
        "core.services.tag_actions.registered_tags",
        return_value=({"unused": RegisteredTag("unused")}, ""),
    )
    def test_guest_tab_context_includes_registered_unused_tags(self, _registered, _guest_rows, _lineage):
        detail = SimpleNamespace(
            object_type="vm", vmid=100, node="pve1", name="vm-one", status="stopped", config={"tags": "prod"}
        )

        context = _guest_tab_context(detail, "summary")

        self.assertEqual(context["available_user_tags"], ["prod", "unused"])

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
            config={},
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

class TagFanoutTests(TestCase):
    def setUp(self):
        self.scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        self.row = ProxmoxInventory.objects.create(
            scan_run=self.scan, node="old-node", object_type="vm", vmid=100, name="vm-one",
            config={"tags": "old;keep"},
        )
        self.event = AuditEvent.objects.create(
            username="admin", action="tag.bulk_operation", object_type="tag", object_id="old", outcome="queued"
        )

    @patch("core.services.tag_actions.fetch_verified_guest_inventory")
    @patch("core.services.tag_actions.registered_tags", return_value=({}, ""))
    def test_prepare_persists_snapshot_targets_when_live_coverage_is_incomplete(self, _registered, live):
        live.return_value = self.inventory(complete=False, errors=("pve2 unavailable",))
        self.assertEqual(prepare_tag_operation(self.event, operation="delete", source_tag="old"), "")
        self.event.refresh_from_db()
        self.assertEqual(self.event.details["targets"][0]["vmid"], 100)
        self.assertEqual(self.event.details["succeeded"], [])
        self.assertFalse(self.event.details["membership_complete"])

    @patch("core.services.tag_actions.fetch_verified_guest_inventory")
    @patch("core.services.tag_actions.registered_tags", return_value=({}, ""))
    def test_prepare_unions_snapshot_and_partial_live_targets(self, _registered, live):
        live.return_value = self.inventory(
            SimpleNamespace(node="pve2", object_type="ct", vmid=200, name="ct-two", tags=("old",)),
            complete=False,
            errors=("pve3 unavailable",),
        )

        self.assertEqual(prepare_tag_operation(self.event, operation="delete", source_tag="old"), "")

        self.event.refresh_from_db()
        self.assertEqual(
            {(target["object_type"], target["vmid"]) for target in self.event.details["targets"]},
            {("vm", 100), ("ct", 200)},
        )

    @patch("core.services.tag_actions.unregister_tag")
    @patch("core.services.tag_actions.fetch_verified_guest_inventory")
    @patch("core.services.tag_actions.registered_tags", return_value=({}, ""))
    def test_prepare_does_not_unregister_when_empty_membership_is_incomplete(
        self, _registered, live, unregister
    ):
        self.row.delete()
        live.return_value = self.inventory(complete=False, errors=("pve2 unavailable",))

        error = prepare_tag_operation(self.event, operation="delete", source_tag="old")

        self.assertIn("Could not verify", error)
        unregister.assert_not_called()

    @patch("core.services.tag_actions.unregister_tag", return_value=({}, ""))
    @patch("core.services.tag_actions.fetch_verified_guest_inventory")
    @patch("core.services.tag_actions.configured_clients")
    def test_execute_rediscovery_digest_and_cache_update(self, clients, live, _unregister):
        class FakeClient:
            def guest_config(self, **_kwargs):
                return {"tags": "old;keep", "digest": "digest-1"}

            def set_guest_config(self, **kwargs):
                self.written = kwargs

        client = FakeClient()
        clients.return_value = [client]
        live.side_effect = [
            self.inventory(
                SimpleNamespace(
                    object_type="vm", vmid=100, node="new-node", name="vm-one", tags=("old", "keep")
                )
            ),
            self.inventory(
                SimpleNamespace(object_type="vm", vmid=100, node="new-node", name="vm-one", tags=("keep",))
            ),
        ]
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

    @patch("core.services.tag_actions.unregister_tag")
    @patch("core.services.tag_actions.fetch_verified_guest_inventory")
    @patch("core.services.tag_actions.configured_clients", return_value=[])
    def test_execute_keeps_source_registered_when_final_verification_is_incomplete(
        self, _clients, live, unregister
    ):
        guest = SimpleNamespace(object_type="vm", vmid=100, node="old-node", name="vm-one", tags=("old",))
        live.side_effect = [
            self.inventory(guest),
            self.inventory(complete=False, errors=("pve2 unavailable",)),
        ]
        self.event.details = {
            "operation": "delete", "source_tag": "old", "new_tag": "",
            "targets": [], "succeeded": [], "skipped": [], "failed": [], "username": "admin",
        }
        self.event.save(update_fields=["details"])

        execute_tag_operation(self.event.id)

        self.event.refresh_from_db()
        self.assertEqual(self.event.outcome, "failed")
        self.assertFalse(self.event.details["postcondition_complete"])
        unregister.assert_not_called()

    @patch("core.services.tag_actions.unregister_tag")
    @patch("core.services.tag_actions.fetch_verified_guest_inventory")
    @patch("core.services.tag_actions.configured_clients", return_value=[])
    def test_execute_keeps_source_registered_when_a_guest_still_has_it(self, _clients, live, unregister):
        guest = SimpleNamespace(object_type="vm", vmid=100, node="old-node", name="vm-one", tags=("old",))
        live.side_effect = [self.inventory(guest), self.inventory(guest)]
        self.event.details = {
            "operation": "delete", "source_tag": "old", "new_tag": "",
            "targets": [], "succeeded": [], "skipped": [], "failed": [], "username": "admin",
        }
        self.event.save(update_fields=["details"])

        execute_tag_operation(self.event.id)

        self.event.refresh_from_db()
        self.assertEqual(self.event.outcome, "failed")
        self.assertEqual(self.event.details["remaining_targets"][0]["vmid"], 100)
        unregister.assert_not_called()

    @staticmethod
    def inventory(*guests, complete=True, errors=()):
        attempted = ("https://pve1:8006", "https://pve2:8006")
        successful = attempted if complete else attempted[:1]
        return VerifiedGuestInventory(
            guests=tuple(guests),
            attempted_endpoints=attempted,
            successful_endpoints=successful,
            errors=tuple(errors),
        )

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
            status="stopped", config={"tags": "backup-gold", "template": "1"},
        )

    def auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.raw}"}

    @patch("core.views.tags_api.registered_tags", return_value=({}, ""))
    def test_backup_policy_does_not_filter_template(self, _registered):
        response = self.client.get(reverse("core:api_backup_groups"), secure=True, **self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["groups"]["backup-gold"][0]["type"], "template")

    @patch("core.views.tags_api.registered_tags", return_value=({}, ""))
    def test_real_tag_endpoint_and_real_tag_array(self, _registered):
        response = self.client.get(
            reverse("core:api_tag_guests", args=["backup-gold"]), secure=True, **self.auth()
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("kind", response.json())
        self.assertEqual(response.json()["guests"][0]["tags"], ["backup-gold"])
        self.assertEqual(response.json()["guests"][0]["type"], "template")

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

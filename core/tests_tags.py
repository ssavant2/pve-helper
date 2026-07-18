from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock, patch
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_q.models import OrmQ
from django_q.signing import SignedPackage

from core.models import (
    AuditEvent,
    CurrentGuestInventory,
    CurrentGuestInventoryState,
    ProxmoxCluster,
    ProxmoxEndpoint,
    ProxmoxInventory,
    ScanRun,
)
from core.services.tag_actions import (
    TagOperationRetryError,
    execute_tag_operation,
    latest_tag_targets,
    prepare_tag_operation,
    recolor_tag,
    register_tag,
    retry_tag_operation,
    unregister_tag,
)
from core.services.tag_catalog import load_tag_catalog
from core.services.tag_inventory_refresh import (
    TagInventoryRefreshAlreadyActive,
    execute_tag_inventory_refresh,
    queue_tag_inventory_refresh,
)
from core.services.tag_operation_confirmation import (
    CHANGED_CONFIRMATION_ERROR,
    INVALID_CONFIRMATION_ERROR,
    issue_tag_operation_confirmation,
    tag_membership_fingerprint,
    validate_tag_operation_confirmation,
)
from core.services.tag_registry import (
    TAG_REGISTRY_CONFLICT_ERROR,
    refresh_registered_tags,
    tag_registry_cache_key,
)
from core.services.proxmox import ProxmoxAPIError, VerifiedGuestInventory, fetch_verified_guest_inventory
from core.services.recent_tasks import recent_task_page
from core.services.task_queues import BULK_QUEUE_NAME
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
from core.views.guests.read_model_support import _decorate_guest_tag_chips, _guest_tab_context
from core.views.tags import _tag_type_label


class FakeProviderClient:
    """A provider double for one endpoint of the selected cluster."""

    def __init__(self, endpoint, response=None, error=None):
        self.endpoint = endpoint
        self.response = response
        self.error = error

    def get(self, path):
        self.test_path = path
        if self.error:
            raise self.error
        return self.response


class ClusterFixtureMixin:
    """Give provider reads a real cluster to resolve.

    fetch_verified_guest_inventory() selects endpoints from the cluster rather than
    from settings, so tests inject their doubles by mapping the cluster's endpoints
    to fakes instead of patching the global fan-out.
    """

    def _ensure_cluster(self):
        cluster = ProxmoxCluster.objects.filter(enabled=True).first()
        if cluster is None:
            cluster = ProxmoxCluster.objects.create(
                key="default", display_name="Default cluster", enabled=True
            )
        return cluster

    def _configure_cluster(self, *fakes):
        cluster = self._ensure_cluster()
        endpoints = []
        for index, fake in enumerate(fakes):
            endpoints.append(
                ProxmoxEndpoint.objects.create(
                    name=f"tagfix-{index}",
                    url=getattr(fake, "endpoint", f"https://fake-{index}:8006"),
                    cluster=cluster,
                    enabled=True,
                )
            )
        by_name = {endpoint.name: fake for endpoint, fake in zip(endpoints, fakes)}
        mocked = patch(
            "core.services.cluster_resolver.client_for_endpoint",
            side_effect=lambda endpoint: by_name[endpoint.name],
        )
        mocked.start()
        self.addCleanup(mocked.stop)
        return cluster


class TagServiceTests(ClusterFixtureMixin, TestCase):
    class FakeRegistryClient:
        def __init__(self, final_options=None, final_error=None):
            self.final_options = final_options or {}
            self.final_error = final_error
            self.writes = []

        def set_cluster_options(self, updates, *, delete=None):
            self.writes.append((dict(updates), list(delete or [])))

        def cluster_options(self):
            if self.final_error:
                raise self.final_error
            return dict(self.final_options)

    def setUp(self):
        self.cluster = self._ensure_cluster()

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

    @patch(
        "core.services.tag_registry.cluster_options",
        return_value=(object(), {"registered-tags": "fresh"}, ""),
    )
    def test_explicit_registry_refresh_bypasses_and_replaces_display_cache(self, cluster_options):
        cache_key = tag_registry_cache_key(self.cluster)
        cache.set(cache_key, ({"stale": RegisteredTag("stale")}, ""), 60)

        refreshed, error = refresh_registered_tags()

        self.assertEqual(error, "")
        self.assertEqual(list(refreshed), ["fresh"])
        self.assertEqual(list(cache.get(cache_key)[0]), ["fresh"])
        cluster_options.assert_called_once_with(self.cluster)

    @patch("core.services.tag_registry.cluster_options")
    def test_registry_write_reloads_and_caches_actual_final_state(self, cluster_options):
        client = self.FakeRegistryClient(
            final_options={
                "registered-tags": "existing;external;prod",
                "tag-style": "shape=full,color-map=prod:112233:ffffff",
            }
        )
        cluster_options.return_value = (
            client,
            {"registered-tags": "existing", "tag-style": "shape=full"},
            "",
        )

        actual, error = register_tag("prod", "#112233")
        self.cluster.refresh_from_db()

        self.assertEqual(error, "")
        self.assertEqual(set(actual), {"existing", "external", "prod"})
        self.assertEqual(actual["prod"].background, "112233")
        self.assertEqual(set(cache.get(tag_registry_cache_key(self.cluster))[0]), set(actual))
        self.assertEqual(len(client.writes), 1)
        updates, delete = client.writes[0]
        self.assertEqual(updates["registered-tags"], "existing;prod")
        self.assertIn("shape=full", updates["tag-style"])
        self.assertEqual(delete, [])

    @patch("core.services.tag_registry.cluster_options")
    def test_registry_write_reports_concurrent_postcondition_conflict_without_retry(self, cluster_options):
        client = self.FakeRegistryClient(final_options={"registered-tags": "existing;external"})
        cluster_options.return_value = (client, {"registered-tags": "existing"}, "")

        actual, error = register_tag("prod")
        self.cluster.refresh_from_db()

        self.assertEqual(error, TAG_REGISTRY_CONFLICT_ERROR)
        self.assertEqual(set(actual), {"existing", "external"})
        self.assertEqual(
            set(cache.get(tag_registry_cache_key(self.cluster))[0]), {"existing", "external"}
        )
        self.assertEqual(len(client.writes), 1)
        cluster_options.assert_called_once_with(self.cluster)

    @patch("core.services.tag_registry.cluster_options")
    def test_unverifiable_registry_write_invalidates_display_cache(self, cluster_options):
        client = self.FakeRegistryClient(final_error=ProxmoxAPIError("verification unavailable"))
        cluster_options.return_value = (client, {"registered-tags": "existing"}, "")
        old_cache_key = tag_registry_cache_key(self.cluster)
        cache.set(old_cache_key, ({"stale": RegisteredTag("stale")}, ""), 60)

        actual, error = register_tag("prod")
        self.cluster.refresh_from_db()

        self.assertEqual(actual, {})
        self.assertIn("write was submitted", error)
        self.assertIn("could not be verified", error)
        self.assertNotEqual(old_cache_key, tag_registry_cache_key(self.cluster))
        self.assertEqual(len(client.writes), 1)

    @patch("core.services.tag_registry.cluster_options")
    def test_recolor_and_unregister_verify_their_specific_postconditions(self, cluster_options):
        recolor_client = self.FakeRegistryClient(
            final_options={"registered-tags": "prod", "tag-style": "color-map=prod:abcdef:000000"}
        )
        unregister_client = self.FakeRegistryClient(final_options={"registered-tags": "other"})
        cluster_options.side_effect = [
            (
                recolor_client,
                {"registered-tags": "prod", "tag-style": "color-map=prod:112233:ffffff"},
                "",
            ),
            (unregister_client, {"registered-tags": "other;prod"}, ""),
        ]

        recolored, recolor_error = recolor_tag("prod", "#abcdef")
        remaining, unregister_error = unregister_tag("prod")

        self.assertEqual(recolor_error, "")
        self.assertEqual(recolored["prod"].background, "abcdef")
        self.assertEqual(unregister_error, "")
        self.assertEqual(set(remaining), {"other"})

    def test_inventory_includes_registered_unused_and_assigned_tags(self):
        CurrentGuestInventory.objects.create(
            node="pve1", object_type="vm", vmid=100, name="one", observed_at=timezone.now(),
            config={"tags": "prod"},
        )
        registered = parse_registered_tags({"registered-tags": "prod;unused"})
        rows = {row.name: row for row in inventory_rows(CurrentGuestInventory.objects.all(), registered)}
        self.assertEqual(rows["prod"].guest_count, 1)
        self.assertEqual(rows["unused"].guest_count, 0)

    @patch(
        "core.services.tag_catalog.registered_tags",
        return_value=({"unused": RegisteredTag("unused", "112233", "ffffff")}, "registry warning"),
    )
    def test_catalog_unifies_names_colors_freshness_and_degraded_metadata(self, _registered):
        refreshed_at = timezone.now()
        cluster = self._ensure_cluster()
        CurrentGuestInventory.objects.create(
            cluster=cluster,
            node="pve1",
            object_type="vm",
            vmid=100,
            name="selected",
            observed_at=refreshed_at,
            config={"tags": "prod"},
        )
        CurrentGuestInventoryState.objects.create(
            cluster=cluster,
            refreshed_at=refreshed_at,
            complete=False,
            endpoints_attempted=["pve1", "pve2"],
            endpoints_succeeded=["pve1"],
            errors={"live_inventory": ["pve2 unavailable"]},
        )

        catalog = load_tag_catalog()

        self.assertEqual(catalog.assigned, ("prod",))
        self.assertEqual(catalog.available, ("prod", "unused"))
        self.assertEqual(catalog.chip("unused").background, "112233")
        self.assertEqual(catalog.inventory_refreshed_at, refreshed_at)
        self.assertTrue(catalog.degraded)
        self.assertEqual(catalog.errors, ("registry warning", "pve2 unavailable"))
        self.assertEqual(catalog.metadata()["endpoints_succeeded"], ["pve1"])

    @patch("core.services.tag_catalog.registered_tags", side_effect=RuntimeError("programming bug"))
    def test_catalog_does_not_hide_unexpected_registry_errors(self, _registered):
        with self.assertRaisesMessage(RuntimeError, "programming bug"):
            load_tag_catalog()

    def test_tag_operation_confirmation_is_bound_to_user_and_expires(self):
        summary = SimpleNamespace(
            guest_count=1,
            guests=[
                SimpleNamespace(
                    cluster_key=self.cluster.key, node="pve1", object_type="vm", vmid=100
                )
            ],
            registered=True,
        )
        token = issue_tag_operation_confirmation(
            operation="delete",
            tag="prod",
            summary=summary,
            user_id=10,
            cluster_key=self.cluster.key,
        )

        confirmation, error = validate_tag_operation_confirmation(
            token,
            operation="delete",
            tag="prod",
            summary=summary,
            user_id=11,
            cluster_key=self.cluster.key,
        )
        self.assertIsNone(confirmation)
        self.assertEqual(error, INVALID_CONFIRMATION_ERROR)

        with patch(
            "core.services.tag_operation_confirmation.TAG_OPERATION_CONFIRMATION_MAX_AGE_SECONDS",
            -1,
        ):
            confirmation, error = validate_tag_operation_confirmation(
                token,
                operation="delete",
                tag="prod",
                summary=summary,
                user_id=10,
                cluster_key=self.cluster.key,
            )
        self.assertIsNone(confirmation)
        self.assertEqual(error, INVALID_CONFIRMATION_ERROR)

    @patch("core.services.tag_catalog.registered_tags", return_value=({}, ""))
    def test_guest_tag_choices_include_tags_from_the_full_latest_scan(self, _registered):
        CurrentGuestInventory.objects.create(
            node="pve1", object_type="vm", vmid=100, name="selected", observed_at=timezone.now(),
            config={"tags": "prod"},
        )
        CurrentGuestInventory.objects.create(
            node="pve2", object_type="vm", vmid=200, name="other", observed_at=timezone.now(),
            config={"tags": "qa"},
        )
        selected_row = SimpleNamespace(tags=["prod"])

        self.assertEqual(_decorate_guest_tag_chips([selected_row]), ["prod", "qa"])

    def test_verified_inventory_is_complete_when_the_cluster_answers_once(self):
        # cluster/resources is a cluster-wide response from any member, so a
        # redundant endpoint failing first degrades that endpoint's health without
        # making the cluster's answer partial.
        unavailable = FakeProviderClient("https://pve2:8006", error=ProxmoxAPIError("unavailable"))
        available = FakeProviderClient(
            "https://pve1:8006",
            [{"node": "pve1", "type": "qemu", "vmid": 100, "name": "one", "tags": "prod"}],
        )
        self._configure_cluster(unavailable, available)

        with self.assertLogs("core.services.cluster_resolver", level="WARNING") as logs:
            result = fetch_verified_guest_inventory()

        self.assertTrue(result.complete)
        self.assertEqual(result.guests[0].tags, ("prod",))
        self.assertEqual(available.test_path, "cluster/resources?type=vm")
        self.assertIn("operation=verified_guest_inventory", logs.output[0])

    def test_verified_inventory_is_incomplete_when_no_endpoint_answers(self):
        # Guests are then unknown, not absent: destructive callers must fail closed.
        self._configure_cluster(
            FakeProviderClient("https://pve1:8006", error=ProxmoxAPIError("unavailable")),
            FakeProviderClient("https://pve2:8006", error=ProxmoxAPIError("unavailable")),
        )

        result = fetch_verified_guest_inventory()

        self.assertFalse(result.complete)
        self.assertEqual(result.guests, ())
        self.assertEqual(len(result.errors), 2)

    def test_verified_inventory_never_reads_another_cluster(self):
        other = ProxmoxCluster.objects.create(key="other", display_name="Other", enabled=False)
        stranger = FakeProviderClient(
            "https://other:8006",
            [{"node": "pve1", "type": "qemu", "vmid": 100, "name": "other-cluster", "tags": ""}],
        )
        ProxmoxEndpoint.objects.create(
            name="other-1", url="https://other:8006", cluster=other, enabled=True
        )
        self._configure_cluster(FakeProviderClient("https://pve1:8006", error=ProxmoxAPIError("down")))

        result = fetch_verified_guest_inventory()

        self.assertFalse(result.complete)
        self.assertEqual(result.guests, ())
        self.assertIsNone(getattr(stranger, "test_path", None))

    def test_verified_inventory_does_not_hide_unexpected_errors(self):
        client = SimpleNamespace(endpoint="https://pve1:8006")
        client.get = Mock(side_effect=RuntimeError("programming bug"))
        self._configure_cluster(client)

        with self.assertRaisesMessage(RuntimeError, "programming bug"):
            fetch_verified_guest_inventory()


class TagViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("tag-admin")
        self.client.force_login(self.user)
        self.cluster = ProxmoxCluster.objects.create(
            key="default", display_name="Default cluster", enabled=True
        )
        self.scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        CurrentGuestInventory.objects.create(
            cluster=self.cluster,
            node="pve1", object_type="vm", vmid=100, name="vm-one", observed_at=timezone.now(),
            config={"tags": "prod"},
        )
        cache_key = tag_registry_cache_key(self.cluster)
        cache.set(cache_key, ({}, ""), 60)
        self.addCleanup(cache.delete, cache_key)
        lineage_patch = patch("core.views.tags.common.fetch_live_guest_lineage", return_value={})
        lineage_patch.start()
        self.addCleanup(lineage_patch.stop)

    def _confirmation(self, operation: str, tag: str = "prod") -> str:
        summary = next(row for row in load_tag_catalog().summaries if row.name == tag)
        return issue_tag_operation_confirmation(
            operation=operation,
            tag=tag,
            summary=summary,
            user_id=self.user.pk,
            cluster_key=self.cluster.key,
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

    @patch("core.services.tag_catalog.registered_tags", return_value=({}, ""))
    def test_overview_and_detail_render(self, _registered):
        response = self.client.get(reverse("core:tags_overview"))
        self.assertContains(response, "prod")
        response = self.client.get(reverse("core:tag_detail"), {"tag": "prod"})
        self.assertContains(response, "vm-one")
        self.assertContains(response, 'name="confirmation"', count=2)

    @patch(
        "core.views.tags.common.fetch_live_guest_lineage",
        side_effect=ProxmoxAPIError("lineage unavailable"),
    )
    def test_tag_detail_labels_expected_lineage_degradation(self, _lineage):
        with self.assertLogs("core.views.tags", level="WARNING") as logs:
            response = self.client.get(reverse("core:tag_detail"), {"tag": "prod"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Guest type classification is partial")
        self.assertContains(response, "Linked-clone classification is temporarily unavailable")
        self.assertIn("operation=tag_detail_linked_clone_lineage", logs.output[0])

    @patch(
        "core.views.tags.common.fetch_live_guest_lineage",
        side_effect=RuntimeError("programming bug"),
    )
    def test_tag_detail_does_not_hide_unexpected_lineage_errors(self, _lineage):
        with self.assertRaisesMessage(RuntimeError, "programming bug"):
            self.client.get(reverse("core:tag_detail"), {"tag": "prod"})

    @patch("core.services.tag_catalog.registered_tags", return_value=({}, ""))
    def test_overview_ignores_newer_completed_scans_without_guest_inventory(self, _registered):
        ScanRun.objects.create(status=ScanRun.Status.COMPLETED)

        response = self.client.get(reverse("core:tags_overview"))

        self.assertContains(response, "prod")
        prod = next(row for row in response.context["tag_rows"] if row.name == "prod")
        self.assertEqual(prod.guest_count, 1)

    @patch("core.views.guests.read_model_support._apply_workspace_lineage", return_value=[])
    @patch("core.views.guests.read_model_support._guest_rows", return_value=([], False, None))
    @patch(
        "core.services.tag_catalog.registered_tags",
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

    @patch("core.services.tag_inventory_refresh.async_task", return_value="refresh-task-1")
    def test_refresh_fetch_queues_control_plane_operation_without_redirect(self, enqueue):
        response = self.client.post(
            reverse("core:tags_refresh"),
            HTTP_ACCEPT="application/json",
            HTTP_X_REQUESTED_WITH="fetch",
        )

        self.assertEqual(response.status_code, 202)
        event = AuditEvent.objects.get(action="tag.inventory.refresh")
        self.assertEqual(event.user, self.user)
        self.assertEqual(event.outcome, "queued")
        self.assertEqual(event.details["worker_task_id"], "refresh-task-1")
        self.assertEqual(response.json()["task_id"], f"guest:{event.id}")
        enqueue.assert_called_once()

    def test_refresh_rejects_second_active_operation(self):
        AuditEvent.objects.create(
            action="tag.inventory.refresh",
            object_type="tag_inventory",
            object_id=self.cluster.key,
            outcome="running",
            details={"cluster_key": self.cluster.key},
        )

        response = self.client.post(
            reverse("core:tags_refresh"),
            HTTP_ACCEPT="application/json",
            HTTP_X_REQUESTED_WITH="fetch",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("already", response.json()["error"])

    @patch("core.services.tag_actions.async_task", side_effect=RuntimeError("queue unavailable"))
    @patch("core.views.tags.prepare_tag_operation", return_value="")
    def test_bulk_operation_enqueue_failure_is_immediately_retryable(self, _prepare, _enqueue):
        response = self.client.post(
            reverse("core:tag_operation"),
            {"operation": "delete", "tag": "prod", "confirmation": self._confirmation("delete")},
        )

        self.assertRedirects(response, reverse("core:tags_overview"), fetch_redirect_response=False)
        event = AuditEvent.objects.filter(action="tag.bulk_operation").latest("id")
        self.assertEqual(event.outcome, "failed")
        self.assertEqual(event.details["stage"], "enqueue failed")
        self.assertTrue(event.details["retryable"])

    @patch("core.services.tag_actions.async_task", return_value="worker-task-1")
    @patch("core.views.tags.prepare_tag_operation", return_value="")
    def test_bulk_operation_persists_queue_identity_and_timestamp(self, _prepare, _enqueue):
        self.client.post(
            reverse("core:tag_operation"),
            {"operation": "delete", "tag": "prod", "confirmation": self._confirmation("delete")},
        )

        event = AuditEvent.objects.filter(action="tag.bulk_operation").latest("id")
        self.assertEqual(event.outcome, "queued")
        self.assertEqual(event.details["worker_task_id"], "worker-task-1")
        self.assertIsNotNone(event.details["queued_at"])

    @patch("core.views.tags.prepare_tag_operation")
    def test_bulk_operation_rejects_missing_confirmation_before_audit_or_prepare(self, prepare):
        response = self.client.post(
            reverse("core:tag_operation"),
            {"operation": "delete", "tag": "prod"},
            follow=True,
        )

        self.assertContains(response, INVALID_CONFIRMATION_ERROR)
        self.assertFalse(AuditEvent.objects.filter(action="tag.bulk_operation").exists())
        prepare.assert_not_called()

    @patch("core.views.tags.prepare_tag_operation")
    def test_bulk_operation_rejects_confirmation_for_another_operation(self, prepare):
        response = self.client.post(
            reverse("core:tag_operation"),
            {
                "operation": "delete",
                "tag": "prod",
                "confirmation": self._confirmation("rename"),
            },
            follow=True,
        )

        self.assertContains(response, INVALID_CONFIRMATION_ERROR)
        self.assertFalse(AuditEvent.objects.filter(action="tag.bulk_operation").exists())
        prepare.assert_not_called()

    @patch("core.views.tags.prepare_tag_operation")
    def test_bulk_operation_rejects_changed_membership_before_audit_or_prepare(self, prepare):
        confirmation = self._confirmation("delete")
        CurrentGuestInventory.objects.create(
            node="pve2",
            object_type="ct",
            vmid=200,
            name="ct-two",
            observed_at=timezone.now(),
            config={"tags": "prod"},
        )

        response = self.client.post(
            reverse("core:tag_operation"),
            {"operation": "delete", "tag": "prod", "confirmation": confirmation},
            follow=True,
        )

        self.assertContains(response, CHANGED_CONFIRMATION_ERROR)
        self.assertFalse(AuditEvent.objects.filter(action="tag.bulk_operation").exists())
        prepare.assert_not_called()

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

    @patch("core.services.tag_catalog.registered_tags", return_value=({}, ""))
    def test_detail_offers_assignment_for_an_unassigned_guest(self, _registered):
        other = CurrentGuestInventory.objects.create(
            cluster=self.cluster,
            node="pve1", object_type="ct", vmid=101, name="ct-two", observed_at=timezone.now(),
            config={},
        )
        response = self.client.get(reverse("core:tag_detail"), {"tag": "prod"})
        self.assertContains(response, "Assign objects")
        self.assertContains(response, other.name)

    @patch("core.services.tag_catalog.registered_tags", return_value=({}, ""))
    def test_detail_offers_per_guest_tag_removal_for_user_tags(self, _registered):
        response = self.client.get(reverse("core:tag_detail"), {"tag": "prod"})

        self.assertContains(response, 'data-tag-unassign-form')
        self.assertContains(response, 'name="tags_mode" value="remove"')
        self.assertContains(response, 'name="guest" value="gr1:default:vm:100@pve1"')


class TagInventoryRefreshTests(ClusterFixtureMixin, TestCase):
    def setUp(self):
        self.cluster = self._ensure_cluster()
        self.event = AuditEvent.objects.create(
            username="admin",
            action="tag.inventory.refresh",
            object_type="tag_inventory",
            object_id=self.cluster.key,
            outcome="queued",
            details={
                "cluster_key": self.cluster.key,
                "stage": "queued",
                "queued_at": timezone.now().isoformat(),
            },
        )

    @patch("core.services.tag_inventory_refresh.reconcile_live_guest_inventory")
    @patch("core.services.tag_inventory_refresh.fetch_verified_guest_inventory")
    @patch("core.services.tag_inventory_refresh.refresh_registered_tags")
    def test_complete_worker_refreshes_registry_and_membership(self, registry, fetch, reconcile):
        refreshed_at = timezone.now()
        registry.return_value = ({"prod": RegisteredTag("prod")}, "")
        fetch.return_value = VerifiedGuestInventory(
            guests=(),
            attempted_endpoints=("https://pve1:8006",),
            successful_endpoints=("https://pve1:8006",),
            errors=(),
        )
        reconcile.return_value = SimpleNamespace(refreshed_at=refreshed_at)

        execute_tag_inventory_refresh(self.event.id)

        self.event.refresh_from_db()
        self.assertEqual(self.event.outcome, "success")
        self.assertEqual(self.event.details["registry_count"], 1)
        self.assertTrue(self.event.details["membership_reconciled"])
        self.assertEqual(self.event.details["endpoints_succeeded"], ["https://pve1:8006"])
        task = next(task for task in recent_task_page(limit=20).tasks if task["action"] == "tag.inventory.refresh")
        self.assertEqual(task["name"], "Refresh tag inventory")
        self.assertEqual(task["status"], "Completed")

    @patch("core.services.tag_inventory_refresh.reconcile_live_guest_inventory")
    @patch("core.services.tag_inventory_refresh.fetch_verified_guest_inventory")
    @patch("core.services.tag_inventory_refresh.refresh_registered_tags", return_value=({}, ""))
    def test_partial_endpoint_coverage_is_warning_and_reconciles_successes(self, _registry, fetch, reconcile):
        # The first endpoint failed and the second answered: the cluster's coverage
        # is complete, but the degraded endpoint must still reach the operator.
        fetch.return_value = VerifiedGuestInventory(
            guests=(),
            attempted_endpoints=("https://pve1:8006", "https://pve2:8006"),
            successful_endpoints=("https://pve2:8006",),
            errors=("pve1 unavailable",),
        )
        reconcile.return_value = SimpleNamespace(refreshed_at=timezone.now())

        execute_tag_inventory_refresh(self.event.id)

        self.event.refresh_from_db()
        self.assertTrue(fetch.return_value.complete)
        self.assertEqual(self.event.outcome, "warning")
        self.assertEqual(self.event.details["stage"], "completed with warnings")
        reconcile.assert_called_once_with(fetch.return_value)
        task = next(task for task in recent_task_page(limit=20).tasks if task["action"] == "tag.inventory.refresh")
        self.assertEqual(task["status"], "Completed with warnings")

    @patch("core.services.tag_inventory_refresh.reconcile_live_guest_inventory")
    @patch("core.services.tag_inventory_refresh.fetch_verified_guest_inventory")
    @patch(
        "core.services.tag_inventory_refresh.refresh_registered_tags",
        return_value=({}, "registry unavailable"),
    )
    def test_total_failure_does_not_advance_membership_timestamp(self, _registry, fetch, reconcile):
        fetch.return_value = VerifiedGuestInventory(
            guests=(),
            attempted_endpoints=("https://pve1:8006",),
            successful_endpoints=(),
            errors=("pve1 unavailable",),
        )

        execute_tag_inventory_refresh(self.event.id)

        self.event.refresh_from_db()
        self.assertEqual(self.event.outcome, "failed")
        self.assertFalse(self.event.details["membership_reconciled"])
        reconcile.assert_not_called()

    @patch("core.services.tag_inventory_refresh.async_task")
    def test_queue_service_rejects_existing_active_refresh(self, enqueue):
        with self.assertRaises(TagInventoryRefreshAlreadyActive):
            queue_tag_inventory_refresh(username="admin")
        enqueue.assert_not_called()

    def test_stale_refresh_is_failed_by_control_plane_reaper(self):
        self.event.details = {
            "stage": "running",
            "heartbeat_at": (timezone.now() - timedelta(minutes=20)).isoformat(),
            "worker_task_id": "lost-refresh-task",
        }
        self.event.outcome = "running"
        self.event.save(update_fields=["details", "outcome"])

        result = reap_stale_guest_tasks()

        self.event.refresh_from_db()
        self.assertEqual(result["interrupted_tag_inventory_refreshes"], 1)
        self.assertEqual(self.event.outcome, "failed")
        self.assertIn("start a new refresh", self.event.details["error"])

class TagFanoutTests(ClusterFixtureMixin, TestCase):
    def setUp(self):
        self.scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        self.tag_cluster = self._ensure_cluster()
        self.row = CurrentGuestInventory.objects.create(
            cluster=self.tag_cluster,
            source_scan=self.scan, node="old-node", object_type="vm", vmid=100, name="vm-one",
            observed_at=timezone.now(),
            config={"tags": "old;keep"},
        )
        self.event = AuditEvent.objects.create(
            username="admin",
            action="tag.bulk_operation",
            object_type="tag",
            object_id="old",
            outcome="queued",
            details={"cluster_key": self.tag_cluster.key},
        )

    @patch("core.services.tag_actions.fetch_verified_guest_inventory")
    @patch("core.services.tag_actions.registered_tags", return_value=({}, ""))
    def test_prepare_persists_snapshot_targets_when_live_coverage_is_incomplete(self, _registered, live):
        live.return_value = self.inventory(complete=False, errors=("pve2 unavailable",))
        self.assertEqual(
            prepare_tag_operation(
                self.event,
                operation="delete",
                source_tag="old",
                confirmed_membership_fingerprint=tag_membership_fingerprint([self.row]),
                cluster_key=self.tag_cluster.key,
            ),
            "",
        )
        self.event.refresh_from_db()
        self.assertEqual(self.event.details["targets"][0]["vmid"], 100)
        self.assertEqual(self.event.details["succeeded"], [])
        self.assertFalse(self.event.details["membership_complete"])

    @patch("core.services.tag_actions.fetch_verified_guest_inventory")
    def test_newer_empty_scan_does_not_hide_current_fanout_targets(self, live):
        ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        live.return_value = self.inventory()

        targets, membership = latest_tag_targets("old", cluster=self.tag_cluster)

        self.assertTrue(membership.complete)
        self.assertEqual([(target["object_type"], target["vmid"]) for target in targets], [("vm", 100)])

    @patch("core.services.tag_actions.fetch_verified_guest_inventory")
    @patch("core.services.tag_actions.registered_tags", return_value=({}, ""))
    def test_prepare_unions_snapshot_and_partial_live_targets(self, _registered, live):
        live.return_value = self.inventory(
            SimpleNamespace(node="pve2", object_type="ct", vmid=200, name="ct-two", tags=("old",)),
            complete=False,
            errors=("pve3 unavailable",),
        )

        self.assertEqual(
            prepare_tag_operation(
                self.event,
                operation="delete",
                source_tag="old",
                confirmed_membership_fingerprint=tag_membership_fingerprint(
                    [
                        self.row,
                        {
                            "cluster_key": self.tag_cluster.key,
                            "node": "pve2",
                            "object_type": "ct",
                            "vmid": 200,
                        },
                    ]
                ),
                cluster_key=self.tag_cluster.key,
            ),
            "",
        )

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

        error = prepare_tag_operation(
            self.event,
            operation="delete",
            source_tag="old",
            confirmed_membership_fingerprint=tag_membership_fingerprint([]),
            cluster_key=self.tag_cluster.key,
        )

        self.assertIn("Could not verify", error)
        unregister.assert_not_called()

    @patch("core.services.tag_actions.fetch_verified_guest_inventory")
    @patch("core.services.tag_actions.registered_tags", return_value=({}, ""))
    def test_prepare_rejects_live_membership_added_after_confirmation(self, _registered, live):
        live.return_value = self.inventory(
            SimpleNamespace(node="pve2", object_type="ct", vmid=200, name="ct-two", tags=("old",)),
            complete=True,
        )

        error = prepare_tag_operation(
            self.event,
            operation="delete",
            source_tag="old",
            confirmed_membership_fingerprint=tag_membership_fingerprint([self.row]),
            cluster_key=self.tag_cluster.key,
        )

        self.assertEqual(error, CHANGED_CONFIRMATION_ERROR)
        self.event.refresh_from_db()
        self.assertEqual(self.event.details, {"cluster_key": self.tag_cluster.key})

    @patch("core.services.tag_actions.unregister_tag", return_value=({}, ""))
    @patch("core.services.tag_actions.fetch_verified_guest_inventory")
    def test_execute_rediscovery_digest_and_cache_update(self, live, _unregister):
        class FakeClient:
            def guest_config(self, **_kwargs):
                return {"tags": "old;keep", "digest": "digest-1"}

            def set_guest_config(self, **kwargs):
                self.written = kwargs

        client = FakeClient()
        self._configure_cluster(client)
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
            "cluster_key": self.tag_cluster.key,
            "operation": "delete", "source_tag": "old", "new_tag": "",
            "targets": [{"cluster_key": self.tag_cluster.key, "node": "old-node", "object_type": "vm", "vmid": 100, "name": "vm-one"}],
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
        guest_event = AuditEvent.objects.get(action="tag.membership.removed")
        summary_event = AuditEvent.objects.get(action="tag.deleted", object_id="old")
        self.assertEqual(guest_event.module, "vms")
        self.assertEqual(summary_event.module, "system")

    def test_recent_tasks_only_shows_bulk_lifecycle_not_child_or_summary_audits(self):
        self.event.details = {"operation": "rename", "source_tag": "old", "targets": [{"vmid": 100}]}
        self.event.save(update_fields=["details"])
        AuditEvent.objects.create(
            username="admin",
            action="tag.membership.renamed",
            object_type="guest",
            object_id="vm:100@old-node",
            details={"source_tag": "old", "new_tag": "new", "target_type": "vm", "vmid": 100},
        )
        AuditEvent.objects.create(
            username="admin",
            action="tag.renamed",
            object_type="tag",
            object_id="old",
            details={"source_tag": "old", "new_tag": "new", "operation_event_id": self.event.id},
        )

        tasks = [task for task in recent_task_page(limit=20).tasks if task["action"].startswith("tag.")]

        self.assertEqual([task["id"] for task in tasks], [f"guest:{self.event.id}"])
        self.assertEqual(tasks[0]["name"], "Tag operation")

    @patch("core.services.tag_actions.unregister_tag")
    @patch("core.services.tag_actions.fetch_verified_guest_inventory")
    @patch("core.services.cluster_resolver.client_for_endpoint")
    def test_execute_refuses_ambiguous_same_vmid_rediscovery(self, client_for_endpoint, live, unregister):
        duplicate_guests = (
            SimpleNamespace(object_type="vm", vmid=100, node="node-a", name="one", tags=("old",)),
            SimpleNamespace(object_type="vm", vmid=100, node="node-b", name="two", tags=("old",)),
        )
        live.side_effect = [self.inventory(*duplicate_guests), self.inventory(*duplicate_guests)]
        self.event.details = {
            "cluster_key": self.tag_cluster.key,
            "operation": "delete",
            "source_tag": "old",
            "targets": [{"cluster_key": self.tag_cluster.key, "node": "stale-node", "object_type": "vm", "vmid": 100, "name": "vm-one"}],
            "succeeded": [],
            "skipped": [],
            "failed": [],
        }
        self.event.save(update_fields=["details"])

        execute_tag_operation(self.event.id)

        self.event.refresh_from_db()
        self.assertEqual(self.event.outcome, "failed")
        self.assertIn("not found", self.event.details["failed"][0]["reason"])
        # An ambiguous vmid must be refused before any provider client is built:
        # picking one of two same-vmid guests is exactly the wrong-target write.
        client_for_endpoint.assert_not_called()
        unregister.assert_not_called()

    @patch("core.services.tag_actions.unregister_tag")
    @patch("core.services.tag_actions.fetch_verified_guest_inventory")
    def test_execute_keeps_source_registered_when_final_verification_is_incomplete(
        self, live, unregister
    ):
        self._configure_cluster()  # a cluster with no reachable endpoint
        guest = SimpleNamespace(object_type="vm", vmid=100, node="old-node", name="vm-one", tags=("old",))
        live.side_effect = [
            self.inventory(guest),
            self.inventory(complete=False, errors=("pve2 unavailable",)),
        ]
        self.event.details = {
            "cluster_key": self.tag_cluster.key,
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
    def test_execute_keeps_source_registered_when_a_guest_still_has_it(self, live, unregister):
        self._configure_cluster()  # a cluster with no reachable endpoint
        guest = SimpleNamespace(object_type="vm", vmid=100, node="old-node", name="vm-one", tags=("old",))
        live.side_effect = [self.inventory(guest), self.inventory(guest)]
        self.event.details = {
            "cluster_key": self.tag_cluster.key,
            "operation": "delete", "source_tag": "old", "new_tag": "",
            "targets": [], "succeeded": [], "skipped": [], "failed": [], "username": "admin",
        }
        self.event.save(update_fields=["details"])

        execute_tag_operation(self.event.id)

        self.event.refresh_from_db()
        self.assertEqual(self.event.outcome, "failed")
        self.assertEqual(self.event.details["remaining_targets"][0]["vmid"], 100)
        unregister.assert_not_called()

    def test_partial_rename_retry_preserves_both_registry_names_until_verified_success(self):
        second = CurrentGuestInventory.objects.create(
            source_scan=self.scan,
            node="old-node",
            object_type="ct",
            vmid=101,
            name="ct-two",
            observed_at=timezone.now(),
            config={"tags": "old"},
        )
        first_live = SimpleNamespace(
            node=self.row.node,
            object_type=self.row.object_type,
            vmid=self.row.vmid,
            name=self.row.name,
            tags=("old",),
        )
        second_live = SimpleNamespace(
            node=second.node,
            object_type=second.object_type,
            vmid=second.vmid,
            name=second.name,
            tags=("old",),
        )
        self.event.details = {
            "cluster_key": self.tag_cluster.key,
            "operation": "rename",
            "source_tag": "old",
            "new_tag": "new",
            "targets": [
                {"cluster_key": self.tag_cluster.key, "node": self.row.node, "object_type": self.row.object_type, "vmid": self.row.vmid},
                {"cluster_key": self.tag_cluster.key, "node": second.node, "object_type": second.object_type, "vmid": second.vmid},
            ],
            "succeeded": [],
            "skipped": [],
            "failed": [],
            "username": "admin",
        }
        self.event.save(update_fields=["details"])
        registry = {"old", "new"}

        def unregister(tag, **_kwargs):
            registry.discard(tag)
            return {}, ""

        with (
            patch(
                "core.services.tag_actions.fetch_verified_guest_inventory",
                side_effect=[self.inventory(first_live, second_live), self.inventory(second_live)],
            ),
            patch(
                "core.services.tag_actions._update_target",
                side_effect=[("succeeded", ""), ("failed", "Guest is locked (backup).")],
            ),
            patch("core.services.tag_actions.unregister_tag", side_effect=unregister) as unregister_mock,
        ):
            execute_tag_operation(self.event.id)

        self.event.refresh_from_db()
        self.assertEqual(self.event.outcome, "failed")
        self.assertTrue(self.event.details["retryable"])
        self.assertEqual(registry, {"old", "new"})
        unregister_mock.assert_not_called()

        with patch("core.services.tag_actions.async_task", return_value="retry-rename-task"):
            retry_tag_operation(self.event.id)

        with (
            patch(
                "core.services.tag_actions.fetch_verified_guest_inventory",
                side_effect=[self.inventory(second_live), self.inventory()],
            ),
            patch("core.services.tag_actions._update_target", return_value=("succeeded", "")) as update,
            patch("core.services.tag_actions.unregister_tag", side_effect=unregister) as unregister_mock,
        ):
            execute_tag_operation(self.event.id, 1)

        self.event.refresh_from_db()
        self.assertEqual(self.event.outcome, "success")
        self.assertEqual(registry, {"new"})
        update.assert_called_once()
        unregister_mock.assert_called_once_with("old", cluster=self.tag_cluster)

    @staticmethod
    def inventory(*guests, complete=True, errors=()):
        attempted = ("https://pve1:8006", "https://pve2:8006")
        # Coverage is the cluster's, not each endpoint's: one authoritative answer
        # is complete, and incomplete means no endpoint in the cluster answered.
        successful = attempted[:1] if complete else ()
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

    def test_stale_queued_operation_missing_from_django_q_is_marked_retryable(self):
        self.event.outcome = "queued"
        self.event.details = {
            "operation": "delete",
            "source_tag": "old",
            "targets": [{"vmid": 100}],
            "worker_task_id": "lost-task",
            "queued_at": (timezone.now() - timedelta(minutes=20)).isoformat(),
        }
        self.event.save(update_fields=["outcome", "details"])

        result = reap_stale_guest_tasks()

        self.event.refresh_from_db()
        self.assertEqual(result["interrupted_tag_operations"], 1)
        self.assertEqual(self.event.outcome, "failed")
        self.assertTrue(self.event.details["retryable"])
        self.assertIn("no longer present", self.event.details["error"])

    def test_stale_queued_operation_still_present_in_django_q_is_left_alone(self):
        task_id = "live-queued-task"
        self.event.outcome = "queued"
        self.event.details = {
            "operation": "delete",
            "source_tag": "old",
            "targets": [{"vmid": 100}],
            "worker_task_id": task_id,
            "queued_at": (timezone.now() - timedelta(minutes=20)).isoformat(),
        }
        self.event.save(update_fields=["outcome", "details"])
        OrmQ.objects.create(
            key=BULK_QUEUE_NAME,
            payload=SignedPackage.dumps({"id": task_id}),
            lock=timezone.now(),
        )

        result = reap_stale_guest_tasks()

        self.event.refresh_from_db()
        self.assertEqual(result["interrupted_tag_operations"], 0)
        self.assertEqual(self.event.outcome, "queued")

    @patch("core.services.tag_actions.async_task", return_value="retry-task-2")
    def test_retry_reuses_event_preserves_terminal_targets_and_advances_attempt(self, enqueue):
        succeeded = [{"node": "old-node", "object_type": "vm", "vmid": 100}]
        self.event.outcome = "failed"
        self.event.details = {
            "operation": "delete",
            "source_tag": "old",
            "targets": succeeded + [{"node": "old-node", "object_type": "ct", "vmid": 101}],
            "succeeded": succeeded,
            "skipped": [],
            "failed": [{"vmid": 101, "reason": "locked"}],
            "retryable": True,
            "retry_attempt": 0,
        }
        self.event.save(update_fields=["outcome", "details"])

        task_id = retry_tag_operation(self.event.id)

        self.assertEqual(task_id, "retry-task-2")
        self.event.refresh_from_db()
        self.assertEqual(self.event.outcome, "queued")
        self.assertEqual(self.event.details["retry_attempt"], 1)
        self.assertEqual(self.event.details["succeeded"], succeeded)
        self.assertEqual(self.event.details["failed"], [])
        enqueue.assert_called_once_with(
            "core.services.tag_actions.execute_tag_operation",
            self.event.id,
            1,
            q_options={"cluster": BULK_QUEUE_NAME},
        )

    def test_retry_rejects_non_retryable_operation(self):
        self.event.outcome = "failed"
        self.event.details = {"targets": [{"vmid": 100}]}
        self.event.save(update_fields=["outcome", "details"])

        with self.assertRaisesMessage(TagOperationRetryError, "not available"):
            retry_tag_operation(self.event.id)

    @patch("core.services.tag_actions.async_task", return_value="retry-task-3")
    def test_recent_tasks_retry_endpoint_returns_operation_to_queued(self, _enqueue):
        user = get_user_model().objects.create_user(username="operator", password="unused")
        self.client.force_login(user)
        self.event.outcome = "failed"
        self.event.details = {
            "operation": "delete",
            "source_tag": "old",
            "targets": [{"node": "old-node", "object_type": "vm", "vmid": 100}],
            "failed": [{"vmid": 100, "reason": "locked"}],
            "retryable": True,
        }
        self.event.save(update_fields=["outcome", "details"])

        task = next(item for item in recent_task_page(limit=20).tasks if item["id"] == f"guest:{self.event.id}")
        self.assertEqual(task["status"], "Failed — right-click for options")
        self.assertTrue(task["retryable"])
        self.assertEqual(task["retry_label"], "Failed — right-click for options")
        self.assertIn("1 target(s) failed", task["details"])

        response = self.client.post(reverse("core:retry_recent_task"), {"task_id": f"guest:{self.event.id}"})

        self.assertEqual(response.status_code, 202)
        self.event.refresh_from_db()
        self.assertEqual(self.event.outcome, "queued")
        self.assertFalse(self.event.details.get("retryable", False))

    def test_stale_worker_attempt_cannot_resume_after_retry(self):
        self.event.outcome = "queued"
        self.event.details = {"retry_attempt": 2, "targets": []}
        self.event.save(update_fields=["outcome", "details"])

        with patch("core.services.tag_actions.fetch_verified_guest_inventory") as inventory:
            execute_tag_operation(self.event.id, 1)

        inventory.assert_not_called()
        self.event.refresh_from_db()
        self.assertEqual(self.event.outcome, "queued")

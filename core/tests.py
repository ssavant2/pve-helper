from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import httpx
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import (
    RequestFactory,
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.urls import reverse
from django.utils import timezone
from django_q.models import Schedule

from core.auth import PveHelperOIDCBackend
from core.checks import production_startup_errors
from core.models import (
    AuditEvent,
    ClusterStorage,
    ClusterStorageMount,
    ClusterStorageNodeState,
    ClusterStorageVolumeCoverage,
    ClusterStorageVolumeObservation,
    ConsoleSession,
    CurrentGuestInventory,
    FileInventory,
    OidcIdentity,
    ProxmoxCluster,
    ProxmoxEndpoint,
    ProxmoxInventory,
    ProxmoxStorageConsumer,
    RuntimeConfigurationState,
    ScanRun,
    ScheduledAction,
    ScheduledActionRun,
    StorageCatalogState,
    StorageMount,
    StorageSpaceSnapshot,
    TrashItem,
)
from core.services import runtime_bootstrap
from core.services.audit_retention_schedule import AUDIT_RETENTION_SCHEDULE_NAME, audit_retention_schedule_state
from core.services.classification import categorize_proxmox_path, classify_entry, extract_disk_references
from core.services.cluster_activation import (
    ClusterActivationError,
    enable_cluster,
    set_initial_cluster_key,
)
from core.services.cluster_state_identity import cluster_cache_key
from core.services.file_actions import ReferencedObject, _unverified_consumers, file_action_risk
from core.services.filesystem import MountInfo, StorageSpaceInfo, mount_access_mode, storage_space_info
from core.services.guest_inventory_refresh_schedule import GUEST_INVENTORY_REFRESH_SCHEDULE_NAME
from core.services.proxmox import (
    LIVE_GUEST_DISPLAY_TIMEOUT_SECONDS,
    LIVE_GUEST_INVENTORY_CACHE_SECONDS,
    InventoryResult,
    ProxmoxAPIError,
    ProxmoxClient,
    ProxmoxObject,
    ProxmoxTaskResult,
    ProxmoxTaskTimeout,
    _fetch_live_guest_inventory_uncached,
    _fetch_live_guest_status_uncached,
)
from core.services.recent_tasks import RECENT_TASK_RETENTION_MINUTES, recent_task_page
from core.services.refs import GuestRef, NodeRef, RefParseError
from core.services.runtime_bootstrap import ensure_bootstrap
from core.services.scan_retention import prune_scan_history
from core.services.scan_schedule import SCAN_SCHEDULE_NAME, update_scan_schedule
from core.services.scheduled_actions import (
    SCHEDULED_ACTION_DISPATCH_FUNC,
    SCHEDULED_ACTION_DISPATCH_INTERVAL_MINUTES,
    SCHEDULED_ACTION_DISPATCH_SCHEDULE_NAME,
    dispatch_due_scheduled_actions,
    execute_scheduled_action_run,
    prune_scheduled_action_runs,
    reap_stale_scheduled_action_runs,
)
from core.services.scheduled_recurrence import RecurrenceError, next_run_after
from core.services.space_snapshot_schedule import SPACE_SNAPSHOT_INTERVAL_MINUTES, SPACE_SNAPSHOT_SCHEDULE_NAME
from core.services.storage import StorageEntry, StorageScanner
from core.services.storage_actions import (
    InflatePreflight,
    StorageActionError,
    inflate_storage_file,
    move_file_to_trash,
    normalize_uploaded_proxmox_image_paths,
    validate_inflate_storage_file,
)
from core.services.storage_catalog import SHARED_OBSERVATION_NODE, storage_view
from core.services.storage_details import storage_details
from core.services.storage_mounts import MountHealth, bind_storage_mount
from core.services.trash_schedule import TRASH_PURGE_SCHEDULE_NAME, trash_purge_schedule_state
from core.signals import ensure_always_on_schedules
from core.tasks import (
    _storage_gate_status,
    dispatch_scheduled_actions,
    enqueue_scheduled_scan,
    inflate_storage_file_task,
    normalize_uploaded_proxmox_image_paths_task,
    poll_guest_audit_task,
    purge_expired_audit_events,
    purge_expired_trash,
    reap_stale_guest_tasks,
    record_storage_space_snapshots,
    restore_guest_backup_task,
    run_scan,
)
from pve_helper.settings import external_url_uses_https


class HermeticProxmoxMixin:
    """Explicit default live-data doubles for view tests.

    Individual tests that exercise a live-data branch override these patches
    locally with the precise inventory/status they need.
    """

    def setUp(self):
        super().setUp()
        for target in (
            "core.views.common.fetch_live_guest_lineage",
            "core.views.common.fetch_live_guest_status",
            "core.services.file_actions.fetch_live_guest_status",
        ):
            mocked = patch(target, return_value={})
            mocked.start()
            self.addCleanup(mocked.stop)

    def _patch_provider_client(self, client):
        """Make `client` the provider client the selected cluster resolves to.

        Guest writes now resolve an explicit cluster and build their client from its
        endpoint, so patching the global fan-out alone no longer reaches them. The
        cluster fixture lives here rather than in setUp because this mixin is also
        used by SimpleTestCase classes, which may not touch the database.
        """
        # Only one cluster may be enabled before activation, so reuse whatever the
        # test already configured rather than violating that invariant.
        cluster = ProxmoxCluster.objects.filter(enabled=True).first()
        if cluster is None:
            cluster = ProxmoxCluster.objects.create(key="default", display_name="Default cluster", enabled=True)
        if not ProxmoxEndpoint.objects.filter(cluster=cluster, enabled=True).exists():
            ProxmoxEndpoint.objects.create(
                name="hermetic-pve",
                url="https://pve.test.invalid:8006",
                cluster=cluster,
                enabled=True,
            )
        self.test_cluster = cluster

        mocked = patch("core.services.cluster_resolver.client_for_endpoint", return_value=client)
        mocked.start()
        self.addCleanup(mocked.stop)
        return client


def browser_url(mount, path: str = "") -> str:
    """The Files tab of the datastore a host mount belongs to.

    The file browser is no longer a page of its own: a mount is pve-helper's
    access to a *datastore*, so browsing happens on that datastore's page. Tests
    that only need a mount get a minimal binding here, because a mount with no
    binding has no datastore to browse and that is the product's answer, not a
    test-setup shortcut.
    """
    from core.views.storage import _storage_browser_url

    if isinstance(mount, str):
        from core.services.storage_mounts import resolve_storage_mount

        mount = resolve_storage_mount(mount)
    if not mount.cluster_bindings.exists():
        cluster = ProxmoxCluster.objects.first() or ProxmoxCluster.objects.create(
            key="default", display_name="Default cluster", enabled=True
        )
        definition, _ = ClusterStorage.objects.get_or_create(
            cluster=cluster,
            storage_id=mount.storage_id,
            # `dir`, because these mounts are temp directories rather than real
            # network mounts: an `nfs` definition would demand a mountpoint and
            # every file test would fail on health rather than on its own subject.
            defaults={"storage_type": "dir", "shared": True, "present": True},
        )
        ClusterStorageMount.objects.create(
            cluster_storage=definition, mount=mount, node=None, scope=ClusterStorageMount.Scope.SHARED
        )
        # A healthy datastore, published and completely covered. Without this the
        # usage preflight has no evidence and refuses every destructive action —
        # correctly, but that is the coverage gate's own subject, not these tests'.
        generation = uuid.uuid4()
        StorageCatalogState.objects.update_or_create(
            cluster=cluster,
            defaults={
                "metadata_generation": generation,
                "metadata_complete": True,
                "metadata_refreshed_at": timezone.now(),
            },
        )
        ClusterStorageVolumeCoverage.objects.update_or_create(
            cluster_storage=definition,
            scope=ClusterStorageVolumeCoverage.Scope.SHARED,
            node=None,
            defaults={
                "volume_generation": uuid.uuid4(),
                "based_on_metadata_generation": generation,
                "refreshed_at": timezone.now(),
                "complete": True,
            },
        )
    return _storage_browser_url(mount, path)


def unbound_files_url(cluster_key: str, storage_id: str, path: str = "") -> str:
    """A datastore Files URL written out, for tests that must leave a mount unbound.

    `_require_file_not_blocked` takes a different route once a mount has a cluster
    binding: it runs the fresh usage preflight instead of the offline fallback.
    Tests whose subject is the *file* action, not the coverage gate, keep the
    mount unbound and name the URL directly rather than deriving it from a
    binding they must not have.
    """
    suffix = f"?path={path}" if path else ""
    return f"/clusters/{cluster_key}/datastores/{storage_id}/files/{suffix}"


class LiveGuestDisplayReadTests(TestCase):
    """Display reads resolve an explicit cluster and read through its endpoints."""

    def test_live_guest_inventory_uses_cluster_resources(self):
        client = Mock()

        def get(path, timeout=None):
            if path == "cluster/resources?type=vm":
                return [
                    {"type": "qemu", "vmid": 500, "name": "Lab VM", "node": "pve1", "status": "running"},
                    {"type": "lxc", "vmid": 101, "name": "Lab CT", "node": "pve2", "status": "stopped"},
                ]
            if path == "nodes":
                return []
            raise AssertionError(f"Unexpected Proxmox path: {path}")

        client.get.side_effect = get

        # The read resolves its cluster and builds the client from that cluster's
        # endpoint, so the double is bound there rather than to the global fan-out.
        cluster = ProxmoxCluster.objects.create(key="default", display_name="Default", enabled=True)
        ProxmoxEndpoint.objects.create(
            name="inv-pve", url="https://pve.test.invalid:8006", cluster=cluster, enabled=True
        )
        with patch("core.services.cluster_resolver.client_for_endpoint", return_value=client):
            guests = _fetch_live_guest_inventory_uncached(cluster=cluster)

        self.assertEqual(
            [(guest.object_type, guest.vmid, guest.name, guest.node) for guest in guests],
            [
                ("ct", 101, "Lab CT", "pve2"),
                ("vm", 500, "Lab VM", "pve1"),
            ],
        )


class PowerActionMapTests(SimpleTestCase):
    def test_power_action_map_and_vm_only_set(self):
        from core.views.common import (
            GUEST_POWER_ACTIONS,
            POWER_ACTION_REQUESTS,
            VM_ONLY_POWER_ACTIONS,
        )

        self.assertEqual(POWER_ACTION_REQUESTS["suspend"], ("status/suspend", {}))
        self.assertEqual(POWER_ACTION_REQUESTS["hibernate"], ("status/suspend", {"todisk": 1}))
        self.assertEqual(POWER_ACTION_REQUESTS["resume"], ("status/resume", {}))
        for action in ("suspend", "resume", "hibernate"):
            self.assertIn(action, GUEST_POWER_ACTIONS)
            self.assertIn(action, VM_ONLY_POWER_ACTIONS)
        # every declared power action has an endpoint mapping
        self.assertEqual(set(POWER_ACTION_REQUESTS), GUEST_POWER_ACTIONS)


class MigrateActionTests(HermeticProxmoxMixin, SimpleTestCase):
    def _detail(self, **kwargs):
        from types import SimpleNamespace

        from core.models import ProxmoxInventory

        base = dict(
            object_type=ProxmoxInventory.ObjectType.VM,
            vmid=500,
            name="ubuntu-test",
            node="pve3",
            status="running",
            config={"scsi0": "TrueNAS-VM:500/vm-500-disk-0.raw,size=32G", "ide2": "none,media=cdrom"},
        )
        base.update(kwargs)
        return SimpleNamespace(**base)

    def test_migrate_is_a_registered_bulk_action(self):
        from core.views.common import VM_BULK_ACTIONS

        self.assertIn("migrate", VM_BULK_ACTIONS)

    def test_movable_disks_lists_disks_but_not_cdrom(self):
        from core.views.guests import _guest_movable_disks

        disks = _guest_movable_disks(self._detail())
        self.assertEqual([d["key"] for d in disks], ["scsi0"])
        self.assertEqual(disks[0]["storage"], "TrueNAS-VM")

    def test_movable_volumes_for_container(self):
        from core.models import ProxmoxInventory
        from core.views.guests import _guest_movable_disks

        detail = self._detail(
            object_type=ProxmoxInventory.ObjectType.CT,
            config={"rootfs": "TrueNAS-VM:900/vm-900-disk-0.raw,size=8G", "mp0": "TrueNAS-FS:900/data.raw,size=50G"},
        )
        self.assertEqual([d["key"] for d in _guest_movable_disks(detail)], ["mp0", "rootfs"])

    def test_nic_bridges_parsed_from_config(self):
        from core.views.guests import _guest_nic_bridges

        detail = self._detail(
            config={
                "net0": "virtio=BC:24:11:70:D6:A2,bridge=server10,firewall=1",
                "net1": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
                "scsi0": "TrueNAS-VM:500/vm-500-disk-0.raw,size=32G",
            }
        )
        self.assertEqual(
            _guest_nic_bridges(detail),
            [{"key": "net0", "bridge": "server10"}, {"key": "net1", "bridge": "vmbr0"}],
        )

    def test_cpu_model_parsed_vm_only(self):
        from core.models import ProxmoxInventory
        from core.views.guests import _guest_cpu_model

        self.assertEqual(_guest_cpu_model(self._detail(config={"cpu": "x86-64-v2-AES,flags=+aes"})), "x86-64-v2-AES")
        self.assertEqual(_guest_cpu_model(self._detail(config={"cpu": "host"})), "host")
        # unset → portable default, no model to check
        self.assertEqual(_guest_cpu_model(self._detail(config={})), "")
        # CT shares the host kernel: no CPU model
        ct = self._detail(object_type=ProxmoxInventory.ObjectType.CT, config={"cpu": "host"})
        self.assertEqual(_guest_cpu_model(ct), "")

    def _running_event(self):
        from types import SimpleNamespace

        return SimpleNamespace(id=1, details={})

    def test_host_migration_requires_a_different_target_node(self):
        from core.views.guests import _migrate_guest_from_bulk_request

        request = RequestFactory().post("/", {"migrate_kind": "host", "migrate_target_node": "pve3"})
        err, _details, response, client = _migrate_guest_from_bulk_request(
            request, self._detail(), self._running_event()
        )
        self.assertIn("must differ", err)
        self.assertIsNone(response)
        self.assertIsNone(client)

    def test_storage_migration_requires_a_target_storage(self):
        from core.views.guests import _migrate_guest_from_bulk_request

        request = RequestFactory().post("/", {"migrate_kind": "storage", "migrate_target_storage": ""})
        err, _details, _response, _client = _migrate_guest_from_bulk_request(
            request, self._detail(), self._running_event()
        )
        self.assertIn("target storage", err.lower())

    def test_storage_migration_noop_when_all_disks_already_on_target(self):
        from core.views.guests import _migrate_guest_from_bulk_request

        # the guest's only disk is already on TrueNAS-VM → nothing to move
        request = RequestFactory().post("/", {"migrate_kind": "storage", "migrate_target_storage": "TrueNAS-VM"})
        err, details, response, _client = _migrate_guest_from_bulk_request(
            request, self._detail(), self._running_event()
        )
        self.assertEqual(err, "")
        self.assertTrue(details.get("noop"))
        self.assertIsNone(response)

    def test_unknown_kind_is_rejected(self):
        from core.views.guests import _migrate_guest_from_bulk_request

        request = RequestFactory().post("/", {"migrate_kind": "sideways"})
        err, _details, _response, _client = _migrate_guest_from_bulk_request(
            request, self._detail(), self._running_event()
        )
        self.assertIn("Choose what to migrate", err)


class GuestHealthTests(SimpleTestCase):
    def _detail(self, *, object_type=None, lock=""):
        from types import SimpleNamespace

        from core.models import ProxmoxInventory

        return SimpleNamespace(
            object_type=object_type or ProxmoxInventory.ObjectType.VM,
            vmid=501,
            node="pve3",
            current={"lock": lock} if lock else {},
            config={},
        )

    def test_display_lock_drops_suspended(self):
        from core.views.guests import _display_lock

        self.assertEqual(_display_lock("suspended"), "")
        self.assertEqual(_display_lock("backup"), "backup")
        self.assertEqual(_display_lock(""), "")

    def test_health_flags_lock_with_qm_unlock_command(self):
        from core.views.guests import _guest_health

        health = _guest_health(self._detail(lock="backup"))
        self.assertFalse(health["ok"])
        issue = health["issues"][0]
        self.assertEqual(issue["command"], "qm unlock 501")
        self.assertIn("backup", issue["title"])

    def test_health_uses_pct_unlock_for_containers(self):
        from core.models import ProxmoxInventory
        from core.views.guests import _guest_health

        health = _guest_health(self._detail(object_type=ProxmoxInventory.ObjectType.CT, lock="snapshot"))
        self.assertEqual(health["issues"][0]["command"], "pct unlock 501")

    def test_health_ok_when_unlocked_or_hibernated(self):
        from core.views.guests import _guest_health

        self.assertTrue(_guest_health(self._detail())["ok"])
        # 'suspended' is hibernate, not a health problem
        self.assertTrue(_guest_health(self._detail(lock="suspended"))["ok"])


class GuestLineageTests(SimpleTestCase):
    def _row(self, vmid, name="", ct=False, cluster=None):
        from types import SimpleNamespace

        from core.models import ProxmoxInventory

        cluster = cluster or SimpleNamespace(key="default")
        ot = ProxmoxInventory.ObjectType.CT if ct else ProxmoxInventory.ObjectType.VM
        return SimpleNamespace(
            cluster=cluster,
            cluster_key=getattr(cluster, "key", ""),
            vmid=vmid,
            object_type=ot,
            name=name or f"g{vmid}",
        )

    def test_children_indent_under_parent_template(self):
        from unittest.mock import patch

        from core.views.guests import _apply_workspace_lineage

        rows = [self._row(100, "template"), self._row(101), self._row(102), self._row(200, ct=True)]
        with patch("core.views.common.stored_guest_lineage", return_value={101: 100, 102: 100}):
            ordered = _apply_workspace_lineage(rows)
        self.assertEqual([(r.vmid, r.depth) for r in ordered], [(100, 0), (101, 1), (102, 1), (200, 0)])
        clone = next(r for r in ordered if r.vmid == 101)
        self.assertEqual(clone.parent_vmid, 100)
        self.assertEqual(clone.lineage_parent_name, "template")

    def test_no_lineage_leaves_flat(self):
        from unittest.mock import patch

        from core.views.guests import _apply_workspace_lineage

        rows = [self._row(100), self._row(101), self._row(200, ct=True)]
        with patch("core.views.common.stored_guest_lineage", return_value={}):
            ordered = _apply_workspace_lineage(rows)
        self.assertEqual([r.vmid for r in ordered], [100, 101, 200])
        self.assertTrue(all(r.depth == 0 and r.parent_vmid is None for r in ordered))

    def test_deep_chain_caps_depth_at_2_with_marker(self):
        from unittest.mock import patch

        from core.views.guests import _apply_workspace_lineage

        rows = [self._row(100), self._row(101), self._row(102), self._row(103)]  # 100→101→102→103
        with patch("core.views.common.stored_guest_lineage", return_value={101: 100, 102: 101, 103: 102}):
            ordered = _apply_workspace_lineage(rows)
        by = {r.vmid: r for r in ordered}
        self.assertEqual([by[v].depth for v in (100, 101, 102, 103)], [0, 1, 2, 2])
        self.assertTrue(by[103].deeper_chain)
        self.assertFalse(by[102].deeper_chain)

    def test_mark_linked_clones_flags_children_regardless_of_parent_presence(self):
        from unittest.mock import patch

        from core.views.guests import _mark_linked_clones

        # 101 is a linked clone; its parent template (100) is NOT in this view.
        rows = [self._row(101), self._row(102), self._row(200, ct=True)]
        with patch("core.views.common.stored_guest_lineage", return_value={101: 100}):
            _mark_linked_clones(rows)
        flags = {r.vmid: r.is_linked_clone for r in rows}
        self.assertEqual(flags, {101: True, 102: False, 200: False})

    def test_apply_lineage_also_sets_linked_clone_flag(self):
        from unittest.mock import patch

        from core.views.guests import _apply_workspace_lineage

        rows = [self._row(100, "template"), self._row(101), self._row(102)]
        with patch("core.views.common.stored_guest_lineage", return_value={101: 100}):
            ordered = _apply_workspace_lineage(rows)
        by = {r.vmid: r for r in ordered}
        self.assertTrue(by[101].is_linked_clone)
        self.assertFalse(by[100].is_linked_clone)

    def test_duplicate_vmids_keep_lineage_inside_their_cluster(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from core.views.guests import _apply_workspace_lineage

        cluster_a = SimpleNamespace(key="a")
        cluster_b = SimpleNamespace(key="b")
        rows = [
            self._row(500, "a-template", cluster=cluster_a),
            self._row(501, "a-clone", cluster=cluster_a),
            self._row(500, "b-vm", cluster=cluster_b),
            self._row(501, "b-vm-2", cluster=cluster_b),
        ]

        def lineage(cluster=None):
            return {501: 500} if cluster is cluster_a else {}

        with patch("core.views.common.stored_guest_lineage", side_effect=lineage):
            ordered = _apply_workspace_lineage(rows)

        self.assertEqual(len(ordered), 4)
        self.assertTrue(rows[1].is_linked_clone)
        self.assertEqual(rows[1].lineage_parent_name, "a-template")
        self.assertFalse(rows[3].is_linked_clone)
        self.assertEqual(rows[2].depth, 0)
        self.assertEqual(rows[3].depth, 0)


class LinkedCloneTemplateGuardTests(TestCase):
    """A linked clone must not be converted to a template (it would seed a
    fragile chained lineage). The action is gated server-side."""

    def test_template_action_rejects_linked_clone(self):
        from types import SimpleNamespace

        from core.models import ProxmoxInventory
        from core.services.refs import GuestRef
        from core.views import guests as G  # noqa: F401 (import path patched below)

        cluster = ProxmoxCluster.objects.create(key="default", display_name="Default", enabled=True)
        ref = GuestRef(cluster.key, "vm", 101, "pve3")
        detail = SimpleNamespace(
            cluster=cluster,
            cluster_key=cluster.key,
            guest_ref=ref,
            node_ref=ref.node_ref,
            object_type=ProxmoxInventory.ObjectType.VM,
            vmid=101,
            name="clone",
            node="pve3",
            config={},
        )
        self.client.force_login(get_user_model().objects.create_user("tester", password="x"))
        with (
            patch("core.views.guests.actions._require_guest", return_value=detail),
            patch("core.views.common.fetch_live_guest_lineage", return_value={101: 100}),
            patch("core.views.guests.actions._guest_post_with_client") as post,
        ):
            response = self.client.post(
                reverse("core:vms_bulk_action"),
                {"bulk_action": "template", "guest": ref.serialize()},
                HTTP_X_REQUESTED_WITH="fetch",
            )
        # The clone → template POST must never be issued.
        post.assert_not_called()
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertFalse(payload["ok"])
        self.assertTrue(any("linked clone" in e.lower() for e in payload["errors"]))


class LinkedCloneBaseProtectionTests(TestCase):
    """A template's base volume backs its linked clones read-only; removing or
    destroying it out from under them corrupts the clones. Both the raw-filesystem
    path (storage view) and the guest destroy are guarded."""

    def _entry(self, path, *, category="base_image", directory=False):
        from types import SimpleNamespace

        from core.models import FileInventory

        # An unbound mount: no cluster binding narrows the lineage lookup, so these
        # tests exercise the wide fallback. The cluster-scoped path has its own
        # tests in `FileBrowserClusterScopedGuestLinkTests`.
        mount, _ = StorageMount.objects.get_or_create(
            storage_id="base-vol-fs", defaults={"display_name": "Base Vol FS", "path": "/storages/base-vol-fs"}
        )
        return SimpleNamespace(
            path=path,
            content_category=category,
            entry_type=FileInventory.EntryType.DIRECTORY if directory else FileInventory.EntryType.FILE,
            storage_id=mount.pk,
        )

    def test_storage_gate_blocks_base_volume_with_clones(self):
        from core.services.storage_actions import StorageActionError
        from core.views.storage import _require_linked_clone_base_unblocked

        ProxmoxCluster.objects.create(key="default", display_name="Default", enabled=True)
        entry = self._entry("images/505/base-505-disk-0.qcow2")
        with patch("core.views.common.stored_guest_lineage", return_value={102: 505, 103: 505}):
            with self.assertRaises(StorageActionError) as ctx:
                _require_linked_clone_base_unblocked([entry])
        self.assertIn("2 linked clones", str(ctx.exception))

    def test_storage_gate_ignores_non_base_entries_without_live_call(self):
        # A plain directory is not a base volume; the guard must short-circuit
        # before any live lineage call (the empty-dir rule covers directories).
        from core.views.storage import _require_linked_clone_base_unblocked

        entry = self._entry("images/505", category="", directory=True)
        with patch("core.views.common.fetch_live_guest_lineage") as fetch:
            _require_linked_clone_base_unblocked([entry])  # no raise
        fetch.assert_not_called()

    def test_storage_gate_allows_base_volume_without_clones(self):
        from core.views.storage import _require_linked_clone_base_unblocked

        ProxmoxCluster.objects.create(key="default", display_name="Default", enabled=True)
        entry = self._entry("images/505/base-505-disk-0.qcow2")
        with patch("core.views.common.fetch_live_guest_lineage", return_value={}):
            _require_linked_clone_base_unblocked([entry])  # no raise

    def test_destroy_blocks_template_with_linked_clones(self):
        from types import SimpleNamespace

        from core.models import ProxmoxInventory
        from core.views.guests.actions import _destroy_guest_from_bulk_request

        cluster = ProxmoxCluster.objects.create(key="default", display_name="Default", enabled=True)
        ref = GuestRef(cluster.key, "vm", 505, "pve3")
        detail = SimpleNamespace(
            cluster=cluster,
            cluster_key=cluster.key,
            guest_ref=ref,
            node_ref=ref.node_ref,
            object_type=ProxmoxInventory.ObjectType.VM,
            vmid=505,
            name="tmpl",
            node="pve3",
            status="stopped",
        )
        request = RequestFactory().post("/", {"destroy_confirm_vmid": "505"})
        with patch("core.views.common.fetch_live_guest_lineage", return_value={102: 505, 103: 505}):
            err, details, response, client = _destroy_guest_from_bulk_request(request, detail)
        self.assertIn("linked clone", err.lower())
        self.assertIsNone(response)
        self.assertEqual(details["linked_children"], [102, 103])


class FileBrowserClusterScopedGuestLinkTests(TestCase):
    """A VMID names a guest only inside its own cluster.

    The file browser used to resolve disk owners and template lineage across every
    cluster at once, so two clusters that both happened to hold `vm:500` made each
    other's links disappear and each other's clone counts wrong. A mount is bound
    to the cluster that consumes it, so the answer is knowable; these tests hold it
    to that.
    """

    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(self.user)
        self.cluster_a = ProxmoxCluster.objects.create(key="clus-a", display_name="Cluster A", enabled=True)
        self.cluster_b = ProxmoxCluster.objects.create(key="clus-b", display_name="Cluster B", enabled=True)
        self.mount = StorageMount.objects.create(
            storage_id="shared-fs", display_name="Shared FS", path="/storages/shared-fs"
        )

    def _bind(self, cluster):
        definition = ClusterStorage.objects.create(
            cluster=cluster,
            storage_id=self.mount.storage_id,
            storage_type="dir",
            shared=True,
            present=True,
        )
        ClusterStorageMount.objects.create(
            cluster_storage=definition,
            mount=self.mount,
            node=None,
            scope=ClusterStorageMount.Scope.SHARED,
        )

    def _guest(self, cluster, name, *, vmid=500):
        CurrentGuestInventory.objects.create(
            cluster=cluster,
            object_type="vm",
            vmid=vmid,
            node="pve1",
            name=name,
            status="stopped",
            observed_at=timezone.now(),
        )

    def _scan_with(self, path, category, classification):
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        FileInventory.objects.create(
            scan_run=scan,
            storage=self.mount,
            path="images",
            entry_type=FileInventory.EntryType.DIRECTORY,
            content_category="unknown",
            classification=FileInventory.Classification.UNKNOWN,
        )
        FileInventory.objects.create(
            scan_run=scan,
            storage=self.mount,
            path="images/500",
            entry_type=FileInventory.EntryType.DIRECTORY,
            content_category="unknown",
            classification=FileInventory.Classification.UNKNOWN,
        )
        FileInventory.objects.create(
            scan_run=scan,
            storage=self.mount,
            path=path,
            entry_type=FileInventory.EntryType.FILE,
            content_category=category,
            classification=classification,
            size_bytes=1024,
        )
        return scan

    def _browse(self):
        from core.views.storage import _storage_browser_url

        return self.client.get(_storage_browser_url(self.mount, "images/500"))

    def _file_entry(self, response):
        """The decorated row for the disk image, read from the context.

        Asserting on the rendered page would be looser than it looks: a guest name
        also appears in the workspace chrome, so `assertContains` passes whether or
        not the row actually links anywhere.
        """
        self.assertEqual(response.status_code, 200)
        entries = [entry for entry in response.context["entries"] if entry.path.endswith(".qcow2")]
        self.assertEqual(len(entries), 1)
        return entries[0]

    def test_disk_links_to_the_owner_in_the_mount_s_own_cluster(self):
        self._bind(self.cluster_a)
        self._guest(self.cluster_a, "Owner in A")
        self._guest(self.cluster_b, "Unrelated in B")
        self._scan_with("images/500/vm-500-disk-0.qcow2", "vm_disk", FileInventory.Classification.REFERENCED)

        entry = self._file_entry(self._browse())

        self.assertIsNotNone(entry.referenced_guest)
        self.assertEqual(entry.referenced_guest["name"], "Owner in A")
        self.assertIn(self.cluster_a.key, entry.referenced_guest["url"])
        self.assertNotIn(self.cluster_b.key, entry.referenced_guest["url"])

    def test_clone_count_comes_from_the_owning_cluster_only(self):
        self._bind(self.cluster_a)
        self._guest(self.cluster_a, "Template in A")
        self._guest(self.cluster_b, "Template in B")
        self._scan_with("images/500/base-500-disk-0.qcow2", "base_image", FileInventory.Classification.REFERENCED)

        def lineage(cluster):
            # One clone in A, three unrelated clones in B. Merged, the badge would
            # claim four; scoped, it claims the one that is really there.
            return {501: 500} if cluster.key == self.cluster_a.key else {601: 500, 602: 500, 603: 500}

        with patch("core.views.common.stored_guest_lineage", side_effect=lineage):
            response = self._browse()

        entry = self._file_entry(response)
        self.assertEqual(entry.template_base["name"], "Template in A")
        self.assertEqual(entry.template_base["clone_count"], 1)
        self.assertContains(response, "1 linked clone")

    def test_a_genuinely_ambiguous_clone_count_says_so_instead_of_guessing(self):
        # The mount really is shared by both clusters and both hold a `vm:500`.
        # Nothing can tell them apart here, and a number would be a claim.
        self._bind(self.cluster_a)
        self._bind(self.cluster_b)
        self._guest(self.cluster_a, "Template in A")
        self._guest(self.cluster_b, "Template in B")
        self._scan_with("images/500/base-500-disk-0.qcow2", "base_image", FileInventory.Classification.REFERENCED)

        with patch("core.views.common.stored_guest_lineage", return_value={501: 500}):
            response = self._browse()

        self.assertIsNone(self._file_entry(response).template_base["clone_count"])
        self.assertContains(response, "linked clones unknown")

    def test_the_base_volume_block_ignores_clones_in_a_cluster_without_this_mount(self):
        from types import SimpleNamespace

        from core.views.storage import _require_linked_clone_base_unblocked

        self._bind(self.cluster_a)
        entry = SimpleNamespace(
            path="images/500/base-500-disk-0.qcow2",
            content_category="base_image",
            entry_type=FileInventory.EntryType.FILE,
            storage_id=self.mount.pk,
        )

        def lineage(cluster):
            # Only cluster B has clones of a `500`, and B cannot reach this mount,
            # so those clones cannot be riding this volume.
            return {} if cluster.key == self.cluster_a.key else {601: 500}

        with patch("core.views.common.stored_guest_lineage", side_effect=lineage):
            _require_linked_clone_base_unblocked([entry])  # no raise

    def test_the_base_volume_block_still_fires_for_a_cluster_that_has_the_mount(self):
        from types import SimpleNamespace

        from core.services.storage_actions import StorageActionError
        from core.views.storage import _require_linked_clone_base_unblocked

        self._bind(self.cluster_a)
        entry = SimpleNamespace(
            path="images/500/base-500-disk-0.qcow2",
            content_category="base_image",
            entry_type=FileInventory.EntryType.FILE,
            storage_id=self.mount.pk,
        )

        def lineage(cluster):
            return {501: 500, 502: 500} if cluster.key == self.cluster_a.key else {}

        with patch("core.views.common.stored_guest_lineage", side_effect=lineage):
            with self.assertRaises(StorageActionError) as ctx:
                _require_linked_clone_base_unblocked([entry])
        self.assertIn("2 linked clones", str(ctx.exception))


class LinkedCloneReferenceTests(SimpleTestCase):
    """A linked clone's compound volid must expand to the base and overlay volids
    so the overlay file classifies as REFERENCED, not an orphan."""

    def test_expand_compound_volid(self):
        from core.services.classification import expand_linked_clone_volid

        self.assertEqual(
            expand_linked_clone_volid("TrueNAS-FS:505/base-505-disk-0.qcow2/102/vm-102-disk-0.qcow2"),
            ["TrueNAS-FS:505/base-505-disk-0.qcow2", "TrueNAS-FS:102/vm-102-disk-0.qcow2"],
        )

    def test_plain_volid_unchanged(self):
        from core.services.classification import expand_linked_clone_volid

        self.assertEqual(
            expand_linked_clone_volid("TrueNAS-FS:100/vm-100-disk-0.qcow2"),
            ["TrueNAS-FS:100/vm-100-disk-0.qcow2"],
        )

    def test_extract_references_includes_clone_overlay(self):
        from core.services.classification import extract_disk_references

        refs = extract_disk_references(
            {"scsi0": "TrueNAS-FS:505/base-505-disk-0.qcow2/102/vm-102-disk-0.qcow2,size=40G"}
        )
        self.assertIn("TrueNAS-FS:102/vm-102-disk-0.qcow2", refs)
        self.assertIn("TrueNAS-FS:505/base-505-disk-0.qcow2", refs)


class LinkedCloneParentEditGuardTests(SimpleTestCase):
    """Disk removal/resize on a template whose base volume still backs clones is
    blocked; safe edits and childless templates pass."""

    def _detail(self):
        from types import SimpleNamespace

        from core.models import ProxmoxInventory

        cluster = SimpleNamespace(key="default")
        return SimpleNamespace(
            cluster=cluster,
            cluster_key=cluster.key,
            object_type=ProxmoxInventory.ObjectType.VM,
            vmid=505,
        )

    def test_blocks_disk_delete_with_children(self):
        from core.views.guests import _linked_clone_disk_edit_block

        with patch("core.views.common.fetch_live_guest_lineage", return_value={102: 505}):
            msg = _linked_clone_disk_edit_block(self._detail(), ["scsi1"], [])
        self.assertIsNotNone(msg)
        self.assertIn("remove", msg)
        self.assertIn("102", msg)

    def test_blocks_resize_with_children(self):
        from core.views.guests import _linked_clone_disk_edit_block

        with patch("core.views.common.fetch_live_guest_lineage", return_value={102: 505}):
            msg = _linked_clone_disk_edit_block(self._detail(), [], [("scsi0", "50G")])
        self.assertIn("resize", msg)

    def test_allows_non_disk_delete(self):
        from core.views.guests import _linked_clone_disk_edit_block

        with patch("core.views.common.fetch_live_guest_lineage", return_value={102: 505}):
            self.assertIsNone(_linked_clone_disk_edit_block(self._detail(), ["net0"], []))

    def test_allows_when_no_children(self):
        from core.views.guests import _linked_clone_disk_edit_block

        with patch("core.views.common.fetch_live_guest_lineage", return_value={}):
            self.assertIsNone(_linked_clone_disk_edit_block(self._detail(), ["scsi1"], []))


class DisplayDiskReferenceTests(SimpleTestCase):
    """A linked clone's disk references render as its own overlays annotated
    '(backed by base-<template>)', with the template's base volumes dropped."""

    def test_linked_clone_annotates_and_drops_base(self):
        from core.views.storage import _display_disk_references

        refs = _display_disk_references(
            102,
            [
                "TrueNAS-FS:102/vm-102-disk-0.qcow2",
                "TrueNAS-FS:505/base-505-disk-0.qcow2",
            ],
            {102: 505},
        )
        self.assertEqual(refs, [{"volid": "TrueNAS-FS:102/vm-102-disk-0.qcow2", "backed_by": "base-505"}])

    def test_non_clone_unchanged(self):
        from core.views.storage import _display_disk_references

        refs = _display_disk_references(100, ["TrueNAS-FS:100/vm-100-disk-0.qcow2"], {})
        self.assertEqual(refs, [{"volid": "TrueNAS-FS:100/vm-100-disk-0.qcow2", "backed_by": ""}])


class StorageRescanAfterCloneTests(TestCase):
    """A successful clone/destroy enqueues a scoped scan of the affected storage
    so new/removed disks reclassify at once instead of lingering as orphans."""

    def _storage(self):
        return StorageMount.objects.create(
            storage_id="TrueNAS-FS", display_name="TrueNAS-FS", path="/tmp/x", enabled=True
        )

    def test_enqueues_scoped_scan_for_affected_storage(self):
        from core.tasks import enqueue_storage_rescan

        storage = self._storage()
        with patch("core.tasks.async_task", return_value="task-1") as async_task:
            enqueue_storage_rescan(["TrueNAS-FS", "TrueNAS-FS", "unknown-storage"])
        scans = ScanRun.objects.filter(target_storage=storage)
        self.assertEqual(scans.count(), 1)  # deduped; unknown storage skipped
        async_task.assert_called_once_with(
            "core.tasks.run_scan",
            scans.first().id,
            q_options={"cluster": "bulk"},
        )

    def test_skips_when_scan_already_active(self):
        from core.tasks import enqueue_storage_rescan

        storage = self._storage()
        ScanRun.objects.create(target_storage=storage, status=ScanRun.Status.RUNNING)
        with patch("core.tasks.async_task") as async_task:
            enqueue_storage_rescan(["TrueNAS-FS"])
        async_task.assert_not_called()
        self.assertEqual(ScanRun.objects.filter(target_storage=storage, status=ScanRun.Status.QUEUED).count(), 0)


class ShutdownForceStopOfferTests(TestCase):
    """A timed-out graceful shutdown offers a force-stop follow-up on its task."""

    def _event(self, action, error):
        return AuditEvent.objects.create(
            action=action,
            object_type="guest",
            object_id="vm:106",
            outcome="failed",
            details={
                "target_type": "vm",
                "vmid": 106,
                "node": "pve3",
                "name": "Linux-Tinycore",
                "error": error,
            },
        )

    def _task(self, event):
        from core.services.recent_tasks import _guest_task, serialize_task

        return serialize_task(_guest_task(event))

    def test_timed_out_shutdown_offers_force_stop_while_running(self):
        event = self._event("guest.power.shutdown", "VM quit/powerdown failed - got timeout")
        task = self._task(event)
        self.assertTrue(task["offer_force_stop"])
        self.assertEqual(task["force_stop_target"], "vm:106@pve3")

    def test_resolves_to_completed_once_guest_stopped(self):
        event = self._event("guest.power.shutdown", "VM quit/powerdown failed - got timeout")
        details = dict(event.details)
        details["force_stop_resolved_at"] = timezone.now().isoformat()
        event.details = details
        event.save(update_fields=["details"])
        task = self._task(event)
        self.assertFalse(task["offer_force_stop"])
        self.assertEqual(task["status_class"], "completed")

    def test_offers_when_reconciliation_has_not_resolved_the_question(self):
        event = self._event("guest.power.shutdown", "got timeout")
        task = self._task(event)
        self.assertTrue(task["offer_force_stop"])

    def test_dismissed_question_resolves_even_if_running(self):
        event = self._event("guest.power.shutdown", "got timeout")
        details = dict(event.details)
        details["force_stop_dismissed"] = True
        event.details = details
        event.save(update_fields=["details"])
        task = self._task(event)
        self.assertFalse(task["offer_force_stop"])
        self.assertEqual(task["status_class"], "completed")

    def test_dismiss_endpoint_marks_event(self):
        event = self._event("guest.power.shutdown", "got timeout")
        self.client.force_login(get_user_model().objects.create_user("d", password="x"))
        response = self.client.post(
            reverse("core:dismiss_task_question"),
            {"task_id": f"guest:{event.id}"},
            HTTP_X_REQUESTED_WITH="fetch",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.content)["ok"])
        event.refresh_from_db()
        self.assertTrue(event.details["force_stop_dismissed"])

    def test_open_offer_survives_the_retention_window(self):
        from core.services.recent_tasks import _visible_guest_tasks

        event = self._event("guest.power.shutdown", "got timeout")
        AuditEvent.objects.filter(pk=event.pk).update(
            timestamp=timezone.now() - timedelta(minutes=RECENT_TASK_RETENTION_MINUTES + 5)
        )
        # Nothing has been dismissed, so the details hold neither flag. That is the
        # ordinary shape of an open question, and it must not be what evicts it.
        self.assertIn(event.id, [visible.id for visible in _visible_guest_tasks()])

        details = dict(event.details)
        details["force_stop_dismissed"] = True
        AuditEvent.objects.filter(pk=event.pk).update(details=details)
        self.assertNotIn(event.id, [visible.id for visible in _visible_guest_tasks()])

    def test_non_timeout_shutdown_failure_does_not_offer(self):
        event = self._event("guest.power.shutdown", "permission denied")
        task = self._task(event)
        self.assertFalse(task["offer_force_stop"])

    def test_other_actions_never_offer(self):
        event = self._event("guest.power.reboot", "got timeout")
        task = self._task(event)
        self.assertFalse(task["offer_force_stop"])


class GuestTaskReaperTests(TestCase):
    def setUp(self):
        self.cluster = ProxmoxCluster.objects.create(key="default", display_name="Default cluster", enabled=True)

    def _running_event(self, age_seconds: int):
        from datetime import timedelta

        from django.utils import timezone

        event = AuditEvent.objects.create(
            action="guest.migrate", object_type="vm", object_id="9990", outcome="running", details={"vmid": 9990}
        )
        AuditEvent.objects.filter(pk=event.pk).update(timestamp=timezone.now() - timedelta(seconds=age_seconds))
        return event

    def test_reaps_stale_running_event_without_upid(self):
        from core.tasks import STALE_GUEST_TASK_SECONDS, reap_stale_guest_tasks

        event = self._running_event(STALE_GUEST_TASK_SECONDS + 60)
        result = reap_stale_guest_tasks()
        event.refresh_from_db()
        self.assertEqual(event.outcome, "failed")
        self.assertEqual(result["reaped_dead"], 1)
        self.assertTrue((event.details or {}).get("reaped"))

    def test_leaves_recent_running_event_alone(self):
        from core.tasks import reap_stale_guest_tasks

        event = self._running_event(30)
        reap_stale_guest_tasks()
        event.refresh_from_db()
        self.assertEqual(event.outcome, "running")

    def test_reaper_schedule_is_ensured(self):
        from django_q.models import Schedule

        from core.services.guest_task_reaper_schedule import (
            GUEST_TASK_REAPER_SCHEDULE_NAME,
            ensure_guest_task_reaper_schedule,
        )

        ensure_guest_task_reaper_schedule()
        self.assertTrue(Schedule.objects.filter(name=GUEST_TASK_REAPER_SCHEDULE_NAME).exists())

    def test_reaper_resolves_force_stop_question_when_guest_is_already_stopped(self):
        event = AuditEvent.objects.create(
            cluster=self.cluster,
            action="guest.power.shutdown",
            outcome="failed",
            details={
                "guest_ref": GuestRef(self.cluster.key, "vm", 100, "pve1").serialize(),
                "target_type": "vm",
                "vmid": 100,
                "node": "pve1",
                "error": "powerdown failed - timeout",
            },
        )

        with patch("core.tasks.fetch_live_guest_status", return_value={("pve1", "vm", 100): "stopped"}):
            result = reap_stale_guest_tasks()

        event.refresh_from_db()
        self.assertEqual(result["resolved_force_stop_questions"], 1)
        self.assertEqual(event.details["force_stop_resolution"], "guest_stopped")
        self.assertIn("force_stop_resolved_at", event.details)


class BulkTaskReaperTests(TestCase):
    @override_settings(SCAN_TASK_TIMEOUT_SECONDS=60, STORAGE_INFLATE_TIMEOUT_SECONDS=60)
    def test_reaps_timed_out_scan_and_inflate_without_terminal_event(self):
        from core.tasks import STALE_BULK_TASK_GRACE_SECONDS, reap_stale_bulk_tasks

        now = timezone.now()
        expired_at = now - timedelta(seconds=61 + STALE_BULK_TASK_GRACE_SECONDS)
        scan = ScanRun.objects.create(status=ScanRun.Status.RUNNING, started_at=expired_at)
        queued = AuditEvent.objects.create(
            action="file.inflate_queued",
            object_type="file",
            object_id="nfs-vm:images/100/disk.qcow2",
            details={
                "storage_id": "nfs-vm",
                "path": "images/100/disk.qcow2",
                "target_preallocation": "full",
                "task_id": "bulk-task-1",
            },
        )
        AuditEvent.objects.filter(pk=queued.pk).update(timestamp=expired_at)

        result = reap_stale_bulk_tasks(now=now)

        self.assertEqual(result, {"scans_reaped": 1, "inflates_reaped": 1})
        scan.refresh_from_db()
        self.assertEqual(scan.status, ScanRun.Status.FAILED)
        self.assertTrue(scan.error_details["reaped"])
        failure = AuditEvent.objects.get(action="file.inflate_failed")
        self.assertTrue(failure.details["reaped"])
        self.assertEqual(failure.details["task_id"], "bulk-task-1")

    @override_settings(SCAN_TASK_TIMEOUT_SECONDS=60, STORAGE_INFLATE_TIMEOUT_SECONDS=60)
    def test_leaves_recent_or_already_terminal_bulk_work_alone(self):
        from core.tasks import reap_stale_bulk_tasks

        now = timezone.now()
        scan = ScanRun.objects.create(status=ScanRun.Status.RUNNING, started_at=now - timedelta(seconds=30))
        queued = AuditEvent.objects.create(
            action="file.inflate_queued",
            object_type="file",
            object_id="nfs-vm:images/100/disk.qcow2",
            details={"storage_id": "nfs-vm", "path": "images/100/disk.qcow2"},
        )
        AuditEvent.objects.create(
            action="file.inflated",
            object_type="file",
            object_id=queued.object_id,
            details={"storage_id": "nfs-vm", "path": "images/100/disk.qcow2"},
        )

        result = reap_stale_bulk_tasks(now=now)

        self.assertEqual(result, {"scans_reaped": 0, "inflates_reaped": 0})
        scan.refresh_from_db()
        self.assertEqual(scan.status, ScanRun.Status.RUNNING)

    def test_bulk_reaper_schedule_is_ensured(self):
        from core.services.bulk_task_reaper_schedule import (
            BULK_TASK_REAPER_SCHEDULE_NAME,
            ensure_bulk_task_reaper_schedule,
        )

        ensure_bulk_task_reaper_schedule()
        self.assertTrue(Schedule.objects.filter(name=BULK_TASK_REAPER_SCHEDULE_NAME).exists())


class ConsoleSessionLifecycleTests(TestCase):
    def _session(self, *, token_hash: str, expires_at=None, status=ConsoleSession.Status.PENDING, closed_at=None):
        return ConsoleSession.objects.create(
            token_hash=token_hash,
            target_type=ConsoleSession.TargetType.VM,
            target_vmid=100,
            target_node="pve1",
            expires_at=expires_at or timezone.now() + timedelta(seconds=30),
            status=status,
            closed_at=closed_at,
            proxmox_ticket="PVEVNC:secret-ticket",
            proxmox_password="secret-password",
        )

    def test_consuming_session_clears_database_credentials_but_keeps_handshake_copy(self):
        from asgiref.sync import async_to_sync

        from console_app.main import _consume_session
        from core.services.console_sessions import console_token_hash

        token = "one-time-console-token"
        session = self._session(token_hash=console_token_hash(token))

        consumed = async_to_sync(_consume_session)(token)

        self.assertIsNotNone(consumed)
        self.assertEqual(consumed.proxmox_ticket, "PVEVNC:secret-ticket")
        self.assertEqual(consumed.proxmox_password, "secret-password")
        self.assertEqual(consumed.status, ConsoleSession.Status.CONNECTING)
        session.refresh_from_db()
        self.assertEqual(session.proxmox_ticket, "")
        self.assertEqual(session.proxmox_password, "")
        self.assertEqual(session.status, ConsoleSession.Status.CONNECTING)

    def test_pruning_expires_unconsumed_sessions_and_deletes_old_terminal_rows(self):
        from core.services.console_session_cleanup import prune_console_sessions

        now = timezone.now()
        expired = self._session(token_hash="1" * 64, expires_at=now - timedelta(seconds=1))
        old_closed = self._session(
            token_hash="2" * 64,
            status=ConsoleSession.Status.CLOSED,
            closed_at=now - timedelta(hours=25),
        )
        recent_closed = self._session(
            token_hash="3" * 64,
            status=ConsoleSession.Status.CLOSED,
            closed_at=now - timedelta(hours=1),
        )

        result = prune_console_sessions(now=now, retention_hours=24)

        self.assertEqual(result["expired"], 1)
        self.assertEqual(result["deleted"], 1)
        expired.refresh_from_db()
        self.assertEqual(expired.status, ConsoleSession.Status.EXPIRED)
        self.assertEqual(expired.proxmox_ticket, "")
        self.assertEqual(expired.proxmox_password, "")
        self.assertFalse(ConsoleSession.objects.filter(pk=old_closed.pk).exists())
        self.assertTrue(ConsoleSession.objects.filter(pk=recent_closed.pk).exists())

    def test_cleanup_schedule_is_ensured(self):
        from core.services.console_session_cleanup_schedule import (
            CONSOLE_SESSION_CLEANUP_SCHEDULE_NAME,
            ensure_console_session_cleanup_schedule,
        )

        ensure_console_session_cleanup_schedule()
        self.assertTrue(Schedule.objects.filter(name=CONSOLE_SESSION_CLEANUP_SCHEDULE_NAME).exists())


class RequestMetadataTests(SimpleTestCase):
    def test_prefers_trusted_real_ip_over_client_supplied_xff(self):
        from core.services.request_metadata import client_ip

        request = RequestFactory().get(
            "/",
            REMOTE_ADDR="192.0.2.10",
            HTTP_X_REAL_IP="198.51.100.42",
            HTTP_X_FORWARDED_FOR="forged-address, 198.51.100.42",
        )

        self.assertEqual(client_ip(request), "198.51.100.42")

    def test_falls_back_to_remote_address_and_rejects_invalid_values(self):
        from core.services.request_metadata import client_ip

        fallback = RequestFactory().get("/", REMOTE_ADDR="2001:db8::42")
        invalid = RequestFactory().get("/", REMOTE_ADDR="not-an-ip", HTTP_X_REAL_IP="also-not-an-ip")

        self.assertEqual(client_ip(fallback), "2001:db8::42")
        self.assertIsNone(client_ip(invalid))


class TaskQueueConfigurationTests(SimpleTestCase):
    def test_bulk_cluster_cannot_run_schedules_and_retries_after_its_timeout(self):
        bulk = settings.Q_CLUSTER["ALT_CLUSTERS"]["bulk"]

        self.assertFalse(bulk["scheduler"])
        self.assertGreater(bulk["retry"], bulk["timeout"])


class TrashDirectoryConventionTests(TestCase):
    """One convention, enforced at every writer.

    A mount whose trash is spelled differently is not a cosmetic inconsistency:
    its trashed files are classified `app_internal` instead of `trash`, so they
    vanish from the Trash view while still occupying the storage.
    """

    def test_every_writer_derives_the_same_trash_path(self):
        from core.services.storage_paths import default_trash_relative_path

        self.assertEqual(default_trash_relative_path("nas-files"), "nas-files/.trash/pve-helper")
        self.assertEqual(categorize_proxmox_path(".trash/pve-helper/20260101T000000Z/x.iso"), "trash")

    def test_legacy_trash_directory_still_classifies_as_trash(self):
        # Until a mount is migrated its files must not be labelled Infrastructure.
        legacy = ".pve-helper-trash/20260101T000000Z/x.iso"
        self.assertEqual(categorize_proxmox_path(legacy), "trash")
        self.assertEqual(categorize_proxmox_path(".pve-helper-upload-tmp/x.part"), "app_internal")

        # classify_entry re-checks the raw prefix itself, so it needs the same
        # precedence: the category alone does not save it.
        def classify(relative_path, content_category):
            return classify_entry(
                relative_path=relative_path,
                entry_type=FileInventory.EntryType.FILE,
                content_category=content_category,
                derived_volid="",
                referenced_volids=set(),
                template_vmids=set(),
                gate_ok=True,
                missing_consumers=[],
            ).classification

        self.assertEqual(classify(legacy, "trash"), FileInventory.Classification.TRASH)
        self.assertEqual(
            classify(".pve-helper-upload-tmp/x.part", "app_internal"),
            FileInventory.Classification.INFRASTRUCTURE,
        )

    def _run_migration(self, root):
        from importlib import import_module

        # The module name starts with a digit, so it cannot be imported by name.
        migration = import_module("core.migrations.0022_canonical_trash_directory")
        with override_settings(PVE_HELPER_STORAGE_CONTAINER_ROOT=Path(root)):
            migration._adopt_canonical_trash_directory(apps, None)

    def _legacy_mount(self):
        return StorageMount.objects.create(
            storage_id="legacy-fs",
            display_name="Legacy FS",
            path="/storages/legacy-fs",
            relative_path="legacy-fs",
            trash_path="/storages/legacy-fs/.pve-helper-trash",
            trash_relative_path="legacy-fs/.pve-helper-trash",
        )

    def test_migration_moves_the_directory_the_row_and_the_trash_items_together(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount = self._legacy_mount()
            trashed = root / "legacy-fs" / ".pve-helper-trash" / "20260101T000000Z" / "old.iso"
            trashed.parent.mkdir(parents=True)
            trashed.write_bytes(b"trashed")
            item = TrashItem.objects.create(
                original_path="/storages/legacy-fs/template/iso/old.iso",
                trash_path=str(trashed),
                mount=mount,
            )

            self._run_migration(root)

            mount.refresh_from_db()
            item.refresh_from_db()
            moved = root / "legacy-fs" / ".trash" / "pve-helper" / "20260101T000000Z" / "old.iso"
            self.assertEqual(mount.trash_relative_path, "legacy-fs/.trash/pve-helper")
            self.assertEqual(mount.trash_path, f"{root}/legacy-fs/.trash/pve-helper")
            self.assertEqual(item.trash_path, str(moved))
            self.assertEqual(moved.read_bytes(), b"trashed")
            self.assertFalse((root / "legacy-fs" / ".pve-helper-trash").exists())

    def test_migration_rewrites_the_row_when_nothing_was_ever_trashed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "legacy-fs").mkdir()
            mount = self._legacy_mount()

            self._run_migration(root)

            mount.refresh_from_db()
            self.assertEqual(mount.trash_relative_path, "legacy-fs/.trash/pve-helper")

    def test_migration_leaves_the_row_alone_when_the_storage_is_not_mounted(self):
        # An unmounted storage looks exactly like an empty one. Rewriting the row
        # here would point the app at a directory the trashed files are not in.
        with TemporaryDirectory() as tmp:
            mount = self._legacy_mount()

            with self.assertLogs("core.migrations.0022_canonical_trash_directory", level="WARNING") as logs:
                self._run_migration(Path(tmp))

            mount.refresh_from_db()
            self.assertEqual(mount.trash_relative_path, "legacy-fs/.pve-helper-trash")
            self.assertIn("legacy-fs", logs.output[0])


class StorageFoundationBackfillRemediationTests(TestCase):
    """Migration 0018 refuses to guess, so its message is the whole handover.

    Whoever sees it is mid-upgrade with a database between two releases. A bare
    primary key tells them nothing about which table to open or what a valid
    resolution looks like, so every abort names the conflicting values and the
    runbook section that explains them.
    """

    RUNBOOK_HEADING = "Upgrade halted by the storage foundation backfill"

    def _run_backfill(self):
        from importlib import import_module

        migration = import_module("core.migrations.0018_storage_catalog_foundation")
        migration.backfill_storage_foundation(apps, None)

    def _mount(self, storage_id="truenas-fs"):
        return StorageMount.objects.create(
            storage_id=storage_id,
            display_name=storage_id,
            export=f"nas:/mnt/{storage_id}",
            path=f"/storages/{storage_id}",
        )

    def test_the_runbook_section_the_messages_point_at_exists(self):
        runbook = (Path(settings.BASE_DIR) / "docs" / "deployment-runbook.md").read_text(encoding="utf-8")

        self.assertIn(f"## {self.RUNBOOK_HEADING}", runbook)

    def test_unattributable_trash_item_names_its_storage_id_and_the_runbook(self):
        self._mount()
        TrashItem.objects.create(
            original_path="/storages/gone/template/iso/old.iso",
            trash_path="/storages/gone/.trash/pve-helper/20260101T000000Z/old.iso",
            storage_id="storage-that-was-removed",
        )

        with self.assertRaises(RuntimeError) as raised:
            self._run_backfill()

        message = str(raised.exception)
        self.assertIn("cannot be attributed to a unique storage mount", message)
        self.assertIn("storage-that-was-removed", message)
        self.assertIn(self.RUNBOOK_HEADING, message)
        self.assertIn("deployment-runbook.md", message)

    def test_unattributable_consumer_names_its_storage_and_the_runbook(self):
        cluster = ProxmoxCluster.objects.create(key="cluster-0018", display_name="Cluster 0018")
        ProxmoxStorageConsumer.objects.create(
            storage=self._mount("never-scanned"),
            cluster=cluster,
            expected_node_name="pve1",
        )

        with self.assertRaises(RuntimeError) as raised:
            self._run_backfill()

        message = str(raised.exception)
        self.assertIn("cannot be attributed to a cluster storage definition", message)
        self.assertIn("never-scanned", message)
        self.assertIn(self.RUNBOOK_HEADING, message)

    def test_conflicting_consumers_name_both_mounts_and_the_runbook(self):
        cluster = ProxmoxCluster.objects.create(key="cluster-0018", display_name="Cluster 0018")
        first = self._mount("shared-a")
        second = self._mount("shared-b")
        # Both host mounts claim the same shared cluster storage; the catalog can
        # hold one, so the backfill must stop rather than pick.
        for storage_id in ("shared-a", "shared-b"):
            ClusterStorage.objects.create(
                cluster=cluster,
                storage_id=storage_id,
                storage_type="nfs",
                shared=True,
                present=True,
            )
        for mount in (first, second):
            ProxmoxStorageConsumer.objects.create(storage=mount, cluster=cluster, expected_node_name="pve1")
        ClusterStorageMount.objects.create(
            cluster_storage=ClusterStorage.objects.get(cluster=cluster, storage_id="shared-a"),
            node=None,
            mount=second,
            scope="shared",
        )

        with self.assertRaises(RuntimeError) as raised:
            self._run_backfill()

        message = str(raised.exception)
        self.assertIn("map one cluster storage scope to multiple mounts", message)
        self.assertIn("shared-a", message)
        self.assertIn(f"mount {second.pk}", message)
        self.assertIn(self.RUNBOOK_HEADING, message)


class ClassificationTests(SimpleTestCase):
    def test_extracts_disk_references_from_nested_snapshot_config(self):
        config = {
            "scsi0": "nfs-vm:100/vm-100-disk-0.qcow2,size=32G",
            "ide2": "none,media=cdrom",
            "snapshots": {
                "before-upgrade": {
                    "scsi0": "nfs-vm:100/vm-100-disk-0.qcow2,size=32G",
                    "unused0": "nfs-vm:100/vm-100-disk-1.qcow2",
                }
            },
        }

        references = extract_disk_references(config)

        self.assertEqual(
            references,
            [
                "nfs-vm:100/vm-100-disk-0.qcow2",
                "nfs-vm:100/vm-100-disk-1.qcow2",
            ],
        )

    def test_unreferenced_vm_disk_is_blocked_when_gate_is_not_ok(self):
        result = classify_entry(
            relative_path="images/100/vm-100-disk-0.qcow2",
            entry_type=FileInventory.EntryType.FILE,
            content_category="vm_disk",
            derived_volid="nfs-vm:100/vm-100-disk-0.qcow2",
            referenced_volids=set(),
            template_vmids=set(),
            gate_ok=False,
            missing_consumers=["pve-node-1"],
        )

        self.assertEqual(result.classification, FileInventory.Classification.CLASSIFICATION_BLOCKED)

    def test_base_image_is_never_likely_orphan_in_v1(self):
        result = classify_entry(
            relative_path="images/900/base-900-disk-0.qcow2",
            entry_type=FileInventory.EntryType.FILE,
            content_category="base_image",
            derived_volid="nfs-vm:900/base-900-disk-0.qcow2",
            referenced_volids=set(),
            template_vmids=set(),
            gate_ok=True,
            missing_consumers=[],
        )

        self.assertEqual(result.classification, FileInventory.Classification.UNKNOWN)

    def test_categorizes_proxmox_image_directories(self):
        self.assertEqual(categorize_proxmox_path("images"), "vm_images")
        self.assertEqual(categorize_proxmox_path("images/500"), "vm_image_directory")

    def test_categorizes_import_content_and_app_internal_paths(self):
        self.assertEqual(categorize_proxmox_path("import"), "import_directory")
        self.assertEqual(categorize_proxmox_path("import/disk.qcow2"), "import_content")
        self.assertEqual(categorize_proxmox_path(".pve-helper-upload-tmp"), "app_internal")
        self.assertEqual(categorize_proxmox_path(".pve-helper-upload-tmp/x.part"), "app_internal")

    def _classify(self, **overrides):
        params = dict(
            relative_path="",
            entry_type=FileInventory.EntryType.FILE,
            content_category="unknown",
            derived_volid="",
            referenced_volids=set(),
            template_vmids=set(),
            gate_ok=True,
            missing_consumers=[],
        )
        params.update(overrides)
        return classify_entry(**params)

    def test_import_content_directory_is_infrastructure(self):
        result = self._classify(
            relative_path="import",
            entry_type=FileInventory.EntryType.DIRECTORY,
            content_category="import_directory",
        )
        self.assertEqual(result.classification, FileInventory.Classification.INFRASTRUCTURE)

    def test_import_content_file_is_proxmox_content(self):
        result = self._classify(relative_path="import/disk.qcow2", content_category="import_content")
        self.assertEqual(result.classification, FileInventory.Classification.PROXMOX_CONTENT)

    def test_app_internal_directory_is_infrastructure_not_unknown(self):
        result = self._classify(
            relative_path=".pve-helper-upload-tmp",
            entry_type=FileInventory.EntryType.DIRECTORY,
            content_category="app_internal",
        )
        self.assertEqual(result.classification, FileInventory.Classification.INFRASTRUCTURE)

    def test_loose_disk_image_is_a_recognized_import_source_not_an_orphan(self):
        result = self._classify(
            relative_path="images/cirros-0.6.2-x86_64-disk.img",
            content_category="vm_image_directory",
        )
        self.assertEqual(result.classification, FileInventory.Classification.IMPORT_SOURCE)
        self.assertIn("importable disk image", result.reason)

    def test_ova_ovf_and_manifest_are_recognized_import_sources(self):
        for path, category in (
            ("template/iso/appliance.ova", "import_package"),
            ("template/iso/appliance.ovf", "import_package"),
            ("template/iso/appliance.mf", "import_manifest"),
            ("template/iso/appliance-disk1.vmdk", "import_disk"),
        ):
            result = self._classify(relative_path=path, content_category=category)
            self.assertEqual(result.classification, FileInventory.Classification.IMPORT_SOURCE)

    def test_real_vm_disk_volume_without_reference_is_orphan(self):
        result = self._classify(
            relative_path="images/100/vm-100-disk-0.qcow2",
            content_category="vm_disk",
            derived_volid="nfs-vm:100/vm-100-disk-0.qcow2",
        )
        self.assertEqual(result.classification, FileInventory.Classification.LIKELY_ORPHAN)

    def test_foreign_file_is_unknown_and_flagged_as_not_belonging(self):
        result = self._classify(
            relative_path="images/backdoor.sh",
            content_category="vm_image_directory",
        )
        self.assertEqual(result.classification, FileInventory.Classification.UNKNOWN)
        self.assertIn("does not belong", result.reason)

    def test_stray_txt_in_iso_folder_is_not_proxmox_content(self):
        result = self._classify(relative_path="template/iso/junk.txt", content_category="iso")
        self.assertEqual(result.classification, FileInventory.Classification.UNKNOWN)
        self.assertIn("misplaced", result.reason)

    def test_real_iso_in_iso_folder_is_proxmox_content(self):
        result = self._classify(relative_path="template/iso/ubuntu-24.04.iso", content_category="iso")
        self.assertEqual(result.classification, FileInventory.Classification.PROXMOX_CONTENT)

    def test_ct_template_tarball_is_content_but_stray_file_is_not(self):
        good = self._classify(relative_path="template/cache/debian-12.tar.zst", content_category="ct_template")
        self.assertEqual(good.classification, FileInventory.Classification.PROXMOX_CONTENT)
        stray = self._classify(relative_path="template/cache/readme.md", content_category="ct_template")
        self.assertEqual(stray.classification, FileInventory.Classification.UNKNOWN)

    def test_storage_scanner_records_permission_errors_without_raising(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            readable = root / "dump"
            blocked = root / "images" / "500"
            readable.mkdir()
            blocked.mkdir(parents=True)
            blocked.chmod(0)

            try:
                scanner = StorageScanner("nfs-vm", root.as_posix())
                entries = list(scanner.iter_entries())
            finally:
                blocked.chmod(0o700)

        self.assertIn("images/500", {entry.relative_path for entry in entries})
        self.assertEqual(scanner.errors[0]["path"], "images/500")
        self.assertEqual(scanner.errors[0]["error"], "PermissionError")

    def test_storage_space_info_reads_capacity_for_existing_path(self):
        with TemporaryDirectory() as tmp:
            storage = StorageMount(
                storage_id="space-info",
                display_name="space-info",
                path=tmp,
            )
            info = storage_space_info(storage)

        self.assertTrue(info.ok)
        self.assertGreater(info.total_bytes or 0, 0)
        self.assertGreaterEqual(info.available_bytes or 0, 0)

    def test_mount_access_mode_prefers_read_only_when_present(self):
        mount = MountInfo(mount_options="rw,noatime", super_options="ro,vers=4.2")

        self.assertEqual(mount_access_mode(mount), "read_only")


class ScannerToInventoryPathTests(TestCase):
    """The two scan paths that write `FileInventory` rows from a live filesystem.

    Both translate a `StorageEntry` into a row, and the row's `path` column holds the
    path relative to the mount root — the same convention every later consumer reads,
    from the browser's directory prefixes to `extract_vmid_from_image_path`. A row
    written with an absolute path would look plausible in the database and break
    silently downstream, so the translation is asserted here rather than assumed. The
    periodic scan in `core.tasks` already had coverage; these two did not, and the
    preflight is patched out in the tests that exercise its caller.
    """

    def _mount(self, root: Path) -> StorageMount:
        return StorageMount.objects.create(
            storage_id="scan-fs",
            display_name="Scan FS",
            path=root.as_posix(),
            expected_consumers=["pve-node-1"],
        )

    def _tree(self, root: Path) -> None:
        (root / "images" / "500").mkdir(parents=True)
        (root / "images" / "500" / "vm-500-disk-0.qcow2").write_bytes(b"disk")

    def test_the_preflight_scan_records_paths_relative_to_the_mount_root(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._tree(root)
            storage = self._mount(root)

            from core.views.storage import _run_storage_content_preflight_scan

            scan = _run_storage_content_preflight_scan(storage)

        self.assertEqual(scan.status, ScanRun.Status.COMPLETED)
        self.assertEqual(
            set(FileInventory.objects.filter(scan_run=scan).values_list("path", flat=True)),
            {"images", "images/500", "images/500/vm-500-disk-0.qcow2"},
        )

    def test_the_directory_refresh_records_paths_relative_to_the_mount_root(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._tree(root)
            storage = self._mount(root)
            scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)

            from core.services.partial_scan import refresh_storage_directory

            refresh_storage_directory(storage=storage, scan=scan, directory_path="images/500")

        rows = FileInventory.objects.filter(scan_run=scan)
        self.assertEqual([row.path for row in rows], ["images/500/vm-500-disk-0.qcow2"])
        self.assertEqual(rows[0].evidence["full_path"], (root / "images/500/vm-500-disk-0.qcow2").as_posix())


@override_settings(
    PVE_ENDPOINTS=["https://pve-node-1.example.com:8006"],
)
class RuntimeConfigurationTests(TestCase):
    def test_empty_endpoint_environment_records_bootstrap_without_inventing_a_cluster(self):
        with self.settings(PVE_ENDPOINTS=[]):
            first = ensure_bootstrap()
            second = ensure_bootstrap()

        self.assertTrue(first.bootstrap_completed)
        self.assertEqual(first.pk, second.pk)
        self.assertFalse(ProxmoxCluster.objects.exists())
        self.assertFalse(ProxmoxEndpoint.objects.exists())
        self.assertFalse(StorageMount.objects.exists())

    def test_bootstrap_imports_environment_into_default_cluster(self):
        state = ensure_bootstrap()

        self.assertTrue(state.bootstrap_completed)
        self.assertTrue(state.bootstrap_fingerprint)
        self.assertEqual(state.identity_contract_version, 0)

        cluster = ProxmoxCluster.objects.get(key="default")
        self.assertEqual(ProxmoxEndpoint.objects.get(name="pve-node-1").url, "https://pve-node-1.example.com:8006")
        self.assertEqual(ProxmoxEndpoint.objects.get(name="pve-node-1").cluster, cluster)

    def test_bootstrap_does_not_reapply_environment_after_marker_exists(self):
        ensure_bootstrap()

        endpoint = ProxmoxEndpoint.objects.get(name="pve-node-1")
        endpoint.url = "https://operator-chosen.example.com:8006"
        endpoint.save(update_fields=["url"])
        # A later environment change must not mutate DB-owned configuration: the
        # database is the sole runtime authority once the marker exists.
        with self.settings(PVE_ENDPOINTS=["https://env-changed.example.com:8006"]):
            ensure_bootstrap()

        endpoint.refresh_from_db()
        self.assertEqual(endpoint.url, "https://operator-chosen.example.com:8006")
        self.assertFalse(ProxmoxEndpoint.objects.filter(name="env-changed").exists())

    def test_bootstrap_is_idempotent_and_keeps_one_default_cluster(self):
        first = ensure_bootstrap()
        second = ensure_bootstrap()

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ProxmoxCluster.objects.count(), 1)
        self.assertEqual(RuntimeConfigurationState.objects.count(), 1)
        self.assertEqual(ProxmoxEndpoint.objects.filter(name="pve-node-1").count(), 1)

    def test_emptied_configuration_is_not_silently_reimported_from_environment(self):
        ensure_bootstrap()
        # Clusters are PROTECTed by the records that reference them, so emptying
        # configuration means removing those first — as an explicit removal flow would.
        ProxmoxStorageConsumer.objects.all().delete()
        ProxmoxEndpoint.objects.all().delete()
        ProxmoxCluster.objects.all().delete()

        # The marker survives deletion of cluster records, so an installation an
        # operator deliberately emptied stays empty.
        ensure_bootstrap()

        self.assertFalse(ProxmoxCluster.objects.exists())
        self.assertFalse(ProxmoxEndpoint.objects.exists())

    def test_storage_details_normalizes_pve_options_order(self):
        storage = StorageMount.objects.create(
            storage_id="nfs-vm",
            display_name="nfs-vm",
            export="truenas.example.com:/mnt/tank/proxmox-vm",
            path="/storages/truenas-vm",
        )
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED, storage_gate_status={"nfs-vm": {"ok": True}})
        ProxmoxInventory.objects.create(
            scan_run=scan,
            node="pve-node-1",
            object_type=ProxmoxInventory.ObjectType.STORAGE,
            name="nfs-vm",
            config={"storage": "nfs-vm", "options": "nconnect=4,vers=4.2"},
        )

        details = storage_details(storage, scan, StorageSpaceInfo(ok=False))

        self.assertEqual(details.options, "vers=4.2,nconnect=4")


class ClusterActivationInvariantTests(TestCase):
    """More than one enabled cluster is permitted only once every read, write, URL
    and payload boundary is cluster-qualified. Until then the app must refuse it."""

    def setUp(self):
        super().setUp()
        ensure_bootstrap()
        self.default = ProxmoxCluster.objects.get(key="default")

    def test_second_cluster_cannot_be_enabled_at_contract_version_zero(self):
        second = ProxmoxCluster.objects.create(key="lab", display_name="Lab", enabled=False)

        with self.assertRaises(ClusterActivationError):
            enable_cluster(second)

        second.refresh_from_db()
        self.assertFalse(second.enabled)

    def test_database_allows_multiple_enabled_clusters_after_url_activation_migration(self):
        # Phase 4 removes the old partial unique constraint. The service still
        # refuses this at contract version zero; after activation the database must
        # be able to persist several enabled clusters.
        RuntimeConfigurationState.objects.filter(pk=RuntimeConfigurationState.SINGLETON_PK).update(
            identity_contract_version=1
        )
        second = ProxmoxCluster.objects.create(key="lab", display_name="Lab", enabled=True)
        self.assertTrue(second.enabled)

    def test_enabling_the_sole_cluster_is_allowed(self):
        self.default.enabled = False
        self.default.save(update_fields=["enabled"])

        enable_cluster(self.default)

        self.default.refresh_from_db()
        self.assertTrue(self.default.enabled)

    def test_cluster_key_is_case_insensitively_unique(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProxmoxCluster.objects.create(key="DEFAULT", display_name="Shouting", enabled=False)

    def test_initial_cluster_key_can_be_chosen_before_activation(self):
        cluster = set_initial_cluster_key("lab")

        self.assertEqual(cluster.key, "lab")
        self.assertEqual(ProxmoxCluster.objects.get(pk=self.default.pk).key, "lab")

    def test_initial_cluster_key_rejects_invalid_keys(self):
        for invalid in ["with space", "-leading", "under_score", "", "a" * 64]:
            with self.subTest(key=invalid), self.assertRaises((ValidationError, ClusterActivationError)):
                set_initial_cluster_key(invalid)

        self.assertEqual(ProxmoxCluster.objects.get(pk=self.default.pk).key, "default")

    def test_initial_cluster_key_is_normalized_rather_than_rejected(self):
        # Operators type what looks natural; the stored key is the canonical form.
        cluster = set_initial_cluster_key("  LAB  ")

        self.assertEqual(cluster.key, "lab")

    def test_key_can_be_chosen_after_a_second_cluster_is_registered(self):
        # The guard is contract version 0, not cluster count: version 0 is the
        # statement that no durable cluster-qualified payload exists yet. Gating on
        # "only one cluster" locked out a safe rename as soon as cluster two was
        # registered, which is easy to do before choosing the first one's key.
        ProxmoxCluster.objects.create(key="clusterb", display_name="B", enabled=False)

        cluster = set_initial_cluster_key("clusterhq", current_key="default")

        self.assertEqual(cluster.key, "clusterhq")
        self.assertEqual(ProxmoxCluster.objects.get(pk=self.default.pk).key, "clusterhq")

    def test_rekeying_several_clusters_requires_naming_one(self):
        ProxmoxCluster.objects.create(key="clusterb", display_name="B", enabled=False)

        with self.assertRaises(ClusterActivationError) as ctx:
            set_initial_cluster_key("clusterhq")

        self.assertIn("clusterb", str(ctx.exception))

    def test_rekeying_an_unknown_cluster_is_refused(self):
        with self.assertRaises(ClusterActivationError):
            set_initial_cluster_key("clusterhq", current_key="nope")

    def test_rekeying_cannot_collide_with_another_clusters_key(self):
        ProxmoxCluster.objects.create(key="clusterb", display_name="B", enabled=False)

        with self.assertRaises(ClusterActivationError):
            set_initial_cluster_key("clusterb", current_key="default")

    def test_initial_cluster_key_is_immutable_once_contract_is_active(self):
        RuntimeConfigurationState.objects.filter(pk=RuntimeConfigurationState.SINGLETON_PK).update(
            identity_contract_version=1
        )

        with self.assertRaises(ClusterActivationError):
            set_initial_cluster_key("lab")

        self.assertEqual(ProxmoxCluster.objects.get(pk=self.default.pk).key, "default")


class StorageGateClusterIdentityTests(TestCase):
    """The storage gate governs destructive file operations, so its evidence must be
    cluster-qualified: two clusters may each contain a node named `pve1`."""

    def setUp(self):
        super().setUp()
        self.cluster_a = ProxmoxCluster.objects.create(key="a", display_name="Cluster A", enabled=True)
        # Cluster two cannot be enabled before activation; it may still be configured.
        self.cluster_b = ProxmoxCluster.objects.create(key="b", display_name="Cluster B", enabled=False)
        self.storage = StorageMount.objects.create(
            storage_id="nfs-vm",
            display_name="nfs-vm",
            export="truenas.example.com:/mnt/tank/proxmox-vm",
            path="/storages/truenas-vm",
            expected_consumers=["pve1"],
        )
        self.now = timezone.now()

    def _consumer(self, cluster, node="pve1"):
        return ProxmoxStorageConsumer.objects.create(storage=self.storage, cluster=cluster, expected_node_name=node)

    def test_cluster_a_success_does_not_satisfy_cluster_b_gate(self):
        self._consumer(self.cluster_a)
        consumer_b = self._consumer(self.cluster_b)

        # Only cluster A's pve1 was inventoried successfully.
        status = _storage_gate_status([self.storage], {self.cluster_a.pk: {"pve1"}}, self.now)["nfs-vm"]

        self.assertFalse(status["ok"])
        self.assertEqual(status["status"], "inventory incomplete")
        self.assertIn("nr1:b:pve1", status["missing_node_refs"])
        self.assertNotIn("nr1:a:pve1", status["missing_node_refs"])

        consumer_b.refresh_from_db()
        self.assertEqual(consumer_b.last_gate_status, "unavailable")
        self.assertIsNone(consumer_b.last_successful_inventory_scan)

    def test_another_cluster_being_uncovered_is_reported_as_an_unverified_node(self):
        self._consumer(self.cluster_a)
        self._consumer(self.cluster_b)

        gate = _storage_gate_status([self.storage], {self.cluster_a.pk: {"pve1"}}, self.now)
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED, storage_gate_status=gate)
        entry = FileInventory.objects.create(
            scan_run=scan,
            storage=self.storage,
            path="images/100/vm-100-disk-0.qcow2",
            size_bytes=1,
            classification=FileInventory.Classification.REFERENCED,
        )

        # Naming the node is the point: an operator deciding whether to act on an
        # unverifiable file needs to know *which* consumer is missing, and the
        # answer is no longer a refusal, so the name is what they act on.
        self.assertEqual(_unverified_consumers(entry), ("pve1",))

    def test_unreachable_coverage_asks_instead_of_locking_the_operator_out(self):
        """ "Unknown" must not become "forbidden".

        A node being unreachable is precisely when an operator needs to move or
        delete the disk of a guest that is not coming back. Refusing there makes
        the tool useless in the only situation it was reached for, so incomplete
        coverage asks a second question and names the node it could not reach.
        """
        self._consumer(self.cluster_a)
        self._consumer(self.cluster_b)

        gate = _storage_gate_status([self.storage], {self.cluster_a.pk: {"pve1"}}, self.now)
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED, storage_gate_status=gate)
        entry = FileInventory.objects.create(
            scan_run=scan,
            storage=self.storage,
            path="images/100/vm-100-disk-0.qcow2",
            size_bytes=1,
            content_category="vm_disk",
            classification=FileInventory.Classification.REFERENCED,
        )

        risk = file_action_risk(entry)

        self.assertFalse(risk.blocked)
        self.assertTrue(risk.requires_extra_confirmation)
        self.assertEqual(risk.unverified_nodes, ("pve1",))
        self.assertTrue(risk.acknowledgeable)
        self.assertIn("pve1", risk.warning_message)

    def test_the_question_names_every_fact_that_applies_not_just_the_first(self):
        """The confirmation is what authorises the override, so it must be complete.

        A disk can be pointed at by a guest configuration *and* sit on storage a
        dead node never reported. Returning at whichever check ran first asked
        about one of them and then acted on both — an answer to a question the
        operator was never shown.
        """
        self._consumer(self.cluster_a)
        self._consumer(self.cluster_b)
        gate = _storage_gate_status([self.storage], {self.cluster_a.pk: {"pve1"}}, self.now)
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED, storage_gate_status=gate)
        ProxmoxInventory.objects.create(
            scan_run=scan,
            cluster=self.cluster_a,
            node="pve2",
            object_type=ProxmoxInventory.ObjectType.VM,
            vmid=100,
            name="ubuntu-test",
            status="stopped",
            disk_references=[f"{self.storage.storage_id}:100/vm-100-disk-0.qcow2"],
        )
        entry = FileInventory.objects.create(
            scan_run=scan,
            storage=self.storage,
            path="images/100/vm-100-disk-0.qcow2",
            derived_volid=f"{self.storage.storage_id}:100/vm-100-disk-0.qcow2",
            size_bytes=1,
            content_category="vm_disk",
            classification=FileInventory.Classification.REFERENCED,
        )

        message = file_action_risk(entry).warning_message

        self.assertIn("guest configuration still points at this file", message)
        self.assertIn("pve1", message)
        self.assertIn("ubuntu-test", message)

    def test_a_stale_running_status_from_an_unreachable_node_does_not_block(self):
        """A crashed node's guests are exactly the ones that are *not* running.

        "running" here is the last value anyone recorded, not a fact about now.
        Blocking on it would let a stale record lock the operator out of the
        recovery, so it becomes a confirmation that says the status is unconfirmed.
        """
        self._consumer(self.cluster_a)
        self._consumer(self.cluster_b)
        gate = _storage_gate_status([self.storage], {self.cluster_a.pk: {"pve1"}}, self.now)
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED, storage_gate_status=gate)
        entry = FileInventory.objects.create(
            scan_run=scan,
            storage=self.storage,
            path="images/100/vm-100-disk-0.qcow2",
            size_bytes=1,
            content_category="vm_disk",
            classification=FileInventory.Classification.REFERENCED,
        )
        stale = ReferencedObject(
            cluster_key="b", object_type="vm", vmid=100, name="ghost", node="pve1", status="running"
        )

        with patch("core.services.file_actions._referenced_objects", return_value=[stale]):
            risk = file_action_risk(entry)

        self.assertFalse(risk.blocked)
        self.assertTrue(risk.requires_extra_confirmation)
        self.assertIn("could not be confirmed", risk.warning_message)

    def test_a_running_guest_on_a_reachable_node_still_blocks(self):
        """Evidence from a node that answered is not overridable.

        The point of the change above is that *unknown* stops being a refusal —
        not that a guest genuinely running somewhere reachable becomes fair game.
        """
        self._consumer(self.cluster_a)
        gate = _storage_gate_status([self.storage], {self.cluster_a.pk: {"pve1"}}, self.now)
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED, storage_gate_status=gate)
        entry = FileInventory.objects.create(
            scan_run=scan,
            storage=self.storage,
            path="images/100/vm-100-disk-0.qcow2",
            size_bytes=1,
            content_category="vm_disk",
            classification=FileInventory.Classification.REFERENCED,
        )
        live = ReferencedObject(cluster_key="a", object_type="vm", vmid=100, name="real", node="pve1", status="running")

        with patch("core.services.file_actions._referenced_objects", return_value=[live]):
            risk = file_action_risk(entry)

        self.assertTrue(risk.blocked)
        self.assertEqual(risk.unverified_nodes, ())

    def test_single_cluster_coverage_still_opens_its_own_gate(self):
        consumer_a = self._consumer(self.cluster_a)

        status = _storage_gate_status([self.storage], {self.cluster_a.pk: {"pve1"}}, self.now)["nfs-vm"]

        self.assertTrue(status["ok"])
        self.assertEqual(status["status"], "complete")
        self.assertEqual(status["inventoried_consumers"], ["pve1"])
        self.assertEqual(status["expected_node_refs"], ["nr1:a:pve1"])

        consumer_a.refresh_from_db()
        self.assertEqual(consumer_a.last_gate_status, "ok")
        self.assertEqual(consumer_a.last_successful_inventory_scan, self.now)

    def test_unattributed_consumer_is_rejected_by_the_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProxmoxStorageConsumer.objects.create(storage=self.storage, cluster=None, expected_node_name="pve1")

    def test_expectation_without_a_consumer_row_is_missing(self):
        status = _storage_gate_status([self.storage], {self.cluster_a.pk: {"pve1"}}, self.now)["nfs-vm"]

        self.assertFalse(status["ok"])
        self.assertEqual(status["missing_consumers"], ["pve1"])

    def test_same_node_name_in_two_clusters_are_distinct_consumers(self):
        self._consumer(self.cluster_a)
        self._consumer(self.cluster_b)

        self.assertEqual(self.storage.consumer_statuses.count(), 2)
        self.assertEqual(
            sorted(str(c.node_ref()) for c in self.storage.consumer_statuses.all()),
            ["nr1:a:pve1", "nr1:b:pve1"],
        )

    def test_duplicate_consumer_within_one_cluster_is_still_rejected(self):
        self._consumer(self.cluster_a)

        with self.assertRaises(IntegrityError), transaction.atomic():
            self._consumer(self.cluster_a)


class NodeRefTests(SimpleTestCase):
    def test_roundtrips_through_the_versioned_serializer(self):
        ref = NodeRef(cluster_key="lab", node="pve1")

        self.assertEqual(ref.serialize(), "nr1:lab:pve1")
        self.assertEqual(NodeRef.parse("nr1:lab:pve1"), ref)

    def test_same_node_name_in_two_clusters_is_not_equal(self):
        self.assertNotEqual(NodeRef(cluster_key="a", node="pve1"), NodeRef(cluster_key="b", node="pve1"))

    def test_rejects_malformed_or_unknown_versions(self):
        for raw in ["", "pve1", "lab:pve1", "nr2:lab:pve1", "nr1::pve1", "nr1:LAB:pve1"]:
            with self.subTest(raw=raw), self.assertRaises(RefParseError):
                NodeRef.parse(raw)

    def test_rejects_invalid_construction(self):
        with self.assertRaises(RefParseError):
            NodeRef(cluster_key="Lab", node="pve1")
        with self.assertRaises(RefParseError):
            NodeRef(cluster_key="lab", node="")


class ConcurrentBootstrapTests(TransactionTestCase):
    """Bootstrap is a service invariant, not a process-startup assumption: scans
    already overlap, so concurrent callers must serialize on the advisory lock."""

    def test_concurrent_bootstrap_imports_exactly_once(self):
        # Counting imports rather than surviving rows is the point: the unique
        # constraints make the row counts converge even with no lock at all, so only
        # the import count distinguishes "serialized" from "raced, then repaired by
        # an IntegrityError". The contract is that losers observe the committed
        # marker and never repeat the import.
        real_import = runtime_bootstrap._import_environment
        counter_lock = threading.Lock()
        import_calls: list[int] = []

        def counting_import(state):
            with counter_lock:
                import_calls.append(1)
            return real_import(state)

        barrier = threading.Barrier(4)
        errors: list[Exception] = []

        def worker():
            try:
                barrier.wait(timeout=10)
                ensure_bootstrap()
            except Exception as exc:  # surfaced below; a silent thread failure would fake a pass
                errors.append(exc)
            finally:
                connection.close()

        with patch.object(runtime_bootstrap, "_import_environment", counting_import):
            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(len(import_calls), 1)
        self.assertEqual(ProxmoxCluster.objects.count(), 1)
        self.assertEqual(RuntimeConfigurationState.objects.count(), 1)
        self.assertTrue(RuntimeConfigurationState.objects.get().bootstrap_completed)


class DatastoreNavTests(TestCase):
    """The sidebar's datastores come from the catalog, split the way the catalog is."""

    def _cluster(self, key="local-storage"):
        cache.clear()
        return ProxmoxCluster.objects.create(key=key, display_name=key.title(), enabled=True)

    def test_node_local_storages_group_by_node(self):
        from core.services.datastore_nav import datastore_nav

        cluster = self._cluster()
        local_lvm = ClusterStorage.objects.create(cluster=cluster, storage_id="local-lvm", storage_type="lvmthin")
        local = ClusterStorage.objects.create(cluster=cluster, storage_id="local", storage_type="dir")
        ClusterStorageNodeState.objects.create(
            cluster_storage=local_lvm, node="pve1", total_bytes=100, used_bytes=40, available_bytes=60, active=True
        )
        ClusterStorageNodeState.objects.create(
            cluster_storage=local, node="pve1", total_bytes=200, used_bytes=50, available_bytes=150, active=True
        )
        ClusterStorageNodeState.objects.create(cluster_storage=local, node="pve2", total_bytes=0, active=True)

        groups = datastore_nav(use_cache=False, cluster=cluster)
        nodes = {entry["node"]: entry["storages"] for entry in groups["nodes"]}
        self.assertEqual(sorted(nodes), ["pve1", "pve2"])
        pve1 = {s["storage_id"]: s for s in nodes["pve1"]}
        self.assertEqual(sorted(pve1), ["local", "local-lvm"])
        self.assertEqual(pve1["local-lvm"]["used_pct"], 40)  # 40/100
        self.assertIsNone(nodes["pve2"][0]["used_pct"])  # total 0 -> no percentage

    def test_a_shared_storage_is_listed_once_without_a_registered_mount(self):
        """The old tree sourced its shared group from StorageMount, so a shared PVE
        storage nobody had registered a host mount for appeared nowhere at all."""
        from core.services.datastore_nav import datastore_nav

        cluster = self._cluster()
        shared = ClusterStorage.objects.create(
            cluster=cluster, storage_id="TrueNAS-VM", storage_type="nfs", shared=True
        )
        ClusterStorageNodeState.objects.create(cluster_storage=shared, node="pve1", active=False)
        ClusterStorageNodeState.objects.create(cluster_storage=shared, node="pve2", active=True)
        self.assertFalse(StorageMount.objects.exists())

        groups = datastore_nav(use_cache=False, cluster=cluster)
        self.assertEqual([s["storage_id"] for s in groups["shared"]], ["TrueNAS-VM"])
        self.assertEqual(groups["nodes"], [])
        # One cluster-wide object, so its page carries no node at all. Capacity
        # still comes from the first *active* instance, the rule the catalog table
        # on the Overview page uses.
        self.assertEqual(groups["shared"][0]["link_node"], "")
        self.assertEqual(groups["shared"][0]["nav_key"], "local-storage||TrueNAS-VM")

    def test_the_highlight_key_separates_two_clusters_sharing_a_node_name(self):
        """Proxmox defaults make `pve1`/`local` collide across clusters; the old
        template compared node and storage id only, and highlighted both leaves."""
        from core.services.datastore_nav import datastore_nav, nav_datastore_key

        keys = []
        for cluster_key in ("alpha", "beta"):
            cluster = self._cluster(cluster_key)
            local = ClusterStorage.objects.create(cluster=cluster, storage_id="local", storage_type="dir")
            ClusterStorageNodeState.objects.create(cluster_storage=local, node="pve1", active=True)
            groups = datastore_nav(use_cache=False, cluster=cluster)
            keys.append(groups["nodes"][0]["storages"][0]["nav_key"])

        self.assertEqual(len(set(keys)), 2)
        self.assertEqual(keys[0], nav_datastore_key("alpha", "local", "pve1"))
        # A shared storage's key carries no node: the detail page is reachable
        # through any of them, and all of them must highlight the one leaf.
        self.assertEqual(nav_datastore_key("alpha", "TrueNAS-VM"), "alpha||TrueNAS-VM")

    def test_the_rendered_sidebar_shows_the_catalog_and_names_mounts_as_mounts(self):
        cluster = self._cluster()
        shared = ClusterStorage.objects.create(
            cluster=cluster, storage_id="TrueNAS-VM", storage_type="nfs", shared=True
        )
        local = ClusterStorage.objects.create(cluster=cluster, storage_id="local-lvm", storage_type="lvmthin")
        ClusterStorageNodeState.objects.create(cluster_storage=shared, node="pve1", active=True)
        ClusterStorageNodeState.objects.create(cluster_storage=local, node="pve1", active=True)
        StorageMount.objects.create(
            storage_id="TrueNAS-FS", display_name="TrueNAS-FS", path="/tmp/truenas-fs", enabled=True
        )

        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
        # The shared datastore's page is cluster-wide; the node-local one is not.
        self.assertContains(response, f"/clusters/{cluster.key}/datastores/TrueNAS-VM/summary/")
        self.assertContains(response, f"/clusters/{cluster.key}/nodes/pve1/datastores/local-lvm/summary/")
        # Registered mounts are configuration and live only under PVE-helper
        # Settings; the tree shows datastores as the clusters see them.
        self.assertNotContains(response, ">Host Mounts<")
        self.assertNotContains(response, "Shared Datastores")
        self.assertNotContains(response, "Local Datastores")


class ApiStorageContentTests(SimpleTestCase):
    """Local-storage content editor blocker/options logic (API-volume based)."""

    def test_blocker_flags_content_types_in_use(self):
        from core.views.storage import _api_content_blockers, _api_content_options

        usage = {"images": {"count": 3, "examples": ["local-lvm:vm-100-disk-0"]}}
        # Removing a type volumes still use is blocked; an unused type is fine.
        self.assertTrue(_api_content_blockers(usage, ["images"]))
        self.assertFalse(_api_content_blockers(usage, ["iso"]))

        options = {o["key"]: o for o in _api_content_options(["images", "rootdir"], usage)}
        self.assertTrue(options["images"]["selected"])
        self.assertEqual(options["images"]["usage_count"], 3)
        self.assertFalse(options["iso"]["selected"])


class StickyTabUrlTests(SimpleTestCase):
    """Tab-persistent object switching (nav_tags.sticky_object_url)."""

    def _sticky(self, current_path, summary_url):
        from core.templatetags.nav_tags import sticky_object_url

        context = {"request": RequestFactory().get(current_path)}
        return sticky_object_url(context, summary_url)

    def test_keeps_tab_when_switching_guest(self):
        # On guest 506's Networks tab, switching to 501 stays on Networks.
        self.assertEqual(
            self._sticky("/vms/vm/506/networks/", "/vms/ct/501/summary/"),
            "/vms/ct/501/networks/",
        )

    def test_keeps_tab_when_switching_storage(self):
        self.assertEqual(
            self._sticky(
                "/clusters/hq/datastores/nfs-a/monitor/",
                "/clusters/hq/datastores/nfs-b/summary/",
            ),
            "/clusters/hq/datastores/nfs-b/monitor/",
        )

    def test_falls_back_to_summary_across_object_families(self):
        # A storage leaf viewed while on a guest tab has no matching tab.
        self.assertEqual(
            self._sticky("/vms/vm/506/networks/", "/storage/nfs-b/summary/"),
            "/storage/nfs-b/summary/",
        )

    def test_falls_back_when_no_active_tab(self):
        # A list/overview page (no per-object tab) resolves to Summary.
        self.assertEqual(
            self._sticky("/vms/", "/vms/vm/500/summary/"),
            "/vms/vm/500/summary/",
        )


class ProxmoxClientTests(SimpleTestCase):
    def _response(self, data, *, status_code=200):
        return httpx.Response(
            status_code,
            json={"data": data},
            request=httpx.Request("GET", "https://pve.example.com/api2/json/test"),
        )

    def test_request_surfaces_proxmox_error_message(self):
        client = ProxmoxClient("https://pve.example.com:8006")
        error_response = httpx.Response(
            500,
            json={"data": None, "message": "Linked clone feature is not supported for 'x' (scsi0)\n"},
            request=httpx.Request("POST", "https://pve.example.com/api2/json/nodes/pve3/qemu/507/clone"),
        )
        mock_http = Mock()
        mock_http.request.return_value = error_response
        with patch("core.services.cluster_trust.http_client_for", return_value=mock_http):
            with self.assertRaisesMessage(ProxmoxAPIError, "Linked clone feature is not supported"):
                client.post("nodes/pve3/qemu/507/clone", data={"newid": 102, "full": 0})

    @override_settings(PVE_TEST_NETWORK_DISABLED=True)
    def test_test_network_guard_rejects_unmocked_proxmox_requests(self):
        client = ProxmoxClient("https://pve.example.com:8006")

        with self.assertRaisesMessage(AssertionError, "unmocked Proxmox HTTP request"):
            client.get("version")

    def test_request_normalizes_successful_invalid_response_payloads(self):
        client = ProxmoxClient("https://pve.example.com:8006")
        responses = {
            "html": httpx.Response(
                200,
                content=b"<html>proxy error</html>",
                request=httpx.Request("GET", "https://pve.example.com/api2/json/version"),
            ),
            "json list": httpx.Response(
                200,
                json=["not", "an", "object"],
                request=httpx.Request("GET", "https://pve.example.com/api2/json/version"),
            ),
            "missing data": httpx.Response(
                200,
                json={"status": "ok"},
                request=httpx.Request("GET", "https://pve.example.com/api2/json/version"),
            ),
        }

        for label, response in responses.items():
            with self.subTest(label=label):
                mock_http = Mock()
                mock_http.request.return_value = response
                with patch("core.services.cluster_trust.http_client_for", return_value=mock_http):
                    with self.assertRaises(ProxmoxAPIError):
                        client.get("version")

    @override_settings(STORAGE_WRITE_ENABLED=False)
    def test_set_storage_content_refuses_when_storage_writes_are_disabled(self):
        client = ProxmoxClient("https://pve.example.com:8006")
        mock_http = Mock()

        with patch("core.services.cluster_trust.http_client_for", return_value=mock_http):
            with self.assertRaisesMessage(ProxmoxAPIError, "disabled"):
                client.set_storage_content("nfs-vm", ["images"])

        mock_http.request.assert_not_called()

    @override_settings(
        PVE_VERIFY_TLS=True,
        PVE_CA_BUNDLE="",
        PVE_API_TOKEN_ID="pve-helper@pve!pve-helper",
        PVE_API_TOKEN_SECRET="secret",
    )
    def test_power_action_posts_and_returns_upid(self):
        client = ProxmoxClient("https://pve.example.com:8006")
        upid = "UPID:pve1:00000001:00000002:00000003:qmstart:100:root@pam:"

        mock_http = Mock()
        mock_http.request.return_value = self._response(upid)
        with patch("core.services.cluster_trust.http_client_for", return_value=mock_http):
            result = client.power_action(node="pve1", object_type="vm", vmid=100, action="start")

        self.assertEqual(result, upid)
        mock_http.request.assert_called_once()
        args, kwargs = mock_http.request.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "https://pve.example.com:8006/api2/json/nodes/pve1/qemu/100/status/start")
        self.assertEqual(kwargs["data"], {})
        self.assertEqual(kwargs["headers"]["Authorization"], "PVEAPIToken=pve-helper@pve!pve-helper=secret")

    def test_power_action_rejects_unsupported_action(self):
        client = ProxmoxClient("https://pve.example.com:8006")

        mock_http = Mock()
        with patch("core.services.cluster_trust.http_client_for", return_value=mock_http):
            with self.assertRaisesMessage(ProxmoxAPIError, "Unsupported power action"):
                client.power_action(node="pve1", object_type="vm", vmid=100, action="suspend")

        mock_http.request.assert_not_called()

    def test_wait_for_task_returns_successful_result(self):
        client = ProxmoxClient("https://pve.example.com:8006")

        with patch.object(
            client,
            "task_status",
            side_effect=[
                {"status": "running"},
                {"status": "stopped", "exitstatus": "OK"},
            ],
        ):
            result = client.wait_for_task(
                node="pve1",
                upid="UPID:pve1:test:",
                timeout_seconds=10,
                poll_interval_seconds=0.1,
                sleep_func=lambda _seconds: None,
                monotonic_func=lambda: 0,
            )

        self.assertTrue(result.success)
        self.assertEqual(result.status, "stopped")
        self.assertEqual(result.exitstatus, "OK")

    def test_wait_for_task_returns_failed_result(self):
        client = ProxmoxClient("https://pve.example.com:8006")

        with patch.object(client, "task_status", return_value={"status": "stopped", "exitstatus": "interrupted"}):
            result = client.wait_for_task(
                node="pve1",
                upid="UPID:pve1:test:",
                timeout_seconds=10,
                poll_interval_seconds=0.1,
                sleep_func=lambda _seconds: None,
                monotonic_func=lambda: 0,
            )

        self.assertFalse(result.success)
        self.assertEqual(result.exitstatus, "interrupted")

    def test_wait_for_task_times_out(self):
        client = ProxmoxClient("https://pve.example.com:8006")
        monotonic_values = iter([0, 11])

        with patch.object(client, "task_status", return_value={"status": "running"}):
            with self.assertRaisesMessage(ProxmoxTaskTimeout, "Timed out"):
                client.wait_for_task(
                    node="pve1",
                    upid="UPID:pve1:test:",
                    timeout_seconds=10,
                    poll_interval_seconds=0.1,
                    sleep_func=lambda _seconds: None,
                    monotonic_func=lambda: next(monotonic_values),
                )


class ScheduledActionModelTests(TestCase):
    def test_run_occurrence_key_is_unique_per_action(self):
        action = ScheduledAction.objects.create(
            name="Night shutdown",
            action_type=ScheduledAction.ActionType.SHUTDOWN,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=500,
            schedule_type=ScheduledAction.ScheduleType.ONCE,
            next_run_at=timezone.now(),
        )
        planned_for = timezone.now()

        ScheduledActionRun.objects.create(
            scheduled_action=action,
            planned_for=planned_for,
            occurrence_key="2026-07-01T22:00:00Z",
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ScheduledActionRun.objects.create(
                    scheduled_action=action,
                    planned_for=planned_for,
                    occurrence_key="2026-07-01T22:00:00Z",
                )

    def test_same_occurrence_key_can_be_used_for_different_actions(self):
        first = ScheduledAction.objects.create(
            name="Start VM 500",
            action_type=ScheduledAction.ActionType.START,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=500,
        )
        second = ScheduledAction.objects.create(
            name="Start VM 501",
            action_type=ScheduledAction.ActionType.START,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=501,
        )
        planned_for = timezone.now()

        ScheduledActionRun.objects.create(
            scheduled_action=first,
            planned_for=planned_for,
            occurrence_key="2026-07-01T22:00:00Z",
        )
        ScheduledActionRun.objects.create(
            scheduled_action=second,
            planned_for=planned_for,
            occurrence_key="2026-07-01T22:00:00Z",
        )

        self.assertEqual(ScheduledActionRun.objects.count(), 2)


class ScheduledRecurrenceTests(TestCase):
    def _recurring_action(self, *, kind, recurrence, timezone_name="UTC"):
        return ScheduledAction.objects.create(
            name="Recurring action",
            action_type=ScheduledAction.ActionType.SHUTDOWN,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=500,
            schedule_type=ScheduledAction.ScheduleType.RECURRING,
            recurrence_kind=kind,
            recurrence=recurrence,
            timezone=timezone_name,
        )

    def test_next_run_supports_first_sunday_of_month(self):
        action = self._recurring_action(
            kind=ScheduledAction.RecurrenceKind.MONTHLY_ORDINAL,
            recurrence={"ordinal": "first", "weekday": "sunday", "time": "22:00"},
            timezone_name="Europe/Stockholm",
        )
        after = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.UTC)

        next_run = next_run_after(action, after=after)

        self.assertEqual(next_run, datetime(2026, 7, 5, 20, 0, tzinfo=timezone.UTC))

    def test_next_run_supports_multiple_monthly_ordinals(self):
        action = self._recurring_action(
            kind=ScheduledAction.RecurrenceKind.MONTHLY_ORDINAL,
            recurrence={"ordinals": ["second", "fourth"], "weekdays": ["2"], "time": "22:00"},
            timezone_name="Europe/Stockholm",
        )

        first = next_run_after(action, after=datetime(2026, 6, 30, 12, 0, tzinfo=timezone.UTC))
        second = next_run_after(action, after=datetime(2026, 7, 9, 12, 0, tzinfo=timezone.UTC))

        self.assertEqual(first, datetime(2026, 7, 8, 20, 0, tzinfo=timezone.UTC))
        self.assertEqual(second, datetime(2026, 7, 22, 20, 0, tzinfo=timezone.UTC))

    def test_monthly_day_skips_months_without_that_day(self):
        action = self._recurring_action(
            kind=ScheduledAction.RecurrenceKind.MONTHLY_DAY,
            recurrence={"day_of_month": 31, "time": "22:00"},
        )

        first = next_run_after(action, after=datetime(2026, 1, 31, 22, 30, tzinfo=timezone.UTC))

        self.assertEqual(first, datetime(2026, 3, 31, 22, 0, tzinfo=timezone.UTC))

    def test_invalid_timezone_raises_clear_error(self):
        action = self._recurring_action(
            kind=ScheduledAction.RecurrenceKind.DAILY,
            recurrence={"time": "22:00"},
            timezone_name="Mars/Olympus",
        )

        with self.assertRaisesMessage(RecurrenceError, "Unknown timezone"):
            next_run_after(action, after=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.UTC))


class ScheduledActionDispatchTests(TestCase):
    def setUp(self):
        self.cluster = ProxmoxCluster.objects.create(key="default", display_name="Default cluster", enabled=True)

    def _action(self, *, next_run_at=None, catch_up_policy=None, max_lateness_minutes=0):
        return ScheduledAction.objects.create(
            cluster=self.cluster,
            name="Start VM 500",
            action_type=ScheduledAction.ActionType.START,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=500,
            schedule_type=ScheduledAction.ScheduleType.ONCE,
            next_run_at=next_run_at or timezone.now(),
            catch_up_policy=catch_up_policy or ScheduledAction.CatchUpPolicy.SKIP_MISSED,
            max_lateness_minutes=max_lateness_minutes,
        )

    def test_dispatch_queues_due_one_time_action_and_disables_future_runs(self):
        now = timezone.now()
        action = self._action(next_run_at=now)
        enqueue = Mock(return_value="task-id")

        result = dispatch_due_scheduled_actions(now=now, enqueue_func=enqueue)

        self.assertEqual(result.queued, 1)
        run = ScheduledActionRun.objects.get(scheduled_action=action)
        self.assertEqual(run.status, ScheduledActionRun.Status.QUEUED)
        enqueue.assert_called_once_with("core.tasks.run_scheduled_action", run.id)
        action.refresh_from_db()
        self.assertFalse(action.enabled)
        self.assertIsNone(action.next_run_at)
        self.assertEqual(action.last_status, ScheduledAction.LastStatus.QUEUED)
        self.assertTrue(AuditEvent.objects.filter(action="scheduled_action.run_queued").exists())

    def test_dispatch_marks_old_due_action_missed_without_catchup(self):
        now = timezone.now()
        action = self._action(next_run_at=now - timedelta(minutes=10))
        enqueue = Mock()

        result = dispatch_due_scheduled_actions(now=now, enqueue_func=enqueue)

        self.assertEqual(result.missed, 1)
        run = ScheduledActionRun.objects.get(scheduled_action=action)
        self.assertEqual(run.status, ScheduledActionRun.Status.MISSED)
        self.assertEqual(run.outcome, ScheduledActionRun.Outcome.MISSED)
        action.refresh_from_db()
        self.assertFalse(action.enabled)
        self.assertEqual(action.last_status, ScheduledAction.LastStatus.MISSED)
        enqueue.assert_not_called()

    def test_dispatch_does_not_queue_when_previous_run_is_in_flight(self):
        now = timezone.now()
        action = self._action(next_run_at=now)
        ScheduledActionRun.objects.create(
            scheduled_action=action,
            planned_for=now - timedelta(minutes=1),
            occurrence_key="previous",
            status=ScheduledActionRun.Status.POLLING,
        )
        enqueue = Mock()

        result = dispatch_due_scheduled_actions(now=now, enqueue_func=enqueue)

        self.assertEqual(result.skipped, 1)
        self.assertEqual(ScheduledActionRun.objects.count(), 1)
        action.refresh_from_db()
        self.assertTrue(action.enabled)
        enqueue.assert_not_called()

    def test_dispatch_recurring_action_advances_next_run(self):
        now = datetime(2026, 7, 1, 21, 0, tzinfo=timezone.UTC)
        action = ScheduledAction.objects.create(
            cluster=self.cluster,
            name="Night shutdown",
            action_type=ScheduledAction.ActionType.SHUTDOWN,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=500,
            schedule_type=ScheduledAction.ScheduleType.RECURRING,
            recurrence_kind=ScheduledAction.RecurrenceKind.DAILY,
            recurrence={"time": "22:00"},
            timezone="UTC",
            next_run_at=now,
        )
        enqueue = Mock(return_value="task-id")

        result = dispatch_due_scheduled_actions(now=now, enqueue_func=enqueue)

        self.assertEqual(result.queued, 1)
        action.refresh_from_db()
        self.assertTrue(action.enabled)
        self.assertEqual(action.next_run_at, datetime(2026, 7, 1, 22, 0, tzinfo=timezone.UTC))
        run = ScheduledActionRun.objects.get(scheduled_action=action)
        self.assertEqual(run.planned_for, now)
        enqueue.assert_called_once_with("core.tasks.run_scheduled_action", run.id)

    def test_dispatch_task_returns_serializable_result(self):
        result = dispatch_scheduled_actions()

        self.assertEqual(result["queued"], 0)
        self.assertNotIn("disabled", result)

    @override_settings(SCHEDULED_ACTION_TIMEOUT_SECONDS=30)
    def test_stale_reaper_marks_old_in_flight_runs(self):
        now = timezone.now()
        action = self._action(next_run_at=now + timedelta(hours=1))
        action.action_timeout_seconds = 30
        action.save(update_fields=["action_timeout_seconds", "updated_at"])
        run = ScheduledActionRun.objects.create(
            scheduled_action=action,
            planned_for=now - timedelta(minutes=15),
            occurrence_key="stale",
            status=ScheduledActionRun.Status.POLLING,
            started_at=now - timedelta(minutes=10),
        )

        count = reap_stale_scheduled_action_runs(now=now)

        self.assertEqual(count, 1)
        run.refresh_from_db()
        action.refresh_from_db()
        self.assertEqual(run.status, ScheduledActionRun.Status.STALE)
        self.assertEqual(run.outcome, ScheduledActionRun.Outcome.STALE)
        self.assertEqual(action.last_status, ScheduledAction.LastStatus.FAILED)
        event = AuditEvent.objects.get(action="scheduled_action.run_failed")
        self.assertEqual(event.outcome, "stale")

    def test_run_retention_prunes_finished_history_only(self):
        now = timezone.now()
        action = self._action(next_run_at=now + timedelta(hours=1))
        old_finished = ScheduledActionRun.objects.create(
            scheduled_action=action,
            planned_for=now - timedelta(days=120),
            occurrence_key="old-finished",
            status=ScheduledActionRun.Status.COMPLETED,
            outcome=ScheduledActionRun.Outcome.SUCCESS,
            finished_at=now - timedelta(days=120),
        )
        old_in_flight = ScheduledActionRun.objects.create(
            scheduled_action=action,
            planned_for=now - timedelta(days=120),
            occurrence_key="old-active",
            status=ScheduledActionRun.Status.QUEUED,
            finished_at=now - timedelta(days=120),
        )

        count = prune_scheduled_action_runs(retention_days=90, now=now)

        self.assertEqual(count, 1)
        self.assertFalse(ScheduledActionRun.objects.filter(pk=old_finished.pk).exists())
        self.assertTrue(ScheduledActionRun.objects.filter(pk=old_in_flight.pk).exists())
        event = AuditEvent.objects.get(action="scheduled_action.run_retention.purge")
        self.assertEqual(event.details["purged"], 1)


class ScheduledActionExecutionTests(TestCase):
    def setUp(self):
        self.cluster = ProxmoxCluster.objects.create(key="default", display_name="Default cluster", enabled=True)

    class FakeProxmoxClient:
        def __init__(self, *, status="stopped", config=None, task_success=True):
            self.status = status
            self.config = config or {"name": "Test VM"}
            self.task_success = task_success
            self.power_calls = []

        def node_names(self, *, fallback=""):
            return ["pve1"]

        def guest_current(self, *, node, object_type, vmid):
            return {"status": self.status, "name": "Test VM"}

        def guest_config(self, *, node, object_type, vmid):
            return self.config

        def power_action(self, *, node, object_type, vmid, action, parameters=None):
            self.power_calls.append(
                {
                    "node": node,
                    "object_type": object_type,
                    "vmid": vmid,
                    "action": action,
                    "parameters": parameters or {},
                }
            )
            return "UPID:pve1:test:"

        def wait_for_task(self, *, node, upid, timeout_seconds=None):
            exitstatus = "OK" if self.task_success else "interrupted"
            if self.task_success and self.power_calls:
                self.status = {
                    "start": "running",
                    "reboot": "running",
                    "shutdown": "stopped",
                    "stop": "stopped",
                }.get(self.power_calls[-1]["action"], self.status)
            return ProxmoxTaskResult(
                node=node,
                upid=upid,
                status="stopped",
                exitstatus=exitstatus,
                raw={"status": "stopped", "exitstatus": exitstatus},
            )

    def _queued_run(self, *, action_type=ScheduledAction.ActionType.START):
        action = ScheduledAction.objects.create(
            cluster=self.cluster,
            name="Power action",
            action_type=action_type,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=500,
            action_timeout_seconds=30,
        )
        run = ScheduledActionRun.objects.create(
            scheduled_action=action,
            planned_for=timezone.now(),
            occurrence_key="manual",
            status=ScheduledActionRun.Status.QUEUED,
        )
        ProxmoxEndpoint.objects.create(
            cluster=self.cluster,
            name="pve1",
            url="https://pve1.example.com:8006",
        )
        return action, run

    def test_execute_run_submits_power_action_and_records_success(self):
        action, run = self._queued_run()
        fake_client = self.FakeProxmoxClient(status="stopped")

        execute_scheduled_action_run(run.id, client_factory=lambda _url: fake_client)

        run.refresh_from_db()
        action.refresh_from_db()
        self.assertEqual(run.status, ScheduledActionRun.Status.COMPLETED)
        self.assertEqual(run.outcome, ScheduledActionRun.Outcome.SUCCESS)
        self.assertEqual(run.proxmox_task_node, "pve1")
        self.assertEqual(run.proxmox_task_upid, "UPID:pve1:test:")
        self.assertEqual(run.preflight_snapshot["status"], "stopped")
        self.assertEqual(action.last_status, ScheduledAction.LastStatus.COMPLETED)
        self.assertEqual(fake_client.power_calls[0]["action"], "start")
        self.assertEqual(CurrentGuestInventory.objects.get(object_type="vm", vmid=500).status, "running")
        self.assertTrue(AuditEvent.objects.filter(action="scheduled_action.run_completed").exists())

    def test_execute_run_completes_noop_when_guest_already_in_desired_state(self):
        action, run = self._queued_run()
        fake_client = self.FakeProxmoxClient(status="running")

        execute_scheduled_action_run(run.id, client_factory=lambda _url: fake_client)

        run.refresh_from_db()
        action.refresh_from_db()
        self.assertEqual(run.status, ScheduledActionRun.Status.COMPLETED)
        self.assertEqual(run.outcome, ScheduledActionRun.Outcome.SUCCESS_NOOP)
        self.assertEqual(run.proxmox_task_node, "pve1")
        self.assertEqual(run.preflight_snapshot["node"], "pve1")
        self.assertEqual(fake_client.power_calls, [])
        self.assertEqual(action.last_status, ScheduledAction.LastStatus.COMPLETED)

    def test_execute_run_skips_locked_guest(self):
        action, run = self._queued_run(action_type=ScheduledAction.ActionType.SHUTDOWN)
        fake_client = self.FakeProxmoxClient(status="running", config={"name": "Test VM", "lock": "backup"})

        execute_scheduled_action_run(run.id, client_factory=lambda _url: fake_client)

        run.refresh_from_db()
        action.refresh_from_db()
        self.assertEqual(run.status, ScheduledActionRun.Status.SKIPPED)
        self.assertEqual(run.outcome, ScheduledActionRun.Outcome.SKIPPED)
        self.assertIn("locked", run.error)
        self.assertEqual(fake_client.power_calls, [])
        self.assertEqual(action.last_status, ScheduledAction.LastStatus.SKIPPED)


@override_settings(OIDC_ISSUER_URL="https://issuer.example/application/o/pve-helper")
class OidcBackendTests(TestCase):
    def setUp(self):
        self.backend = PveHelperOIDCBackend()

    def _group_claims(self, groups):
        return {
            "sub": "subject-1",
            "preferred_username": "alice",
            "email": "alice@example.test",
            "groups": groups,
        }

    @override_settings(OIDC_REQUIRED_GROUP="gg-pve-helper-admins")
    def test_group_claim_must_contain_the_required_group(self):
        self.assertTrue(self.backend.verify_claims(self._group_claims(["gg-pve-helper-admins"])))
        self.assertFalse(self.backend.verify_claims(self._group_claims(["gg-other"])))
        self.assertFalse(self.backend.verify_claims(self._group_claims([])))

    @override_settings(OIDC_REQUIRED_GROUP=settings.OIDC_ANY_AUTHENTICATED_USER)
    def test_sentinel_waives_the_group_check_for_provider_gated_deployments(self):
        """Providers that emit no usable group claim opt out explicitly, never by omission."""
        self.assertTrue(self.backend.verify_claims(self._group_claims([])))
        self.assertTrue(self.backend.verify_claims({"sub": "s", "email": "a@example.test"}))

    @override_settings(OIDC_REQUIRED_GROUP="")
    def test_empty_required_group_denies_login_instead_of_admitting_everyone(self):
        """Startup rejects this (E011); if it is reached anyway the check was lost, not waived."""
        self.assertFalse(self.backend.verify_claims(self._group_claims(["gg-pve-helper-admins"])))

    @override_settings(DJANGO_ADMIN_ENABLED=False)
    def test_login_grants_no_django_admin_rights_where_admin_is_not_routed(self):
        """is_staff/is_superuser unlock Django admin and nothing else; the app's own
        authorization is the OIDC group. Granting them where admin is unrouted would
        be dead privilege waiting for someone to mount it."""
        user = self.backend.create_user(self._group_claims(["gg-pve-helper-admins"]))

        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    @override_settings(DJANGO_ADMIN_ENABLED=True)
    def test_links_and_finds_user_by_subject_after_username_change(self):
        claims = {
            "sub": "subject-1",
            "preferred_username": "alice",
            "email": "alice@example.test",
            "given_name": "Stefan",
            "family_name": "Nilsson",
        }

        user = self.backend.create_user(claims)

        self.assertEqual(user.username, "alice")
        self.assertTrue(user.is_superuser)
        self.assertTrue(
            OidcIdentity.objects.filter(
                user=user,
                issuer="https://issuer.example/application/o/pve-helper",
                subject="subject-1",
            ).exists()
        )

        renamed_claims = {
            **claims,
            "preferred_username": "alice.renamed",
            "email": "new@example.test",
        }

        self.assertEqual(list(self.backend.filter_users_by_claims(renamed_claims)), [user])
        updated = self.backend.update_user(user, renamed_claims)

        self.assertEqual(updated.username, "alice.renamed")
        self.assertEqual(updated.email, "new@example.test")

    def test_links_existing_username_once_when_identity_missing(self):
        User = get_user_model()
        user = User.objects.create_user(username="alice", email="old@example.test")
        claims = {
            "sub": "subject-1",
            "preferred_username": "alice",
            "email": "new@example.test",
        }

        self.assertEqual(list(self.backend.filter_users_by_claims(claims)), [user])

        updated = self.backend.update_user(user, claims)

        self.assertEqual(updated.pk, user.pk)
        self.assertEqual(updated.email, "new@example.test")
        self.assertTrue(OidcIdentity.objects.filter(user=user, subject="subject-1").exists())

    def test_reused_username_does_not_match_different_subject_identity(self):
        User = get_user_model()
        old_user = User.objects.create_user(username="alex", email="old@example.test")
        OidcIdentity.objects.create(
            user=old_user,
            issuer="https://issuer.example/application/o/pve-helper",
            subject="old-subject",
        )
        claims = {
            "sub": "new-subject",
            "preferred_username": "alex",
            "email": "new@example.test",
        }

        self.assertEqual(list(self.backend.filter_users_by_claims(claims)), [])
        new_user = self.backend.create_user(claims)

        self.assertNotEqual(new_user.pk, old_user.pk)
        self.assertEqual(new_user.username, "alex-2")
        self.assertTrue(OidcIdentity.objects.filter(user=new_user, subject="new-subject").exists())

    def test_subject_collision_denies_access_cleanly(self):
        User = get_user_model()
        old_user = User.objects.create_user(username="alex", email="old@example.test")
        new_user = User.objects.create_user(username="alice", email="new@example.test")
        OidcIdentity.objects.create(
            user=old_user,
            issuer="https://issuer.example/application/o/pve-helper",
            subject="subject-1",
        )
        claims = {
            "sub": "subject-1",
            "preferred_username": "alice",
            "email": "new@example.test",
        }

        with self.assertRaises(PermissionDenied):
            self.backend.update_user(new_user, claims)


class ScanRetentionTests(TestCase):
    def test_prunes_stale_inventory_per_storage_and_old_scan_metadata(self):
        now = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.get_current_timezone())
        fs_storage = StorageMount.objects.create(storage_id="nfs-fs", display_name="nfs-fs", path="/fs")
        vm_storage = StorageMount.objects.create(storage_id="nfs-vm", display_name="nfs-vm", path="/vm")

        old_global = self._completed_scan(now - timedelta(days=10))
        old_fs = self._completed_scan(now - timedelta(days=9), target_storage=fs_storage)
        latest_fs = self._completed_scan(now - timedelta(hours=1), target_storage=fs_storage)
        old_failed = ScanRun.objects.create(status=ScanRun.Status.FAILED, finished_at=now - timedelta(days=9))
        old_metadata_only = self._completed_scan(now - timedelta(days=9), target_storage=fs_storage)
        ScanRun.objects.filter(pk=old_failed.pk).update(created_at=now - timedelta(days=9))

        self._file(old_global, fs_storage, "template/iso/old-global.iso")
        self._file(old_global, vm_storage, "images/501/vm-501-disk-0.qcow2")
        self._file(old_fs, fs_storage, "template/iso/old-target.iso")
        self._file(old_failed, fs_storage, "template/iso/failed-leftover.iso")
        self._file(latest_fs, fs_storage, "template/iso/current-target.iso")
        self._proxmox_object(old_global, "old-global")
        self._proxmox_object(old_fs, "old-fs")
        self._proxmox_object(latest_fs, "latest-fs")

        result = prune_scan_history(now=now)

        self.assertTrue(result.deleted_anything)
        self.assertFalse(FileInventory.objects.filter(scan_run=old_global, storage=fs_storage).exists())
        self.assertTrue(FileInventory.objects.filter(scan_run=old_global, storage=vm_storage).exists())
        self.assertFalse(FileInventory.objects.filter(scan_run=old_fs).exists())
        self.assertFalse(FileInventory.objects.filter(scan_run=old_failed).exists())
        self.assertTrue(FileInventory.objects.filter(scan_run=latest_fs, storage=fs_storage).exists())
        self.assertTrue(ProxmoxInventory.objects.filter(scan_run=old_global).exists())
        self.assertFalse(ProxmoxInventory.objects.filter(scan_run=old_fs).exists())
        self.assertTrue(ProxmoxInventory.objects.filter(scan_run=latest_fs).exists())
        self.assertTrue(ScanRun.objects.filter(pk=old_global.pk).exists())
        self.assertTrue(ScanRun.objects.filter(pk=latest_fs.pk).exists())
        self.assertFalse(ScanRun.objects.filter(pk=old_fs.pk).exists())
        self.assertFalse(ScanRun.objects.filter(pk=old_failed.pk).exists())
        self.assertFalse(ScanRun.objects.filter(pk=old_metadata_only.pk).exists())

    def _completed_scan(self, when, *, target_storage=None) -> ScanRun:
        scan = ScanRun.objects.create(
            status=ScanRun.Status.COMPLETED,
            target_storage=target_storage,
            finished_at=when,
            filesystem_scan_at=when,
            progress_message="Scan completed.",
        )
        ScanRun.objects.filter(pk=scan.pk).update(created_at=when, updated_at=when)
        scan.refresh_from_db()
        return scan

    def _file(self, scan: ScanRun, storage: StorageMount, path: str) -> FileInventory:
        return FileInventory.objects.create(
            scan_run=scan,
            storage=storage,
            path=path,
            entry_type=FileInventory.EntryType.FILE,
            classification=FileInventory.Classification.UNKNOWN,
        )

    def _proxmox_object(
        self, scan: ScanRun, name: str, *, cluster=None, object_type=ProxmoxInventory.ObjectType.VM
    ) -> ProxmoxInventory:
        return ProxmoxInventory.objects.create(
            scan_run=scan,
            cluster=cluster,
            node="pve-node-1",
            object_type=object_type,
            vmid=100 if object_type == ProxmoxInventory.ObjectType.VM else None,
            name=name,
        )

    def test_keeps_latest_cluster_proxmox_inventory_without_file_backing(self):
        # Regression: ProxmoxInventory (cluster guests + local/API-only storages)
        # has a different lifecycle from mounted-storage FileInventory. The latest
        # completed scan of a cluster must survive retention even when it backs no
        # FileInventory, or the Datastores nav loses its local storages the moment
        # a scan finishes.
        now = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.get_current_timezone())
        cluster = ProxmoxCluster.objects.create(key="hq", display_name="HQ", enabled=True)
        superseded = self._completed_scan(now - timedelta(hours=6))
        latest = self._completed_scan(now - timedelta(hours=1))
        self._proxmox_object(superseded, "old-local", cluster=cluster, object_type=ProxmoxInventory.ObjectType.STORAGE)
        self._proxmox_object(latest, "current-local", cluster=cluster, object_type=ProxmoxInventory.ObjectType.STORAGE)

        prune_scan_history(now=now)

        self.assertTrue(ProxmoxInventory.objects.filter(scan_run=latest, cluster=cluster).exists())
        self.assertTrue(ScanRun.objects.filter(pk=latest.pk).exists())
        # The superseded scan's inventory is still pruned; only the current one stays.
        self.assertFalse(ProxmoxInventory.objects.filter(scan_run=superseded).exists())

    def test_null_timestamp_scan_does_not_displace_file_inventory(self):
        """PostgreSQL DESC must not sort legacy NULL timestamps first."""
        now = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.get_current_timezone())
        storage = StorageMount.objects.create(storage_id="nfs-vm", display_name="nfs-vm", path="/vm")
        file_scan = self._completed_scan(now - timedelta(hours=1))
        legacy_metadata = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        current_file = self._file(file_scan, storage, "images/500/vm-500-disk-0.qcow2")

        result = prune_scan_history(now=now)

        self.assertIn((file_scan.pk, storage.pk), result.kept_file_pairs)
        self.assertTrue(FileInventory.objects.filter(pk=current_file.pk).exists())
        self.assertTrue(ScanRun.objects.filter(pk=legacy_metadata.pk).exists())


class ScanTaskTests(TestCase):
    def test_run_scan_uses_inventory_result_ok_and_audits_completion(self):
        cluster = ProxmoxCluster.objects.create(key="default", display_name="Default cluster", enabled=True)
        ProxmoxEndpoint.objects.create(name="pve1", url="https://pve1.example.test:8006", cluster=cluster)
        StorageMount.objects.create(storage_id="nfs-fs", display_name="nfs-fs", path="/storage")
        scan = ScanRun.objects.create(progress_message="Queued")

        class FakeProxmoxClient:
            def __init__(self, endpoint):
                self.endpoint = endpoint

            def discover_node_name(self, fallback):
                return fallback

            def node_names(self, *, fallback=""):
                return ["pve1"]

            def inventory(self, node):
                return InventoryResult(node=node, ok=True, objects=[], errors=[])

        class EmptyScanner:
            errors = []

            def __init__(self, *args, **kwargs):
                pass

            def iter_entries(self):
                return iter(())

        with (
            patch("core.tasks.client_for_endpoint", lambda ep: FakeProxmoxClient(ep.url)),
            patch("core.tasks._verify_scan_cluster_identities", return_value=set()),
            patch("core.tasks.StorageScanner", EmptyScanner),
            patch("core.tasks.registered_mount_health", return_value=Mock(available=True)),
            patch("core.tasks.ensure_bootstrap"),
            patch("core.tasks._prune_scan_history_after_success"),
        ):
            run_scan(scan.id)

        scan.refresh_from_db()
        self.assertEqual(scan.status, ScanRun.Status.COMPLETED)
        self.assertEqual(scan.endpoints_succeeded, ["pve1"])
        event = AuditEvent.objects.get(action="scan.completed", object_id=str(scan.id))
        self.assertEqual(event.outcome, "success")
        self.assertEqual(event.details["target_label"], "All storages")

    def test_run_scan_covers_endpointless_node_so_its_disk_is_not_orphaned(self):
        """A guest on a node with no endpoint row of its own must still be scanned
        through the cluster's reachable member, so its shared-storage disk is
        referenced and never mis-classified as a likely orphan."""
        cluster = ProxmoxCluster.objects.create(key="clustera", display_name="Cluster A", enabled=True)
        # Only pve1 has an endpoint row; pve2 hosts a live guest but is un-endpointed.
        ProxmoxEndpoint.objects.create(name="pve1", url="https://pve1.example.test:8006", cluster=cluster)
        StorageMount.objects.create(storage_id="nfs-fs", display_name="nfs-fs", path="/storage")
        scan = ScanRun.objects.create(progress_message="Queued")

        disk_volid = "nfs-fs:500/vm-500-disk-0.qcow2"

        class FakeProxmoxClient:
            def __init__(self, url):
                self.url = url

            def discover_node_name(self, fallback):
                return "pve1"

            def node_names(self, *, fallback=""):
                return ["pve1", "pve2"]

            def inventory(self, node):
                if node == "pve2":
                    return InventoryResult(
                        node="pve2",
                        ok=True,
                        objects=[
                            ProxmoxObject(
                                node="pve2",
                                object_type="vm",
                                vmid=500,
                                name="vm-on-pve2",
                                status="running",
                                config={"scsi0": f"{disk_volid},size=32G"},
                                disk_references=[disk_volid],
                            )
                        ],
                        errors=[],
                    )
                return InventoryResult(node=node, ok=True, objects=[], errors=[])

        disk_entry = StorageEntry(
            full_path="/storage/images/500/vm-500-disk-0.qcow2",
            relative_path="images/500/vm-500-disk-0.qcow2",
            entry_type=FileInventory.EntryType.FILE,
            content_category="vm_disk",
            derived_volid=disk_volid,
            size_bytes=1024,
            modified_at=None,
        )

        class SingleDiskScanner:
            errors = []

            def __init__(self, *args, **kwargs):
                pass

            def iter_entries(self):
                return iter((disk_entry,))

        with (
            patch("core.tasks.client_for_endpoint", lambda ep: FakeProxmoxClient(ep.url)),
            patch("core.tasks._verify_scan_cluster_identities", return_value=set()),
            patch("core.tasks.StorageScanner", SingleDiskScanner),
            patch("core.tasks.registered_mount_health", return_value=Mock(available=True)),
            patch("core.tasks.probe_qemu_image_info", return_value=None),
            patch("core.tasks.ensure_bootstrap"),
            patch("core.tasks._prune_scan_history_after_success"),
        ):
            run_scan(scan.id)

        scan.refresh_from_db()
        self.assertEqual(scan.status, ScanRun.Status.COMPLETED)
        # pve2 was reached through pve1's client and is now covered.
        self.assertIn("pve2", scan.endpoints_succeeded)
        row = FileInventory.objects.get(path="images/500/vm-500-disk-0.qcow2")
        self.assertNotEqual(row.classification, FileInventory.Classification.LIKELY_ORPHAN)


class AuditEventTests(TestCase):
    def test_populates_filter_columns_from_details(self):
        event = AuditEvent.objects.create(
            username="viewer",
            action="file.inflated",
            object_type="file",
            object_id="nfs-vm:images/501/vm-501-disk-0.qcow2",
            details={
                "storage_id": "nfs-vm",
                "path": "images/501/vm-501-disk-0.qcow2",
                "target_preallocation": "full",
            },
        )

        self.assertEqual(event.storage_id, "nfs-vm")
        self.assertEqual(event.path, "images/501/vm-501-disk-0.qcow2")
        self.assertEqual(event.target_preallocation, "full")
        self.assertTrue(
            AuditEvent.objects.filter(
                storage_id="nfs-vm",
                path="images/501/vm-501-disk-0.qcow2",
                target_preallocation="full",
            ).exists()
        )

    def test_poll_guest_audit_task_marks_running_event_completed(self):
        cluster = ProxmoxCluster.objects.create(key="default", display_name="Default", enabled=True)
        event = AuditEvent.objects.create(
            cluster=cluster,
            username="operator",
            action="guest.power.start",
            object_type="guest",
            object_id="vm:500",
            outcome="running",
            details={
                "node": "pve1",
                "vmid": 500,
                "target_type": "vm",
                "name": "Lab VM",
                "proxmox_task_upid": "UPID:pve1:start:500:root@pam:",
                "proxmox_task_node": "pve1",
            },
        )

        client = Mock()
        with (
            patch(
                "core.tasks.client_for_audit_event",
                return_value=(client, GuestRef("default", "vm", 500, "pve1"), cluster),
            ),
            patch("core.tasks.refresh_current_guest_from_client") as refresh_projection,
        ):
            client.wait_for_task.return_value = ProxmoxTaskResult(
                node="pve1",
                upid="UPID:pve1:start:500:root@pam:",
                status="stopped",
                exitstatus="OK",
                raw={"status": "stopped", "exitstatus": "OK"},
            )
            refresh_projection.return_value.error = ""

            poll_guest_audit_task(
                event.id,
                "https://pve1.example.com:8006",
                "pve1",
                "UPID:pve1:start:500:root@pam:",
                30,
            )

        event.refresh_from_db()
        self.assertEqual(event.outcome, "success")
        self.assertEqual(event.details["proxmox_task"]["exitstatus"], "OK")
        self.assertIn("finished_at", event.details)
        refresh_projection.assert_called_once_with(
            client,
            node="pve1",
            object_type="vm",
            vmid=500,
            cluster=cluster,
            allow_relocation=False,
            delete_if_authoritatively_absent=False,
        )


class GuestModuleBoundaryTests(SimpleTestCase):
    def _source(self, relative_path: str) -> str:
        return (Path(settings.BASE_DIR) / relative_path).read_text(encoding="utf-8")

    def test_guest_core_does_not_reabsorb_extracted_ownership(self):
        source = self._source("core/views/guests/_core.py")

        self.assertNotIn("def global_search(", source)
        self.assertNotIn("def _resolve_guest_detail(", source)
        self.assertNotIn("def _audit_guest(", source)
        self.assertNotIn("def _parse_net_value(", source)

    def test_guest_facade_has_no_dynamic_private_exports(self):
        source = self._source("core/views/guests/__init__.py")

        self.assertNotIn("_surface_private", source)
        self.assertNotIn("globals().update", source)

    def test_no_template_hand_versions_a_static_url(self):
        """`{% static %}` already busts the cache; a `?v=` suffix after it does not.

        `CompressedManifestStaticFilesStorage` emits content-hashed filenames, so a
        hand-maintained suffix changes nothing and has to be remembered forever. The
        failure mode this guards against is quiet: a stale asset looks like a caching
        problem, someone bumps a `?v=` that was never doing anything, the real cause
        (an unbuilt image, an unrun collectstatic) stays hidden, and the suffixes drift
        apart — as twenty of them had, into seven different values.

        Under DEBUG the tag does return unhashed names and a suffix would matter, but
        no deployed configuration runs that way and a developer who does needs a hard
        reload for templates and Python regardless.
        """
        offenders = [
            f"{path.relative_to(settings.BASE_DIR)}: {match.group(0)}"
            for path in Path(settings.BASE_DIR).joinpath("templates").rglob("*.html")
            for match in re.finditer(r"\{%\s*static\s[^%]*%\}\?v=[\w.-]*", path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])

    def test_soft_navigation_breaks_the_previous_feature_import_cycle(self):
        navigation = self._source("static/js/app/navigation.js")
        scheduling = self._source("static/js/app/scheduling.js")

        self.assertNotIn('from "./guest-actions.js"', navigation)
        self.assertNotIn('from "./guest-actions.js"', scheduling)


class StartupCheckTests(SimpleTestCase):
    def test_production_startup_checks_are_silent_in_debug(self):
        with override_settings(DEBUG=True, SECRET_KEY="dev-insecure-change-me"):
            self.assertEqual(production_startup_errors(), [])

    def test_production_startup_checks_reject_insecure_defaults(self):
        with override_settings(
            DEBUG=False,
            SECRET_KEY="dev-insecure-change-me",
            ALLOWED_HOSTS=["*"],
            APP_BASE_URL="http://localhost:21080",
            APP_REQUIRE_LOGIN=True,
            OIDC_RP_CLIENT_ID="",
            OIDC_RP_CLIENT_SECRET="",
        ):
            errors = production_startup_errors()

        self.assertEqual(
            {error.id for error in errors},
            {
                "pve_helper.E001",
                "pve_helper.E003",
                "pve_helper.E005",
                "pve_helper.E006",
                "pve_helper.E007",
            },
        )

    def test_production_startup_checks_accept_configured_deploy(self):
        with override_settings(
            DEBUG=False,
            SECRET_KEY="not-the-dev-secret",
            ALLOWED_HOSTS=["pve-helper.example.net"],
            APP_BASE_URL="https://pve-helper.example.net",
            APP_REQUIRE_LOGIN=True,
            OIDC_RP_CLIENT_ID="pve-helper",
            OIDC_RP_CLIENT_SECRET="secret",
        ):
            self.assertEqual(production_startup_errors(), [])

    def test_production_startup_checks_accept_direct_http_deploy(self):
        with override_settings(
            DEBUG=False,
            SECRET_KEY="not-the-dev-secret",
            ALLOWED_HOSTS=["pve-helper.internal"],
            APP_BASE_URL="http://pve-helper.internal:21080",
            APP_REQUIRE_LOGIN=False,
        ):
            self.assertEqual(production_startup_errors(), [])

    def test_production_startup_checks_require_real_upload_temp_storage_for_writes(self):
        with override_settings(
            DEBUG=False,
            SECRET_KEY="not-the-dev-secret",
            ALLOWED_HOSTS=["pve-helper.internal"],
            APP_BASE_URL="http://pve-helper.internal:21080",
            APP_REQUIRE_LOGIN=False,
            STORAGE_WRITE_ENABLED=True,
            FILE_UPLOAD_TEMP_DIR=None,
        ):
            self.assertEqual(
                {error.id for error in production_startup_errors()},
                {"pve_helper.E008"},
            )

    def test_production_startup_checks_reject_an_empty_required_group(self):
        with override_settings(
            DEBUG=False,
            SECRET_KEY="not-the-dev-secret",
            ALLOWED_HOSTS=["pve-helper.internal"],
            APP_BASE_URL="http://pve-helper.internal:21080",
            APP_REQUIRE_LOGIN=True,
            OIDC_RP_CLIENT_ID="client",
            OIDC_RP_CLIENT_SECRET="secret",
            OIDC_REQUIRED_GROUP="",
        ):
            self.assertEqual(
                {error.id for error in production_startup_errors()},
                {"pve_helper.E011"},
            )

    def test_production_startup_checks_accept_the_any_authenticated_user_sentinel(self):
        with override_settings(
            DEBUG=False,
            SECRET_KEY="not-the-dev-secret",
            ALLOWED_HOSTS=["pve-helper.internal"],
            APP_BASE_URL="http://pve-helper.internal:21080",
            APP_REQUIRE_LOGIN=True,
            OIDC_RP_CLIENT_ID="client",
            OIDC_RP_CLIENT_SECRET="secret",
            OIDC_REQUIRED_GROUP=settings.OIDC_ANY_AUTHENTICATED_USER,
        ):
            self.assertEqual(production_startup_errors(), [])

    def test_production_startup_checks_reject_unsupported_external_scheme(self):
        with override_settings(
            DEBUG=False,
            SECRET_KEY="not-the-dev-secret",
            ALLOWED_HOSTS=["pve-helper.internal"],
            APP_BASE_URL="ftp://pve-helper.internal",
            APP_REQUIRE_LOGIN=False,
        ):
            self.assertEqual(
                {error.id for error in production_startup_errors()},
                {"pve_helper.E004"},
            )

    def test_external_url_scheme_controls_secure_cookie_policy(self):
        self.assertFalse(external_url_uses_https("http://pve-helper.internal:21080"))
        self.assertTrue(external_url_uses_https("https://pve-helper.example.net"))


class SettingsFallbackTests(SimpleTestCase):
    """What the settings module decides when the environment says nothing.

    Compose supplies every one of these in the shipped stacks, so the fallbacks are
    reached only when something is missing — a bare `manage.py`, a hand-written
    Compose file, a new service that copied an incomplete environment block. That is
    exactly when a fail-open default does damage, and exactly when no reviewer is
    looking, so the direction of each fallback is asserted here rather than left to
    whichever of the two layers a reader happens to open.
    """

    def _settings_without(self, *names):
        module = importlib.import_module("pve_helper.settings")
        with patch.dict(os.environ, {}, clear=False):
            for name in names:
                os.environ.pop(name, None)
            return importlib.reload(module)

    def tearDown(self):
        # The reload above rebinds module-level names from the current environment;
        # restore the module the rest of the suite (and `django.conf`) is holding.
        importlib.reload(importlib.import_module("pve_helper.settings"))
        super().tearDown()

    def test_debug_is_off_when_the_environment_does_not_say_otherwise(self):
        self.assertFalse(self._settings_without("DEBUG").DEBUG)

    def test_login_is_required_when_the_environment_does_not_say_otherwise(self):
        self.assertTrue(self._settings_without("APP_REQUIRE_LOGIN").APP_REQUIRE_LOGIN)

    def test_the_production_compose_file_never_relies_on_a_debug_fallback(self):
        """DEBUG must be pinned in the released asset, not inherited.

        `production_startup_errors()` returns early under DEBUG, so a production
        deployment that ends up debugging also loses the check that would have
        objected to the default SECRET_KEY and an empty ALLOWED_HOSTS.
        """
        compose = (settings.BASE_DIR / "docker-compose.production.yml").read_text()
        defaults = set(re.findall(r"^\s*DEBUG: \$\{DEBUG:-(\w+)\}", compose, re.MULTILINE))
        self.assertEqual(defaults, {"false"})


class ProcessLocalCacheTests(SimpleTestCase):
    """The conditions that make one cache per process the right choice.

    Nothing here checks that caching works; it checks that the cache stays the
    kind of thing a process may hold alone. A read-through memo of a provider
    response may diverge between workers at the cost of one extra call. A lock,
    a counter or a session may not — those need every process to see the same
    value, and putting one here would fail silently rather than loudly.
    """

    def _modules_using_the_cache(self):
        for path in Path(settings.BASE_DIR).joinpath("core").rglob("*.py"):
            if path.name.startswith("tests"):
                continue
            source = path.read_text(encoding="utf-8")
            if "from django.core.cache import" in source:
                yield path.relative_to(settings.BASE_DIR), source

    def test_the_backend_is_declared_and_outgrows_djangos_default_ceiling(self):
        """An implicit LocMemCache caps at 300 entries and culls a third at a time.

        The per-guest snapshot, agent and HA keys pass 300 in a real fleet well
        inside their TTLs, so the default would evict on volume at a moment that
        has nothing to do with freshness.
        """
        backend = settings.CACHES["default"]
        self.assertEqual(backend["BACKEND"], "django.core.cache.backends.locmem.LocMemCache")
        self.assertGreater(backend["OPTIONS"]["MAX_ENTRIES"], 300)

    def test_sessions_do_not_live_in_a_cache_only_one_process_can_read(self):
        self.assertNotIn("cache", settings.SESSION_ENGINE)

    def test_the_cache_is_never_used_as_a_coordination_primitive(self):
        """`add`, `incr`/`decr` and `get_or_set` are the atomic operations.

        Each one reads as mutual exclusion, a rate limit or a run-once guard,
        and each is atomic only within the process that holds the memory. This
        codebase coordinates through PostgreSQL advisory locks instead
        (`cluster_state_identity.cluster_advisory_lock_id`).
        """
        offenders = [
            f"{path}: {match.group(0)}"
            for path, source in self._modules_using_the_cache()
            for match in re.finditer(r"\bcache\.(add|incr|decr|get_or_set)\b", source)
        ]
        self.assertEqual(offenders, [])

    def test_every_cached_value_is_namespaced_by_its_cluster_generation(self):
        """Divergence is only harmless while a stale copy is unreachable.

        `cluster_cache_key()` folds `cluster.cache_generation` into the key, so a
        writer bumping that column retires the old namespace for every process at
        once — which is why no `cache.delete` is needed and why deleting in one
        worker would not have been enough. A key built any other way would keep a
        stale value alive in whichever process happened to store it.
        """
        offenders = [str(path) for path, source in self._modules_using_the_cache() if "cluster_cache_key" not in source]
        self.assertEqual(offenders, [])


@override_settings(APP_REQUIRE_LOGIN=False)
@override_settings(
    PVE_API_TOKEN_ID="root@pam!test",
    PVE_API_TOKEN_SECRET="test-secret",
)
class ViewSmokeTests(HermeticProxmoxMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.cluster = ProxmoxCluster.objects.create(key="default", display_name="Default cluster", enabled=True)
        registry_patch = patch("core.services.tag_catalog.registered_tags", return_value=({}, ""))
        registry_patch.start()
        self.addCleanup(registry_patch.stop)

    def _live_guest(self, *, object_type="vm", vmid=500, name="Lab VM", node="pve1", status="stopped"):
        CurrentGuestInventory.objects.update_or_create(
            object_type=object_type,
            vmid=vmid,
            defaults={
                "cluster": self.cluster,
                "node": node,
                "name": name,
                "status": status,
                "observed_at": timezone.now(),
                "runtime_observed_at": timezone.now(),
            },
        )
        guest = Mock()
        guest.object_type = object_type
        guest.vmid = vmid
        guest.name = name
        guest.node = node
        guest.status = status
        guest.cluster = self.cluster
        guest.cluster_key = self.cluster.key
        return guest

    def _seed_volume_catalog(self, storage_id: str, rows: list[dict], *, node: str = "pve1") -> None:
        metadata_generation = uuid.uuid4()
        volume_generation = uuid.uuid4()
        definition = ClusterStorage.objects.create(
            cluster=self.cluster,
            storage_id=storage_id,
            storage_type="dir",
            content=["images"],
            shared=False,
            present=True,
            observed_metadata_generation=metadata_generation,
        )
        ClusterStorageNodeState.objects.create(
            cluster_storage=definition,
            node=node,
            active=True,
            enabled=True,
            present=True,
            observed_metadata_generation=metadata_generation,
        )
        ClusterStorageVolumeCoverage.objects.create(
            cluster_storage=definition,
            scope=ClusterStorageVolumeCoverage.Scope.NODE,
            node=node,
            volume_generation=volume_generation,
            based_on_metadata_generation=metadata_generation,
            refreshed_at=timezone.now(),
            last_attempt_at=timezone.now(),
            complete=True,
        )
        for row in rows:
            ClusterStorageVolumeObservation.objects.create(
                cluster_storage=definition,
                node=node,
                volid=row["volid"],
                vmid=row.get("vmid"),
                content="images",
                metadata=row,
                observed_volume_generation=volume_generation,
                based_on_metadata_generation=metadata_generation,
                last_seen_at=timezone.now(),
            )
        StorageCatalogState.objects.create(
            cluster=self.cluster,
            metadata_generation=metadata_generation,
            metadata_complete=True,
            volume_complete=True,
        )

    def _folder_node_tag(self, response, path: str) -> str:
        html = response.content.decode()
        marker = f'data-folder-path="{path}"'
        marker_index = html.index(marker)
        tag_start = html.rfind("<div", 0, marker_index)
        tag_end = html.index(">", marker_index)
        return html[tag_start:tag_end]

    def test_global_search_finds_guests_storage_and_hosts(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        StorageMount.objects.create(
            storage_id="nfs-vm",
            display_name="TrueNAS-VM",
            path="/storages/truenas-vm",
            expected_consumers=["pve1"],
        )
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED, storage_gate_status={"nfs-vm": {"ok": True}})
        ProxmoxInventory.objects.create(
            scan_run=scan,
            node="pve1",
            object_type=ProxmoxInventory.ObjectType.VM,
            vmid=500,
            name="App01",
            status="running",
            config={"agent": 1, "ostype": "l26", "tags": "prod"},
        )
        CurrentGuestInventory.objects.create(
            cluster=self.cluster,
            source_scan=scan,
            node="pve1",
            object_type=CurrentGuestInventory.ObjectType.VM,
            vmid=500,
            name="App01",
            status="running",
            config={"agent": 1, "ostype": "l26", "tags": "prod"},
            observed_at=timezone.now(),
        )
        ProxmoxInventory.objects.create(
            scan_run=scan,
            node="pve1",
            object_type=ProxmoxInventory.ObjectType.STORAGE,
            name="local-lvm",
            config={"type": "lvmthin", "content": "images,rootdir"},
        )
        ProxmoxInventory.objects.create(
            scan_run=scan,
            node="pve1",
            object_type=ProxmoxInventory.ObjectType.NODE,
            name="pve1",
            status="online",
        )
        cache.set(
            cluster_cache_key("guest-agent-summary:v2", self.cluster, "pve1", "vm", 500),
            {
                "enabled": True,
                "running": True,
                "os_pretty_name": "Ubuntu 24.04.4 LTS",
                "hostname": "app01",
                "ips": ["203.0.113.3"],
            },
            60,
        )

        live_guest = self._live_guest(object_type="vm", vmid=500, name="App01", node="pve1", status="running")
        # A mount is searchable through the datastore it is bound to; an unbound
        # one is configuration and has no page to open.
        browser_url("nfs-vm")
        with patch("core.views.common.fetch_live_guest_inventory", return_value=[live_guest]):
            ip_response = self.client.get(reverse("core:global_search"), {"q": "203.0.113.3"})
            storage_response = self.client.get(reverse("core:global_search"), {"q": "TrueNAS"})
            host_response = self.client.get(reverse("core:global_search"), {"q": "pve1"})

        self.assertEqual(ip_response.status_code, 200)
        self.assertIn("App01", [result["label"] for result in ip_response.json()["results"]])
        self.assertIn("TrueNAS-VM", [result["label"] for result in storage_response.json()["results"]])
        self.assertIn("pve1", [result["label"] for result in host_response.json()["results"]])

    def test_storage_views_render(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        storage = StorageMount.objects.create(
            storage_id="nfs-vm",
            display_name="nfs-vm",
            path="/storages/truenas-vm",
            expected_consumers=["pve-node-1"],
        )
        scan = ScanRun.objects.create(
            status=ScanRun.Status.COMPLETED,
            progress_message="Smoke scan",
            storage_gate_status={
                "nfs-vm": {
                    "ok": False,
                    "status": "inventory incomplete",
                    "expected_consumers": ["pve-node-1"],
                    "missing_consumers": ["pve-node-1"],
                }
            },
        )
        FileInventory.objects.create(
            scan_run=scan,
            storage=storage,
            path="images",
            entry_type=FileInventory.EntryType.DIRECTORY,
            content_category="unknown",
            classification=FileInventory.Classification.UNKNOWN,
        )
        FileInventory.objects.create(
            scan_run=scan,
            storage=storage,
            path="images/100",
            entry_type=FileInventory.EntryType.DIRECTORY,
            content_category="unknown",
            classification=FileInventory.Classification.UNKNOWN,
        )
        FileInventory.objects.create(
            scan_run=scan,
            storage=storage,
            path="images/100/vm-100-disk-0.qcow2",
            derived_volid="nfs-vm:100/vm-100-disk-0.qcow2",
            content_category="vm_disk",
            classification=FileInventory.Classification.CLASSIFICATION_BLOCKED,
        )
        ProxmoxInventory.objects.create(
            scan_run=scan,
            node="pve-node-1",
            object_type=ProxmoxInventory.ObjectType.STORAGE,
            name="nfs-vm",
            config={"storage": "nfs-vm", "type": "nfs", "content": "images,iso"},
        )

        for name in ["core:dashboard", "core:orphan_finder"]:
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200)

        response = self.client.get(reverse("core:dashboard"))
        self.assertNotContains(response, "Save")
        content_security_policy = response.headers["Content-Security-Policy"]
        self.assertIn("script-src 'self'", content_security_policy)
        self.assertIn("script-src-attr 'none'", content_security_policy)
        self.assertIn("style-src-attr 'unsafe-inline'", content_security_policy)
        self.assertIn("object-src 'none'", content_security_policy)
        self.assertIn("frame-ancestors 'none'", content_security_policy)
        self.assertContains(response, "data-auto-submit-form")
        # Named per page since Round 9; `tests_page_titles` owns the property.
        self.assertContains(response, "<title>Storage Overview · pve-helper</title>")
        self.assertContains(response, 'rel="icon"')
        self.assertNotContains(response, "pve-helper.example.com")
        self.assertContains(response, "data-soft-nav-content")
        self.assertContains(response, "data-soft-nav-tree")
        self.assertContains(response, "data-global-search")
        self.assertContains(response, "Storage gate")
        self.assertContains(response, "protects orphan classification")

        response = self.client.get(reverse("core:orphan_finder"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Classification legend")
        self.assertContains(response, "Likely orphan")
        self.assertContains(response, "all expected consumers were scanned")

        response = self.client.get(browser_url("nfs-vm"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "images")
        self.assertContains(response, "VM images")
        self.assertContains(response, "Unknown")
        self.assertContains(response, "Start scan")
        self.assertContains(response, "Classification is conservative")

        response = self.client.get(browser_url("nfs-vm"), {"path": "images/100"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "vm-100-disk-0.qcow2")

        response = self.client.get(browser_url("nfs-vm").replace("/files/", "/nodes/"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, ">Nodes<")

        with patch("core.views.storage.common.cluster_scoped_clients", return_value=[]):
            response = self.client.get(browser_url("nfs-vm").replace("/files/", "/content/"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Content Types")
        self.assertContains(response, "Disk image")
        self.assertContains(response, "Container template")

    def test_classified_files_lists_and_links_to_folder(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        storage = StorageMount.objects.create(
            storage_id="nfs-vm",
            display_name="nfs-vm",
            path="/storages/truenas-vm",
            expected_consumers=["pve-node-1"],
        )
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        FileInventory.objects.create(
            scan_run=scan,
            storage=storage,
            path="images/cirros-0.6.2-x86_64-disk.img",
            entry_type=FileInventory.EntryType.FILE,
            content_category="vm_image_directory",
            classification=FileInventory.Classification.UNKNOWN,
        )

        # Unknown drill-down lists the file and links to its containing folder.
        # Resolved first: the link is the mount's datastore page, so the binding
        # has to exist before the page renders.
        expected_browser_url = browser_url(storage.mount_ref)
        response = self.client.get(reverse("core:classified_files"), {"classification": "unknown"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cirros-0.6.2-x86_64-disk.img")
        self.assertContains(response, f'href="{expected_browser_url}?path=images"')

        # Likely orphans have their own workspace.
        response = self.client.get(reverse("core:classified_files"), {"classification": "likely_orphan"})
        self.assertRedirects(response, reverse("core:orphan_finder"))

        # Unknown classifications bounce back to the dashboard.
        response = self.client.get(reverse("core:classified_files"), {"classification": "bogus"})
        self.assertRedirects(response, reverse("core:dashboard"))

    def test_storage_folders_api_lists_directories_for_dest_picker(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        storage = StorageMount.objects.create(
            storage_id="nfs-vm",
            display_name="nfs-vm",
            path="/storages/truenas-vm",
            expected_consumers=["pve-node-1"],
        )
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        for path, kind in [
            ("images", FileInventory.EntryType.DIRECTORY),
            ("images/100", FileInventory.EntryType.DIRECTORY),
            ("template", FileInventory.EntryType.DIRECTORY),
            (".trash", FileInventory.EntryType.DIRECTORY),
            ("images/100/vm-100-disk-0.qcow2", FileInventory.EntryType.FILE),
        ]:
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path=path,
                entry_type=kind,
                content_category="unknown",
                classification=FileInventory.Classification.UNKNOWN,
            )

        response = self.client.get(reverse("core:storage_folders", args=["nfs-vm"]))
        self.assertEqual(response.status_code, 200)
        folders = response.json()["folders"]
        self.assertIn("images", folders)
        self.assertIn("images/100", folders)
        self.assertIn("template", folders)
        # Internal / trash directories and files are excluded.
        self.assertNotIn(".trash", folders)
        self.assertNotIn("images/100/vm-100-disk-0.qcow2", folders)

    def test_storage_content_update_blocks_used_content_type(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)
        storage = StorageMount.objects.create(
            storage_id="nfs-vm",
            display_name="nfs-vm",
            path="/storages/truenas-vm",
        )
        ProxmoxStorageConsumer.objects.create(
            storage=storage,
            cluster=self.cluster,
            expected_node_name="pve1",
        )
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        ProxmoxInventory.objects.create(
            cluster=self.cluster,
            scan_run=scan,
            node="pve1",
            object_type=ProxmoxInventory.ObjectType.STORAGE,
            name="nfs-vm",
            config={"storage": "nfs-vm", "type": "nfs", "content": "images,iso"},
        )
        FileInventory.objects.create(
            scan_run=scan,
            storage=storage,
            path="images/500/vm-500-disk-0.qcow2",
            entry_type=FileInventory.EntryType.FILE,
            content_category="vm_disk",
        )
        fake_client = Mock()
        fake_client.storage_config.return_value = {"content": "images,iso"}

        with (
            patch("core.views.storage._run_storage_content_preflight_scan", return_value=scan),
            patch("core.views.storage.common.cluster_scoped_clients", return_value=[fake_client]),
        ):
            response = self.client.post(
                reverse("core:storage_content_update", args=["nfs-vm"]),
                {"cluster": self.cluster.key, "content": ["iso"]},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        fake_client.set_storage_content.assert_not_called()
        self.assertFalse(AuditEvent.objects.filter(action="storage.content.updated").exists())
        self.assertContains(response, "Cannot disable Disk image")
        self.assertContains(response, "images/500/vm-500-disk-0.qcow2")

    def test_storage_content_update_saves_unused_content_types(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)
        storage = StorageMount.objects.create(
            storage_id="nfs-vm",
            display_name="nfs-vm",
            path="/storages/truenas-vm",
        )
        ProxmoxStorageConsumer.objects.create(
            storage=storage,
            cluster=self.cluster,
            expected_node_name="pve1",
        )
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        storage_inventory = ProxmoxInventory.objects.create(
            cluster=self.cluster,
            scan_run=scan,
            node="pve1",
            object_type=ProxmoxInventory.ObjectType.STORAGE,
            name="nfs-vm",
            config={"storage": "nfs-vm", "type": "nfs", "content": "images,iso,backup"},
        )
        fake_client = Mock()
        fake_client.storage_config.return_value = {"content": "images,iso,backup"}

        with (
            patch("core.views.storage._run_storage_content_preflight_scan", return_value=scan),
            patch("core.views.storage.common.cluster_scoped_clients", return_value=[fake_client]),
        ):
            response = self.client.post(
                reverse("core:storage_content_update", args=["nfs-vm"]),
                {"cluster": self.cluster.key, "content": ["images", "iso"]},
            )

        self.assertEqual(response.status_code, 302)
        fake_client.set_storage_content.assert_called_once_with("nfs-vm", ["images", "iso"])
        event = AuditEvent.objects.get(action="storage.content.updated")
        self.assertEqual(event.details["old_content"], ["images", "iso", "backup"])
        self.assertEqual(event.details["new_content"], ["images", "iso"])
        storage_inventory.refresh_from_db()
        self.assertEqual(storage_inventory.config["content"], "images,iso")

    def test_storage_content_update_scans_before_checking_usage(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)
        fake_client = Mock()

        with TemporaryDirectory() as tmp:
            iso_dir = Path(tmp) / "template" / "iso"
            iso_dir.mkdir(parents=True)
            (iso_dir / "hidden.iso").write_text("x", encoding="utf-8")
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=tmp,
            )
            ProxmoxStorageConsumer.objects.create(
                storage=storage,
                cluster=self.cluster,
                expected_node_name="pve1",
            )
            old_scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
            ProxmoxInventory.objects.create(
                cluster=self.cluster,
                scan_run=old_scan,
                node="pve1",
                object_type=ProxmoxInventory.ObjectType.STORAGE,
                name="nfs-vm",
                config={"storage": "nfs-vm", "type": "nfs", "content": "images,iso"},
            )
            fake_client.storage_config.return_value = {"content": "images,iso"}

            with (
                patch("core.tasks.ensure_bootstrap"),
                patch("core.views.storage.common.cluster_scoped_clients", return_value=[fake_client]),
            ):
                response = self.client.post(
                    reverse("core:storage_content_update", args=["nfs-vm"]),
                    {"cluster": self.cluster.key, "content": ["images"]},
                    follow=True,
                )

        self.assertEqual(response.status_code, 200)
        fake_client.set_storage_content.assert_not_called()
        self.assertContains(response, "Cannot disable ISO image")
        self.assertContains(response, "template/iso/hidden.iso")
        self.assertTrue(
            FileInventory.objects.filter(
                scan_run__target_storage=storage,
                path="template/iso/hidden.iso",
                content_category="iso",
            ).exists()
        )

    def test_storage_monitor_uses_scheduled_space_points_and_recent_activity(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)
        with TemporaryDirectory() as tmp:
            storage = StorageMount.objects.create(
                storage_id="nfs-fs",
                display_name="nfs-fs",
                path=tmp,
            )
            now = timezone.now()
            for index in range(18):
                StorageSpaceSnapshot.objects.create(
                    storage=storage,
                    recorded_at=now - timedelta(hours=12 * index),
                    total_bytes=1000,
                    available_bytes=500 + index,
                    used_bytes=500 - index,
                )

            recent_scan = ScanRun.objects.create(
                status=ScanRun.Status.COMPLETED,
                started_at=now,
                finished_at=now,
                progress_message="Recent scan",
                target_storage=storage,
            )
            global_scan = ScanRun.objects.create(
                status=ScanRun.Status.COMPLETED,
                started_at=now,
                finished_at=now,
                progress_message="Global scan shown in Recent Tasks",
            )
            old_scan = ScanRun.objects.create(
                status=ScanRun.Status.COMPLETED,
                started_at=now - timedelta(days=9),
                finished_at=now - timedelta(days=9),
                progress_message="Old scan",
                target_storage=storage,
            )
            ScanRun.objects.filter(pk=old_scan.pk).update(created_at=now - timedelta(days=9))
            ScanRun.objects.filter(pk=recent_scan.pk).update(created_at=now - timedelta(days=1))
            ScanRun.objects.filter(pk=global_scan.pk).update(created_at=now - timedelta(days=1))

            recent_event = AuditEvent.objects.create(
                username="viewer",
                action="file.upload_normalized",
                object_type="file",
                object_id="nfs-fs:images/100/vm-100-disk-0.qcow2",
                details={"storage_id": storage.storage_id, "path": "images/100/vm-100-disk-0.qcow2"},
            )
            old_event = AuditEvent.objects.create(
                username="viewer",
                action="file.downloaded",
                object_type="file",
                object_id="nfs-fs:old.iso",
                details={"storage_id": storage.storage_id, "path": "old.iso"},
            )
            AuditEvent.objects.filter(pk=recent_event.pk).update(timestamp=now - timedelta(days=1))
            AuditEvent.objects.filter(pk=old_event.pk).update(timestamp=now - timedelta(days=9))
            Schedule.objects.filter(name=SPACE_SNAPSHOT_SCHEDULE_NAME).delete()

            monitor_url = browser_url(storage.mount_ref).replace("/files/", "/monitor/")
            response = self.client.get(monitor_url)
            invalid_page_response = self.client.get(
                monitor_url,
                {"scan_page": "abc", "event_page": "abc"},
            )

        self.assertEqual(response.status_code, 200)
        chart_data = json.loads(response.context["space_chart_data_json"])
        self.assertLessEqual(len(chart_data), 14)
        self.assertGreater(len(chart_data), 0)
        self.assertContains(response, "File Actions (last 7 days)")
        self.assertContains(response, "Recent Scans (last 7 days)")
        self.assertContains(response, "Proxmox does not expose per-datastore performance metrics here")
        self.assertLess(
            response.content.decode().index("File Actions (last 7 days)"),
            response.content.decode().index("Recent Scans (last 7 days)"),
        )
        self.assertContains(response, "Normalize uploaded disk metadata")
        self.assertNotContains(response, "file.upload_normalized")
        self.assertContains(response, "Recent scan")
        self.assertNotIn(global_scan, response.context["recent_scans"])
        self.assertContains(response, "Global scan shown in Recent Tasks")
        self.assertNotContains(response, "Old scan")
        self.assertNotContains(response, "old.iso")
        self.assertEqual(invalid_page_response.status_code, 200)
        self.assertEqual(invalid_page_response.context["scan_page"], 0)
        self.assertEqual(invalid_page_response.context["event_page"], 0)
        self.assertContains(invalid_page_response, "Recent scan")
        self.assertFalse(Schedule.objects.filter(name=SPACE_SNAPSHOT_SCHEDULE_NAME).exists())

    def test_post_migrate_bootstraps_space_snapshot_schedule(self):
        ensure_always_on_schedules(sender=None, app_config=apps.get_app_config("core"))

        schedule = Schedule.objects.get(name=SPACE_SNAPSHOT_SCHEDULE_NAME)
        self.assertEqual(schedule.func, "core.tasks.record_storage_space_snapshots")
        self.assertEqual(schedule.schedule_type, Schedule.MINUTES)
        self.assertEqual(schedule.minutes, SPACE_SNAPSHOT_INTERVAL_MINUTES)
        dispatcher = Schedule.objects.get(name=SCHEDULED_ACTION_DISPATCH_SCHEDULE_NAME)
        self.assertEqual(dispatcher.func, SCHEDULED_ACTION_DISPATCH_FUNC)
        self.assertEqual(dispatcher.schedule_type, Schedule.MINUTES)
        self.assertEqual(dispatcher.minutes, SCHEDULED_ACTION_DISPATCH_INTERVAL_MINUTES)
        guest_refresh = Schedule.objects.get(name=GUEST_INVENTORY_REFRESH_SCHEDULE_NAME)
        self.assertEqual(guest_refresh.func, "core.tasks.refresh_current_guest_inventory")
        self.assertEqual(guest_refresh.minutes, settings.CURRENT_GUEST_REFRESH_INTERVAL_MINUTES)

    def test_storage_vms_uses_non_blocking_current_status_projection(self):
        cache.clear()
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)
        storage = StorageMount.objects.create(
            storage_id="nfs-vm",
            display_name="nfs-vm",
            path="/storages/truenas-vm",
        )
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED, storage_gate_status={"nfs-vm": {"ok": True}})
        ProxmoxInventory.objects.create(
            cluster=self.cluster,
            scan_run=scan,
            node="pve-node-1",
            object_type=ProxmoxInventory.ObjectType.VM,
            vmid=100,
            name="Test VM",
            status="stopped",
            disk_references=["nfs-vm:images/100/vm-100-disk-0.qcow2"],
        )
        CurrentGuestInventory.objects.create(
            cluster=self.cluster,
            source_scan=scan,
            node="pve-node-1",
            object_type=CurrentGuestInventory.ObjectType.VM,
            vmid=100,
            name="Test VM",
            status="stopped",
            runtime_observed_at=timezone.now(),
            disk_references=["nfs-vm:images/100/vm-100-disk-0.qcow2"],
            observed_at=timezone.now(),
        )
        ScheduledAction.objects.create(
            cluster=self.cluster,
            name="Night shutdown",
            action_type=ScheduledAction.ActionType.SHUTDOWN,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=100,
            target_name_snapshot="Test VM",
            target_node="pve-node-1",
            last_status=ScheduledAction.LastStatus.NEVER_RUN,
        )

        with patch(
            "core.views.common.fetch_live_guest_status",
            side_effect=AssertionError("storage views must not perform provider status reads"),
        ):
            vms_url = browser_url(storage.mount_ref).replace("/files/", "/vms/")
            response = self.client.get(vms_url)
            second_response = self.client.get(vms_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertContains(response, "Power state updates immediately after operations")
        self.assertContains(response, "stopped")
        # Scheduling belongs to the guest views; the storage tab only answers
        # "which guests consume this datastore".
        self.assertNotContains(response, "<th>Scheduled Tasks</th>")
        self.assertNotContains(response, "Night shutdown")
        self.assertNotContains(response, "target=gr1%3Adefault%3Avm%3A100")
        self.assertContains(second_response, "stopped")
        cache.clear()

    def test_live_guest_status_fetch_uses_short_cluster_resources_call(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            def get(self, path, *, timeout=None):
                self.calls.append((path, timeout))
                if path == "cluster/resources?type=vm":
                    return [
                        {"type": "qemu", "vmid": 500, "node": "pve1", "name": "Lab VM", "status": "running"},
                        {"type": "lxc", "vmid": 601, "node": "pve1", "name": "Lab CT", "status": "stopped"},
                    ]
                raise ProxmoxAPIError(path)

        fake_client = self._patch_provider_client(FakeClient())
        with patch("core.services.cluster_resolver.client_for_endpoint", return_value=fake_client):
            statuses = _fetch_live_guest_status_uncached(cluster=self.cluster)

        self.assertEqual(
            statuses,
            {
                ("pve1", "vm", 500): "running",
                ("pve1", "ct", 601): "stopped",
            },
        )
        self.assertEqual(len(fake_client.calls), 1)
        self.assertEqual(fake_client.calls[0][0], "cluster/resources?type=vm")
        self.assertIsNotNone(fake_client.calls[0][1])
        self.assertLessEqual(fake_client.calls[0][1], LIVE_GUEST_DISPLAY_TIMEOUT_SECONDS)

    def test_storage_browser_shows_virtual_and_disk_size_when_image_info_exists(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        storage = StorageMount.objects.create(
            storage_id="nfs-vm",
            display_name="nfs-vm",
            path="/storages/truenas-vm",
            expected_consumers=["pve-node-1"],
        )
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        FileInventory.objects.create(
            scan_run=scan,
            storage=storage,
            path="images",
            entry_type=FileInventory.EntryType.DIRECTORY,
            content_category="unknown",
            classification=FileInventory.Classification.UNKNOWN,
        )
        FileInventory.objects.create(
            scan_run=scan,
            storage=storage,
            path="images/501",
            entry_type=FileInventory.EntryType.DIRECTORY,
            content_category="unknown",
            classification=FileInventory.Classification.UNKNOWN,
        )
        FileInventory.objects.create(
            scan_run=scan,
            storage=storage,
            path="images/501/vm-501-disk-0.qcow2",
            entry_type=FileInventory.EntryType.FILE,
            content_category="vm_disk",
            classification=FileInventory.Classification.REFERENCED,
            size_bytes=10 * 1024**3,
            evidence={
                "image_info": {
                    "format": "qcow2",
                    "virtual_size_bytes": 10 * 1024**3,
                    "disk_size_bytes": 1024**3,
                    "qcow2_allocated_clusters": 100,
                    "qcow2_total_clusters": 1000,
                    "qcow2_allocation_percent": 10.0,
                }
            },
        )

        response = self.client.get(browser_url("nfs-vm"), {"path": "images/501"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Virtual Size")
        self.assertContains(response, "Backend Used")
        self.assertContains(response, "QCOW2 Map")
        self.assertContains(response, "10.0\xa0GB")
        self.assertContains(response, "1.0\xa0GB")
        self.assertContains(response, "10%")
        self.assertContains(response, 'data-can-inflate="true"')
        self.assertContains(response, 'data-can-inflate-metadata="true"')
        self.assertContains(response, 'data-can-inflate-full="true"')
        self.assertContains(response, "Inflate Metadata")
        self.assertContains(response, "Inflate Full")
        self.assertContains(response, "Metadata preallocation allocates the QCOW2 map")
        self.assertContains(response, "Full preallocation writes out the whole virtual disk")

    def test_storage_browser_allows_full_inflate_for_metadata_mapped_qcow2(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        storage = StorageMount.objects.create(
            storage_id="nfs-vm",
            display_name="nfs-vm",
            path="/storages/truenas-vm",
            expected_consumers=["pve-node-1"],
        )
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        FileInventory.objects.create(
            scan_run=scan,
            storage=storage,
            path="images",
            entry_type=FileInventory.EntryType.DIRECTORY,
            content_category="unknown",
            classification=FileInventory.Classification.UNKNOWN,
        )
        FileInventory.objects.create(
            scan_run=scan,
            storage=storage,
            path="images/501",
            entry_type=FileInventory.EntryType.DIRECTORY,
            content_category="unknown",
            classification=FileInventory.Classification.UNKNOWN,
        )
        FileInventory.objects.create(
            scan_run=scan,
            storage=storage,
            path="images/501/vm-501-disk-0.qcow2",
            entry_type=FileInventory.EntryType.FILE,
            content_category="vm_disk",
            classification=FileInventory.Classification.REFERENCED,
            evidence={
                "image_info": {
                    "format": "qcow2",
                    "virtual_size_bytes": 10 * 1024**3,
                    "disk_size_bytes": 1024**3,
                    "qcow2_allocated_clusters": 1000,
                    "qcow2_total_clusters": 1000,
                    "qcow2_allocation_percent": 100.0,
                }
            },
        )

        response = self.client.get(browser_url("nfs-vm"), {"path": "images/501"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-can-inflate-metadata="false"')
        self.assertContains(response, 'data-can-inflate-full="true"')

    def test_storage_browser_blocks_repeated_full_inflate(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        storage = StorageMount.objects.create(
            storage_id="nfs-vm",
            display_name="nfs-vm",
            path="/storages/truenas-vm",
            expected_consumers=["pve-node-1"],
        )
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        FileInventory.objects.create(
            scan_run=scan,
            storage=storage,
            path="images",
            entry_type=FileInventory.EntryType.DIRECTORY,
            content_category="unknown",
            classification=FileInventory.Classification.UNKNOWN,
        )
        FileInventory.objects.create(
            scan_run=scan,
            storage=storage,
            path="images/501",
            entry_type=FileInventory.EntryType.DIRECTORY,
            content_category="unknown",
            classification=FileInventory.Classification.UNKNOWN,
        )
        FileInventory.objects.create(
            scan_run=scan,
            storage=storage,
            path="images/501/vm-501-disk-0.qcow2",
            entry_type=FileInventory.EntryType.FILE,
            content_category="vm_disk",
            classification=FileInventory.Classification.REFERENCED,
            evidence={
                "image_info": {
                    "format": "qcow2",
                    "virtual_size_bytes": 10 * 1024**3,
                    "disk_size_bytes": 1024**3,
                    "qcow2_allocated_clusters": 1000,
                    "qcow2_total_clusters": 1000,
                    "qcow2_allocation_percent": 100.0,
                }
            },
        )
        AuditEvent.objects.create(
            username="viewer",
            action="file.inflated",
            object_type="file",
            object_id="nfs-vm:images/501/vm-501-disk-0.qcow2",
            details={
                "storage_id": "nfs-vm",
                "path": "images/501/vm-501-disk-0.qcow2",
                "target_preallocation": "full",
            },
        )

        response = self.client.get(browser_url("nfs-vm"), {"path": "images/501"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-can-inflate-metadata="false"')
        self.assertContains(response, 'data-can-inflate-full="false"')

    def test_storage_browser_allows_inflate_when_scan_status_is_stale_running(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        storage = StorageMount.objects.create(
            storage_id="nfs-vm",
            display_name="nfs-vm",
            path="/storages/truenas-vm",
            expected_consumers=["pve-node-1"],
        )
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        ProxmoxInventory.objects.create(
            scan_run=scan,
            node="pve-node-1",
            object_type=ProxmoxInventory.ObjectType.VM,
            vmid=505,
            name="stale-running-test",
            status="running",
            disk_references=["nfs-vm:505/vm-505-disk-0.qcow2"],
        )
        # The datastore this mount belongs to is bound to a cluster, so the
        # reference read is the current guest inventory rather than the scan
        # snapshot. "Stale" here is that inventory still saying running.
        CurrentGuestInventory.objects.create(
            cluster=self.cluster,
            source_scan=scan,
            node="pve-node-1",
            object_type=CurrentGuestInventory.ObjectType.VM,
            vmid=505,
            name="stale-running-test",
            status="running",
            runtime_observed_at=timezone.now(),
            disk_references=["nfs-vm:505/vm-505-disk-0.qcow2"],
            observed_at=timezone.now(),
        )
        FileInventory.objects.create(
            scan_run=scan,
            storage=storage,
            path="images",
            entry_type=FileInventory.EntryType.DIRECTORY,
            content_category="unknown",
            classification=FileInventory.Classification.UNKNOWN,
        )
        FileInventory.objects.create(
            scan_run=scan,
            storage=storage,
            path="images/505",
            entry_type=FileInventory.EntryType.DIRECTORY,
            content_category="unknown",
            classification=FileInventory.Classification.UNKNOWN,
        )
        FileInventory.objects.create(
            scan_run=scan,
            storage=storage,
            path="images/505/vm-505-disk-0.qcow2",
            derived_volid="nfs-vm:505/vm-505-disk-0.qcow2",
            entry_type=FileInventory.EntryType.FILE,
            content_category="vm_disk",
            classification=FileInventory.Classification.REFERENCED,
            evidence={
                "image_info": {
                    "format": "qcow2",
                    "virtual_size_bytes": 40 * 1024**3,
                    "disk_size_bytes": 2 * 1024**3,
                    "qcow2_allocated_clusters": 55000,
                    "qcow2_total_clusters": 655360,
                    "qcow2_allocation_percent": 8.4,
                }
            },
        )

        response = self.client.get(browser_url("nfs-vm"), {"path": "images/505"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-can-action="false"')
        self.assertContains(response, 'data-can-inflate-metadata="true"')
        self.assertContains(response, 'data-can-inflate-full="true"')
        self.assertContains(response, 'data-inflate-requires-risk-confirmation="true"')

    @override_settings(STORAGE_WRITE_ENABLED=True, STORAGE_INFLATE_TIMEOUT_SECONDS=60)
    def test_qcow2_disk_can_be_inflated(self):
        if not shutil.which("qemu-img"):
            self.skipTest("qemu-img is not available")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_dir = root / "images" / "501"
            image_dir.mkdir(parents=True)
            disk = image_dir / "vm-501-disk-0.qcow2"
            subprocess.run(
                ["qemu-img", "create", "-f", "qcow2", disk.as_posix(), "16M"],
                check=True,
                capture_output=True,
                text=True,
            )
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            ProxmoxEndpoint.objects.create(
                cluster=self.cluster,
                name="pve-node-1",
                url="https://pve-node-1.example.com:8006",
                enabled=True,
                details={"node": "pve-node-1"},
            )
            scan = ScanRun.objects.create(
                status=ScanRun.Status.COMPLETED,
                storage_gate_status={"nfs-vm": {"ok": True, "status": "ok"}},
            )
            ProxmoxInventory.objects.create(
                scan_run=scan,
                node="pve-node-1",
                object_type=ProxmoxInventory.ObjectType.VM,
                vmid=501,
                name="inflate-test",
                cluster=self.cluster,
                status="stopped",
                disk_references=["nfs-vm:501/vm-501-disk-0.qcow2"],
            )
            entry = FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="images/501/vm-501-disk-0.qcow2",
                derived_volid="nfs-vm:501/vm-501-disk-0.qcow2",
                entry_type=FileInventory.EntryType.FILE,
                content_category="vm_disk",
                classification=FileInventory.Classification.REFERENCED,
            )

            original_stat = disk.stat()
            with (
                patch("core.services.proxmox.ProxmoxClient.guest_status", return_value="stopped"),
                patch("core.services.storage_catalog.refresh_storage_catalog"),
                patch("core.services.confined_filesystem.os.fchown") as chown_mock,
                patch("core.services.confined_filesystem.os.fchmod") as chmod_mock,
            ):
                result = inflate_storage_file(storage=storage, entry=entry)

            self.assertTrue(disk.exists())
            self.assertEqual(result["path"], "images/501/vm-501-disk-0.qcow2")
            self.assertEqual(result["target_preallocation"], "full")
            self.assertGreater(result["after"]["disk_size_bytes"], result["before"]["disk_size_bytes"])
            chown_mock.assert_called_once()
            self.assertEqual(chown_mock.call_args.args[1:], (original_stat.st_uid, original_stat.st_gid))
            chmod_mock.assert_called_once()
            self.assertFalse(list(image_dir.glob("*.pve-helper-backup-*")))
            self.assertFalse(list(image_dir.glob(".pve-helper-inflate-*")))

    @override_settings(STORAGE_WRITE_ENABLED=True, STORAGE_INFLATE_TIMEOUT_SECONDS=60)
    def test_failed_inflate_names_the_cause_and_keeps_the_raw_output_in_the_log(self):
        if not shutil.which("qemu-img"):
            self.skipTest("qemu-img is not available")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_dir = root / "images" / "501"
            image_dir.mkdir(parents=True)
            disk = image_dir / "vm-501-disk-0.qcow2"
            subprocess.run(
                ["qemu-img", "create", "-f", "qcow2", disk.as_posix(), "16M"],
                check=True,
                capture_output=True,
                text=True,
            )
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            ProxmoxEndpoint.objects.create(
                cluster=self.cluster,
                name="pve-node-1",
                url="https://pve-node-1.example.com:8006",
                enabled=True,
                details={"node": "pve-node-1"},
            )
            scan = ScanRun.objects.create(
                status=ScanRun.Status.COMPLETED,
                storage_gate_status={"nfs-vm": {"ok": True, "status": "ok"}},
            )
            ProxmoxInventory.objects.create(
                scan_run=scan,
                node="pve-node-1",
                object_type=ProxmoxInventory.ObjectType.VM,
                vmid=501,
                name="inflate-test",
                cluster=self.cluster,
                status="stopped",
                disk_references=["nfs-vm:501/vm-501-disk-0.qcow2"],
            )
            entry = FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="images/501/vm-501-disk-0.qcow2",
                derived_volid="nfs-vm:501/vm-501-disk-0.qcow2",
                entry_type=FileInventory.EntryType.FILE,
                content_category="vm_disk",
                classification=FileInventory.Classification.REFERENCED,
            )
            raw = "qemu-img: error while writing sector 8192: No space left on device"
            real_run = subprocess.run

            def only_convert_fails(args, **kwargs):
                # `subprocess` is one shared module object, so patching its `run`
                # patches it for every caller — including the preflight's own
                # `qemu-img info`, which would then fail first and never reach the
                # branch under test. Fail the conversion and nothing else.
                if len(args) > 1 and args[1] == "convert":
                    return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr=raw)
                return real_run(args, **kwargs)

            with (
                patch("core.services.proxmox.ProxmoxClient.guest_status", return_value="stopped"),
                patch("core.services.storage_actions.subprocess.run", side_effect=only_convert_fails),
                self.assertLogs("core.services.storage_actions", level="WARNING") as logs,
                self.assertRaises(StorageActionError) as ctx,
            ):
                inflate_storage_file(storage=storage, entry=entry)

            message = str(ctx.exception)
            self.assertIn("out of free space", message)
            self.assertIn("original file was left unchanged", message)
            # The raw output is the whole point of the log line and has no business
            # in the dialog: it is unstructured external text carrying host paths.
            self.assertNotIn("sector 8192", message)
            self.assertIn(raw, "\n".join(logs.output))
            self.assertTrue(disk.exists())
            self.assertFalse(list(image_dir.glob(".pve-helper-inflate-*")))

    def test_uploaded_proxmox_image_paths_are_normalized_for_proxmox(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_dir = root / "images" / "700"
            image_dir.mkdir(parents=True)
            disk = image_dir / "vm-700-disk-0.qcow2"
            disk.write_bytes(b"disk")
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )

            with (
                patch("core.services.confined_filesystem.os.fchown") as chown_mock,
                patch("core.services.confined_filesystem.os.fchmod") as chmod_mock,
            ):
                result = normalize_uploaded_proxmox_image_paths(
                    storage=storage,
                    paths=["images/700/vm-700-disk-0.qcow2", "template/iso/test.iso"],
                )

            self.assertEqual(result["normalized"], ["images/700/vm-700-disk-0.qcow2"])
            self.assertEqual(result["skipped"], ["template/iso/test.iso"])
            self.assertEqual(chown_mock.call_count, 2)
            self.assertEqual(chmod_mock.call_count, 2)
            self.assertEqual(chown_mock.call_args_list[0].args[1:], (0, 0))
            self.assertEqual(chmod_mock.call_args_list[0].args[1], 0o775)
            self.assertEqual(chmod_mock.call_args_list[1].args[1], 0o664)

    def test_uploaded_proxmox_image_normalization_task_audits_result(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_dir = root / "images" / "701"
            image_dir.mkdir(parents=True)
            (image_dir / "vm-701-disk-0.qcow2").write_bytes(b"disk")
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )

            with (
                patch("core.services.confined_filesystem.os.fchown"),
                patch("core.services.confined_filesystem.os.fchmod"),
            ):
                normalize_uploaded_proxmox_image_paths_task(
                    storage.id,
                    ["images/701/vm-701-disk-0.qcow2"],
                    "viewer",
                )

            event = AuditEvent.objects.get(action="file.upload_normalized")
            self.assertEqual(event.outcome, "success")
            self.assertEqual(event.details["normalized"], ["images/701/vm-701-disk-0.qcow2"])

    @override_settings(STORAGE_WRITE_ENABLED=True, STORAGE_INFLATE_TIMEOUT_SECONDS=60)
    def test_qcow2_disk_can_be_inflated_to_metadata(self):
        if not shutil.which("qemu-img"):
            self.skipTest("qemu-img is not available")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_dir = root / "images" / "501"
            image_dir.mkdir(parents=True)
            disk = image_dir / "vm-501-disk-0.qcow2"
            subprocess.run(
                ["qemu-img", "create", "-f", "qcow2", disk.as_posix(), "16M"],
                check=True,
                capture_output=True,
                text=True,
            )
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            ProxmoxEndpoint.objects.create(
                cluster=self.cluster,
                name="pve-node-1",
                url="https://pve-node-1.example.com:8006",
                enabled=True,
                details={"node": "pve-node-1"},
            )
            scan = ScanRun.objects.create(
                status=ScanRun.Status.COMPLETED,
                storage_gate_status={"nfs-vm": {"ok": True, "status": "ok"}},
            )
            ProxmoxInventory.objects.create(
                scan_run=scan,
                node="pve-node-1",
                object_type=ProxmoxInventory.ObjectType.VM,
                vmid=501,
                name="inflate-test",
                cluster=self.cluster,
                status="stopped",
                disk_references=["nfs-vm:501/vm-501-disk-0.qcow2"],
            )
            entry = FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="images/501/vm-501-disk-0.qcow2",
                derived_volid="nfs-vm:501/vm-501-disk-0.qcow2",
                entry_type=FileInventory.EntryType.FILE,
                content_category="vm_disk",
                classification=FileInventory.Classification.REFERENCED,
            )

            with patch("core.services.proxmox.ProxmoxClient.guest_status", return_value="stopped"):
                result = inflate_storage_file(storage=storage, entry=entry, target_preallocation="metadata")

            self.assertTrue(disk.exists())
            self.assertEqual(result["target_preallocation"], "metadata")
            self.assertGreaterEqual(result["after"]["qcow2_allocation_percent"], 95)

    @override_settings(STORAGE_WRITE_ENABLED=True, STORAGE_INFLATE_WORKER_PRESERVES_OWNER=True)
    def test_inflate_action_queues_worker_task_when_live_guest_is_stopped(self):
        if not shutil.which("qemu-img"):
            self.skipTest("qemu-img is not available")

        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_dir = root / "images" / "501"
            image_dir.mkdir(parents=True)
            disk = image_dir / "vm-501-disk-0.qcow2"
            subprocess.run(
                ["qemu-img", "create", "-f", "qcow2", disk.as_posix(), "16M"],
                check=True,
                capture_output=True,
                text=True,
            )
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            ProxmoxEndpoint.objects.create(
                cluster=self.cluster,
                name="pve-node-1",
                url="https://pve-node-1.example.com:8006",
                enabled=True,
                details={"node": "pve-node-1"},
            )
            scan = ScanRun.objects.create(
                status=ScanRun.Status.COMPLETED,
                storage_gate_status={"nfs-vm": {"ok": True, "status": "ok"}},
            )
            ProxmoxInventory.objects.create(
                scan_run=scan,
                node="pve-node-1",
                object_type=ProxmoxInventory.ObjectType.VM,
                vmid=501,
                name="inflate-test",
                cluster=self.cluster,
                status="running",
                disk_references=["nfs-vm:501/vm-501-disk-0.qcow2"],
            )
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="images",
                entry_type=FileInventory.EntryType.DIRECTORY,
                content_category="vm_images",
            )
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="images/501",
                entry_type=FileInventory.EntryType.DIRECTORY,
                content_category="vm_image_directory",
            )
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="images/501/vm-501-disk-0.qcow2",
                derived_volid="nfs-vm:501/vm-501-disk-0.qcow2",
                entry_type=FileInventory.EntryType.FILE,
                content_category="vm_disk",
                classification=FileInventory.Classification.REFERENCED,
            )
            expected_browser_url = unbound_files_url("default", "nfs-vm", "images%2F501")

            with (
                patch("core.services.proxmox.ProxmoxClient.guest_status", return_value="stopped"),
                patch("core.services.storage_catalog.refresh_storage_catalog"),
                patch("core.services.storage_actions.os.geteuid", return_value=999999),
                patch("core.services.storage_actions.os.getegid", return_value=999999),
                patch("core.views.common.async_task", return_value="inflate-task-1") as async_task_mock,
            ):
                response = self.client.post(
                    reverse("core:storage_inflate_file", args=[storage.mount_ref]),
                    {
                        "path": "images/501/vm-501-disk-0.qcow2",
                        "confirm_basic": "yes",
                        "confirm_risk": "yes",
                        "target_preallocation": "metadata",
                        "next": expected_browser_url,
                    },
                )

            self.assertRedirects(response, expected_browser_url, fetch_redirect_response=False)
            async_task_mock.assert_called_once_with(
                "core.tasks.inflate_storage_file_task",
                storage.id,
                FileInventory.objects.get(path="images/501/vm-501-disk-0.qcow2").id,
                "viewer",
                "metadata",
                # The operator answered the escalated question, and the worker
                # re-runs the same gate hours later — it has to know the answer
                # was given, or a queued inflate would refuse itself on arrival.
                True,
                q_options={"cluster": "bulk"},
            )
            event = AuditEvent.objects.get(action="file.inflate_queued")
            self.assertEqual(event.details["task_id"], "inflate-task-1")
            self.assertEqual(event.details["target_preallocation"], "metadata")

    @override_settings(STORAGE_WRITE_ENABLED=True, STORAGE_INFLATE_TIMEOUT_SECONDS=60)
    def test_inflate_task_refreshes_directory_inventory_after_completion(self):
        if not shutil.which("qemu-img"):
            self.skipTest("qemu-img is not available")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_dir = root / "images" / "501"
            image_dir.mkdir(parents=True)
            disk = image_dir / "vm-501-disk-0.qcow2"
            subprocess.run(
                ["qemu-img", "create", "-f", "qcow2", disk.as_posix(), "16M"],
                check=True,
                capture_output=True,
                text=True,
            )
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            ProxmoxEndpoint.objects.create(
                cluster=self.cluster,
                name="pve-node-1",
                url="https://pve-node-1.example.com:8006",
                enabled=True,
                details={"node": "pve-node-1"},
            )
            stale_scan_at = timezone.now() - timedelta(days=1)
            stale_scan = ScanRun.objects.create(
                status=ScanRun.Status.COMPLETED,
                target_storage=storage,
                filesystem_scan_at=stale_scan_at,
                storage_gate_status={"nfs-vm": {"ok": True, "status": "ok"}},
            )
            FileInventory.objects.create(
                scan_run=stale_scan,
                storage=storage,
                path="images",
                entry_type=FileInventory.EntryType.DIRECTORY,
                content_category="vm_images",
            )
            scan = ScanRun.objects.create(
                status=ScanRun.Status.COMPLETED,
                filesystem_scan_at=timezone.now(),
                storage_gate_status={"nfs-vm": {"ok": True, "status": "ok"}},
            )
            ProxmoxInventory.objects.create(
                scan_run=scan,
                node="pve-node-1",
                object_type=ProxmoxInventory.ObjectType.VM,
                vmid=501,
                name="inflate-test",
                cluster=self.cluster,
                status="stopped",
                disk_references=["nfs-vm:501/vm-501-disk-0.qcow2"],
            )
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="images",
                entry_type=FileInventory.EntryType.DIRECTORY,
                content_category="vm_images",
            )
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="images/501",
                entry_type=FileInventory.EntryType.DIRECTORY,
                content_category="vm_image_directory",
            )
            entry = FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="images/501/vm-501-disk-0.qcow2",
                derived_volid="nfs-vm:501/vm-501-disk-0.qcow2",
                entry_type=FileInventory.EntryType.FILE,
                content_category="vm_disk",
                classification=FileInventory.Classification.REFERENCED,
                evidence={
                    "image_info": {
                        "format": "qcow2",
                        "virtual_size_bytes": 16 * 1024**2,
                        "disk_size_bytes": 128 * 1024,
                        "qcow2_allocation_percent": 1.0,
                    }
                },
            )

            with patch("core.services.proxmox.ProxmoxClient.guest_status", return_value="stopped"):
                inflate_storage_file_task(storage.id, entry.id, "viewer", "metadata")

            refreshed = FileInventory.objects.get(
                scan_run=scan,
                storage=storage,
                path="images/501/vm-501-disk-0.qcow2",
            )
            image_info = refreshed.evidence["image_info"]
            self.assertNotEqual(refreshed.id, entry.id)
            self.assertTrue(refreshed.evidence["partial_refresh"])
            self.assertEqual(refreshed.evidence["partial_refresh_directory"], "images/501")
            self.assertEqual(image_info["format"], "qcow2")
            self.assertGreaterEqual(image_info["qcow2_allocation_percent"], 95)
            self.assertEqual(refreshed.size_bytes, disk.stat().st_size)
            stale_scan.refresh_from_db()
            self.assertEqual(stale_scan.filesystem_scan_at, stale_scan_at)
            self.assertFalse(
                FileInventory.objects.filter(
                    scan_run=stale_scan,
                    storage=storage,
                    path="images/501/vm-501-disk-0.qcow2",
                ).exists()
            )
            event = AuditEvent.objects.get(action="file.inflated", outcome="success")
            self.assertEqual(event.details["refreshed_scan_id"], scan.id)

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_inflate_blocks_when_live_guest_is_running_even_if_scan_is_stopped(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_dir = root / "images" / "500"
            image_dir.mkdir(parents=True)
            disk = image_dir / "vm-500-disk-0.qcow2"
            disk.write_bytes(b"disk")
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            ProxmoxEndpoint.objects.create(
                cluster=self.cluster,
                name="pve-node-1",
                url="https://pve-node-1.example.com:8006",
                enabled=True,
                details={"node": "pve-node-1"},
            )
            scan = ScanRun.objects.create(
                status=ScanRun.Status.COMPLETED,
                storage_gate_status={"nfs-vm": {"ok": True, "status": "ok"}},
            )
            ProxmoxInventory.objects.create(
                scan_run=scan,
                node="pve-node-1",
                object_type=ProxmoxInventory.ObjectType.VM,
                vmid=500,
                name="stale-scan-test",
                cluster=self.cluster,
                status="stopped",
                disk_references=["nfs-vm:500/vm-500-disk-0.qcow2"],
            )
            entry = FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="images/500/vm-500-disk-0.qcow2",
                derived_volid="nfs-vm:500/vm-500-disk-0.qcow2",
                entry_type=FileInventory.EntryType.FILE,
                content_category="vm_disk",
                classification=FileInventory.Classification.REFERENCED,
            )

            with patch("core.services.proxmox.ProxmoxClient.guest_status", return_value="running"):
                with self.assertRaisesMessage(StorageActionError, "Stop it manually in Proxmox"):
                    validate_inflate_storage_file(storage=storage, entry=entry)

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_inflate_blocks_when_file_owner_cannot_be_preserved(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_dir = root / "images" / "501"
            image_dir.mkdir(parents=True)
            disk = image_dir / "vm-501-disk-0.qcow2"
            disk.write_bytes(b"disk")
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            scan = ScanRun.objects.create(
                status=ScanRun.Status.COMPLETED,
                storage_gate_status={"nfs-vm": {"ok": True, "status": "ok"}},
            )
            entry = FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="images/501/vm-501-disk-0.qcow2",
                entry_type=FileInventory.EntryType.FILE,
                content_category="vm_disk",
                classification=FileInventory.Classification.UNKNOWN,
            )

            with patch("core.services.storage_actions.os.geteuid", return_value=999999):
                with self.assertRaisesMessage(StorageActionError, "Cannot safely inflate this disk"):
                    validate_inflate_storage_file(storage=storage, entry=entry)

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_inflate_blocks_repeated_full_preallocation(self):
        if not shutil.which("qemu-img"):
            self.skipTest("qemu-img is not available")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_dir = root / "images" / "501"
            image_dir.mkdir(parents=True)
            disk = image_dir / "vm-501-disk-0.qcow2"
            subprocess.run(
                ["qemu-img", "create", "-f", "qcow2", disk.as_posix(), "16M"],
                check=True,
                capture_output=True,
                text=True,
            )
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            scan = ScanRun.objects.create(
                status=ScanRun.Status.COMPLETED,
                storage_gate_status={"nfs-vm": {"ok": True, "status": "ok"}},
            )
            entry = FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="images/501/vm-501-disk-0.qcow2",
                entry_type=FileInventory.EntryType.FILE,
                content_category="vm_disk",
                classification=FileInventory.Classification.UNKNOWN,
            )
            AuditEvent.objects.create(
                username="viewer",
                action="file.inflated",
                object_type="file",
                object_id="nfs-vm:images/501/vm-501-disk-0.qcow2",
                details={
                    "storage_id": "nfs-vm",
                    "path": "images/501/vm-501-disk-0.qcow2",
                    "target_preallocation": "full",
                },
            )

            with self.assertRaisesMessage(StorageActionError, "already been full-inflated"):
                validate_inflate_storage_file(storage=storage, entry=entry)

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_inflate_allows_repeated_full_after_virtual_disk_growth(self):
        if not shutil.which("qemu-img"):
            self.skipTest("qemu-img is not available")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_dir = root / "images" / "501"
            image_dir.mkdir(parents=True)
            disk = image_dir / "vm-501-disk-0.qcow2"
            subprocess.run(
                ["qemu-img", "create", "-f", "qcow2", disk.as_posix(), "32M"],
                check=True,
                capture_output=True,
                text=True,
            )
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            ProxmoxEndpoint.objects.create(
                cluster=self.cluster,
                name="pve-node-1",
                url="https://pve-node-1.example.com:8006",
                enabled=True,
                details={"node": "pve-node-1"},
            )
            scan = ScanRun.objects.create(
                status=ScanRun.Status.COMPLETED,
                storage_gate_status={"nfs-vm": {"ok": True, "status": "ok"}},
            )
            ProxmoxInventory.objects.create(
                scan_run=scan,
                node="pve-node-1",
                object_type=ProxmoxInventory.ObjectType.VM,
                vmid=501,
                name="inflate-growth-test",
                cluster=self.cluster,
                status="stopped",
                disk_references=["nfs-vm:501/vm-501-disk-0.qcow2"],
            )
            entry = FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="images/501/vm-501-disk-0.qcow2",
                derived_volid="nfs-vm:501/vm-501-disk-0.qcow2",
                entry_type=FileInventory.EntryType.FILE,
                content_category="vm_disk",
                classification=FileInventory.Classification.REFERENCED,
            )
            AuditEvent.objects.create(
                username="viewer",
                action="file.inflated",
                object_type="file",
                object_id="nfs-vm:images/501/vm-501-disk-0.qcow2",
                details={
                    "storage_id": "nfs-vm",
                    "path": "images/501/vm-501-disk-0.qcow2",
                    "target_preallocation": "full",
                    "after": {"virtual_size_bytes": 16 * 1024**2},
                },
            )

            with patch("core.services.proxmox.ProxmoxClient.guest_status", return_value="stopped"):
                result = validate_inflate_storage_file(storage=storage, entry=entry)

            self.assertEqual(result.virtual_size_bytes, 32 * 1024**2)
            # The interpreter to execute and the request-derived image path are
            # separate fields, not two reads out of one untyped dict. Collapsing
            # them again gives the executable the path's provenance.
            self.assertIsInstance(result, InflatePreflight)
            self.assertEqual(result.qemu_img, shutil.which("qemu-img"))
            self.assertEqual(result.relative_path, "images/501/vm-501-disk-0.qcow2")

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_inflate_blocks_repeated_full_even_when_inventory_mtime_is_newer(self):
        if not shutil.which("qemu-img"):
            self.skipTest("qemu-img is not available")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_dir = root / "images" / "501"
            image_dir.mkdir(parents=True)
            disk = image_dir / "vm-501-disk-0.qcow2"
            subprocess.run(
                ["qemu-img", "create", "-f", "qcow2", disk.as_posix(), "16M"],
                check=True,
                capture_output=True,
                text=True,
            )
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            scan = ScanRun.objects.create(
                status=ScanRun.Status.COMPLETED,
                storage_gate_status={"nfs-vm": {"ok": True, "status": "ok"}},
            )
            entry = FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="images/501/vm-501-disk-0.qcow2",
                entry_type=FileInventory.EntryType.FILE,
                content_category="vm_disk",
                classification=FileInventory.Classification.UNKNOWN,
                modified_at=timezone.now() + timedelta(minutes=5),
            )
            AuditEvent.objects.create(
                username="viewer",
                action="file.inflated",
                object_type="file",
                object_id="nfs-vm:images/501/vm-501-disk-0.qcow2",
                details={
                    "storage_id": "nfs-vm",
                    "path": "images/501/vm-501-disk-0.qcow2",
                    "target_preallocation": "full",
                    "after": {"virtual_size_bytes": 16 * 1024**2},
                },
            )

            with self.assertRaisesMessage(StorageActionError, "already been full-inflated"):
                validate_inflate_storage_file(storage=storage, entry=entry)

    def test_storage_folder_tree_collapses_inactive_branches_by_default(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        storage = StorageMount.objects.create(
            storage_id="nfs-vm",
            display_name="nfs-vm",
            path="/storages/truenas-vm",
            expected_consumers=["pve-node-1"],
        )
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        for path in ["images", "images/100", "template", "template/iso"]:
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path=path,
                entry_type=FileInventory.EntryType.DIRECTORY,
                content_category="unknown",
                classification=FileInventory.Classification.UNKNOWN,
            )

        response = self.client.get(browser_url("nfs-vm"))

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'data-folder-expanded="false"',
            self._folder_node_tag(response, "images"),
        )
        self.assertNotIn("hidden", self._folder_node_tag(response, "images"))
        self.assertIn("hidden", self._folder_node_tag(response, "images/100"))
        self.assertIn("hidden", self._folder_node_tag(response, "template/iso"))

        response = self.client.get(
            browser_url("nfs-vm"),
            {"path": "template/iso"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'data-folder-expanded="true"',
            self._folder_node_tag(response, "template"),
        )
        self.assertNotIn("hidden", self._folder_node_tag(response, "template"))
        self.assertIn(
            'data-folder-expanded="true"',
            self._folder_node_tag(response, "template/iso"),
        )
        self.assertNotIn("hidden", self._folder_node_tag(response, "template/iso"))
        self.assertIn("hidden", self._folder_node_tag(response, "images/100"))

    def test_storage_browser_hides_app_managed_internal_directories(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            upload_tmp = root / ".pve-helper-upload-tmp"
            upload_tmp.mkdir()
            trash_dir = root / ".trash" / "pve-helper"
            trash_dir.mkdir(parents=True)
            visible = root / "template"
            visible.mkdir()
            storage = StorageMount.objects.create(
                storage_id="nfs-fs",
                display_name="nfs-fs",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
            for path in [".pve-helper-upload-tmp", ".trash", ".trash/pve-helper", "template"]:
                FileInventory.objects.create(
                    scan_run=scan,
                    storage=storage,
                    path=path,
                    entry_type=FileInventory.EntryType.DIRECTORY,
                    content_category="unknown",
                    classification=FileInventory.Classification.UNKNOWN,
                )

            with self.settings(FILE_UPLOAD_TEMP_DIR=upload_tmp.as_posix()):
                response = self.client.get(browser_url("nfs-fs"))

            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, ".pve-helper-upload-tmp")
            self.assertNotContains(response, ".trash")
            self.assertContains(response, "template")

    def test_storage_browser_batches_large_directories_and_searches_server_side(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        storage = StorageMount.objects.create(
            storage_id="nfs-fs",
            display_name="nfs-fs",
            path="/storages/truenas-fs",
            expected_consumers=["pve-node-1"],
        )
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        FileInventory.objects.bulk_create(
            [
                FileInventory(
                    scan_run=scan,
                    storage=storage,
                    path=f"file-{index:03d}.iso",
                    entry_type=FileInventory.EntryType.FILE,
                    content_category="iso",
                    classification=FileInventory.Classification.PROXMOX_CONTENT,
                )
                for index in range(205)
            ]
        )

        response = self.client.get(browser_url(storage.mount_ref))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "file-000.iso")
        self.assertContains(response, "file-199.iso")
        self.assertNotContains(response, "file-200.iso")
        self.assertContains(response, "Load next 200")
        self.assertContains(response, "200 of 205")

        response = self.client.get(
            browser_url(storage.mount_ref),
            {"file_offset": "200", "file_partial": "1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("file-200.iso", payload["rows_html"])
        self.assertIn("file-204.iso", payload["rows_html"])
        self.assertFalse(payload["has_next"])

        response = self.client.get(
            browser_url(storage.mount_ref),
            {"q": "file-204"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "file-204.iso")
        self.assertNotContains(response, "file-000.iso")
        self.assertNotContains(response, "Load next 200")

    def test_audit_log_uses_task_timestamp_format(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)
        event = AuditEvent.objects.create(username="viewer", action="scan.queued")
        AuditEvent.objects.filter(pk=event.pk).update(
            timestamp=datetime(2026, 6, 26, 7, 49, 5, tzinfo=timezone.get_current_timezone())
        )

        response = self.client.get(reverse("core:audit_log"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2026-06-26 07:49:05")
        self.assertNotContains(response, "June 26, 2026")

    def test_audit_log_paginates_events(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)
        AuditEvent.objects.all().delete()
        base_time = datetime(2026, 6, 26, 7, 0, 0, tzinfo=timezone.get_current_timezone())
        for index in range(205):
            event = AuditEvent.objects.create(
                username="viewer",
                action="scan.completed",
                object_type="scan",
                object_id=f"event-{index:03d}",
            )
            AuditEvent.objects.filter(pk=event.pk).update(timestamp=base_time + timedelta(seconds=index))

        response = self.client.get(reverse("core:audit_log"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1-200 of 205")
        self.assertContains(response, "event-204")
        self.assertContains(response, "?page=1")
        self.assertNotContains(response, "event-004")

        response = self.client.get(reverse("core:audit_log"), {"page": "1"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "201-205 of 205")
        self.assertContains(response, "event-004")
        self.assertNotContains(response, "event-204")

        response = self.client.get(reverse("core:audit_log"), {"page": "abc"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1-200 of 205")

    def test_audit_export_csv_uses_filters_and_time_range(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)
        AuditEvent.objects.all().delete()
        base_time = datetime(2026, 6, 26, 7, 0, 0, tzinfo=timezone.get_current_timezone())
        included = AuditEvent.objects.create(
            username="viewer",
            action="file.downloaded",
            object_type="file",
            object_id="nfs-vm:template/iso/ubuntu.iso",
            module="storage",
        )
        excluded = AuditEvent.objects.create(
            username="viewer",
            action="auth.login",
            object_type="user",
            object_id="viewer",
            module="auth",
        )
        AuditEvent.objects.filter(pk=included.pk).update(timestamp=base_time)
        AuditEvent.objects.filter(pk=excluded.pk).update(timestamp=base_time + timedelta(days=1))

        response = self.client.get(
            reverse("core:audit_export"),
            {
                "format": "csv",
                "filter": "storage",
                "q": "ubuntu",
                "start": "2026-06-26 00:00",
                "end": "2026-06-26 23:59",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertTrue(response.streaming)
        body = b"".join(response.streaming_content).decode()
        self.assertIn("Download file", body)
        self.assertIn("nfs-vm:template/iso/ubuntu.iso", body)
        self.assertNotIn("Login", body)

    def test_audit_export_json_can_include_technical_columns(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)
        AuditEvent.objects.all().delete()
        AuditEvent.objects.create(
            username="viewer",
            action="guest.power.start",
            object_type="guest",
            object_id="vm:500",
            module="vms",
            details={"target_type": "vm", "vmid": 500, "name": "Lab VM"},
        )

        response = self.client.get(reverse("core:audit_export"), {"format": "json", "include_technical": "on"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.streaming)
        payload = json.loads(b"".join(response.streaming_content))
        self.assertIn("Raw Action", payload["columns"])
        self.assertEqual(payload["rows"][0]["Action"], "Power on guest")
        self.assertEqual(payload["rows"][0]["Raw Action"], "guest.power.start")
        self.assertIn("Lab VM", payload["rows"][0]["Object"])

    def test_audit_export_xlsx_returns_workbook(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)
        AuditEvent.objects.all().delete()
        AuditEvent.objects.create(
            username="viewer", action="scan.completed", object_type="scan_run", object_id="scan-1", module="storage"
        )

        response = self.client.get(reverse("core:audit_export"), {"format": "xlsx"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        with zipfile.ZipFile(BytesIO(response.content)) as workbook:
            self.assertIn("xl/worksheets/sheet1.xml", workbook.namelist())
            self.assertIn("xl/sharedStrings.xml", workbook.namelist())
            self.assertIn("docProps/app.xml", workbook.namelist())
            sheet = workbook.read("xl/worksheets/sheet1.xml").decode()
            strings = workbook.read("xl/sharedStrings.xml").decode()
            app_properties = workbook.read("docProps/app.xml").decode()
        self.assertIn('t="s"', sheet)
        self.assertIn("Full scan completed", strings)
        self.assertIn("Storage inventory scan", strings)
        self.assertIn("HeadingPairs", app_properties)

    def test_audit_export_xlsx_refuses_more_than_the_safe_row_limit(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)
        AuditEvent.objects.all().delete()
        AuditEvent.objects.create(username="viewer", action="scan.completed")
        AuditEvent.objects.create(username="viewer", action="scan.completed")

        with patch("core.views.audit.AUDIT_XLSX_MAX_ROWS", 1):
            response = self.client.get(reverse("core:audit_export"), {"format": "xlsx"}, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Excel export is limited to 1 events")

    def test_audit_log_records_authentication_events(self):
        get_user_model().objects.create_user(username="viewer", password="secret")

        self.assertTrue(self.client.login(username="viewer", password="secret"))
        event = AuditEvent.objects.filter(action="auth.login").latest("timestamp")
        self.assertEqual(event.username, "viewer")
        self.assertEqual(event.object_type, "user")
        self.assertEqual(event.outcome, "success")

        response = self.client.get(reverse("core:audit_log"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Authentication, storage scans, and file actions")
        self.assertContains(response, "Login")
        self.assertContains(response, "Auth")
        self.assertContains(response, "?filter=storage")
        self.assertContains(response, 'name="q"')
        # The auth.login event is categorized server-side into the vms/auth module.
        self.assertEqual(event.module, "auth")

    def test_audit_log_module_filter_is_server_side(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)
        AuditEvent.objects.all().delete()  # force_login emits its own auth.login event
        AuditEvent.objects.create(action="file.trashed", object_type="file", module="storage")
        AuditEvent.objects.create(action="auth.login", object_type="user", module="auth")

        storage_only = self.client.get(reverse("core:audit_log"), {"filter": "storage"})
        self.assertEqual(storage_only.context["audit_total"], 1)
        self.assertEqual([e.action for e in storage_only.context["events"]], ["file.trashed"])

        auth_only = self.client.get(reverse("core:audit_log"), {"filter": "auth"})
        self.assertEqual(auth_only.context["audit_total"], 1)
        self.assertEqual([e.action for e in auth_only.context["events"]], ["auth.login"])

    def test_scheduled_scan_audit_shows_interval_and_readable_labels(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)
        update_scan_schedule(enabled=True, interval_minutes=180)

        with patch("core.tasks.async_task", return_value="scheduled-task-id"):
            scan_id = enqueue_scheduled_scan()

        event = AuditEvent.objects.get(action="scan.queued")
        self.assertEqual(event.details["source"], "schedule")
        self.assertEqual(event.details["interval_minutes"], 180)

        response = self.client.get(reverse("core:audit_log"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Scheduled full scan (180 min)")
        self.assertContains(response, "Storage inventory scan")
        self.assertNotContains(response, f"Scan {scan_id}")
        self.assertNotContains(response, "scan_run")

    def test_audit_log_uses_readable_storage_action_labels(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        AuditEvent.objects.create(
            username="viewer",
            action="file.folder_created",
            object_type="file",
            object_id="nfs-fs:template/iso/new-folder",
            details={"storage_id": "nfs-fs", "path": "template/iso/new-folder"},
        )
        AuditEvent.objects.create(
            username="viewer",
            action="file.inflate_queued",
            object_type="file",
            object_id="nfs-vm:images/502/vm-502-disk-0.qcow2",
            details={
                "storage_id": "nfs-vm",
                "path": "images/502/vm-502-disk-0.qcow2",
                "target_preallocation": "full",
            },
        )
        AuditEvent.objects.create(
            username="viewer",
            action="file.inflated",
            object_type="file",
            object_id="nfs-vm:images/502/vm-502-disk-0.qcow2",
            details={
                "storage_id": "nfs-vm",
                "path": "images/502/vm-502-disk-0.qcow2",
                "target_preallocation": "metadata",
            },
        )
        AuditEvent.objects.create(
            username="viewer",
            action="file.folder_uploaded",
            object_type="file",
            object_id="nfs-fs:/",
            details={"storage_id": "nfs-fs", "path": "/", "file_count": 2},
        )
        AuditEvent.objects.create(
            username="system",
            action="file.upload_normalized",
            object_type="file",
            object_id="nfs-vm:images/501/vm-501-disk-0.qcow2",
            details={"storage_id": "nfs-vm", "path": "images/501/vm-501-disk-0.qcow2"},
        )
        AuditEvent.objects.create(
            username="system",
            action="trash.purge",
            object_type="trash",
            object_id="",
            details={"purged": 3},
        )
        AuditEvent.objects.create(
            username="viewer",
            action="trash.purge.schedule.updated",
            object_type="trash_purge_schedule",
            object_id="automatic-trash-purge",
        )
        AuditEvent.objects.create(
            username="viewer",
            action="storage.content.updated",
            object_type="storage",
            object_id="nfs-vm",
            details={"storage_id": "nfs-vm", "old_content": ["images"], "new_content": ["images", "iso"]},
        )
        AuditEvent.objects.create(
            username="viewer",
            action="audit.retention.schedule.updated",
            object_type="audit_retention_schedule",
            object_id="automatic-audit-retention",
        )
        AuditEvent.objects.create(
            username="system",
            action="scan.retention.purge",
            object_type="scan_retention",
            object_id="automatic-scan-retention",
        )

        response = self.client.get(reverse("core:audit_log"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Create folder")
        self.assertContains(response, "Disk inflate queued (full)")
        self.assertContains(response, "Inflate disk (metadata)")
        self.assertContains(response, "Upload folder")
        self.assertContains(response, "Normalize uploaded disk metadata")
        self.assertContains(response, "Recycle Bin purge")
        self.assertContains(response, "Recycle Bin purge schedule updated")
        self.assertContains(response, "Update storage content")
        self.assertContains(response, "Audit retention schedule updated")
        self.assertContains(response, "Scan retention purge")
        self.assertContains(response, "Recycle Bin")
        self.assertContains(response, "Audit retention schedule")
        self.assertContains(response, "Scan retention")
        self.assertNotContains(response, "file.folder_created")
        self.assertNotContains(response, "file.inflate_queued")
        self.assertNotContains(response, "file.inflated")
        self.assertNotContains(response, "file.upload_normalized")
        self.assertNotContains(response, "trash.purge.schedule.updated")
        self.assertNotContains(response, "scan.retention.purge")

    def test_audit_log_describes_bulk_file_operations_and_their_answer(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        AuditEvent.objects.create(
            username="viewer",
            action="file.bulk_operation",
            object_type="file",
            object_id="nfs-fs:trash",
            outcome="warning",
            details={
                "operation": "trash",
                "storage_id": "nfs-fs",
                "storage_name": "nfs-fs",
                "summary": "2 of 3 moved to trash",
                "total": 3,
                "succeeded": ["dump/a.vma.zst", "dump/b.vma.zst"],
                "failed": [{"path": "images/100/vm-100-disk-0.qcow2", "error": "Blocked by preflight."}],
                "skipped": [],
            },
        )
        AuditEvent.objects.create(
            username="viewer",
            action="file.bulk_operation.answered",
            object_type="file",
            object_id="nfs-fs:trash",
            details={"operation": "trash", "storage_name": "nfs-fs", "answer": "accepted", "remaining": 1},
        )

        response = self.client.get(reverse("core:audit_log"))

        self.assertEqual(response.status_code, 200)
        # The label carries the outcome; the detail column names what failed.
        self.assertContains(response, "Move files to trash (2 of 3)")
        self.assertContains(response, "images/100/vm-100-disk-0.qcow2: Blocked by preflight.")
        self.assertContains(response, "Move files to trash — outcome accepted")
        self.assertContains(response, "Operator accepted the outcome; 1 file(s) left as they were")
        self.assertNotContains(response, "file.bulk_operation")

    def test_audit_log_uses_readable_scheduled_task_labels(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        AuditEvent.objects.create(
            username="viewer",
            action="scheduled_action.created",
            object_type="scheduled_action",
            object_id="42",
            outcome="success",
            details={
                "scheduled_action_name": "Night shutdown",
                "target_type": "vm",
                "target_vmid": 500,
            },
        )
        AuditEvent.objects.create(
            username="system",
            action="scheduled_action.run_completed",
            object_type="scheduled_action_run",
            object_id="run-42",
            outcome="success",
            details={
                "scheduled_action_name": "Morning start",
                "target_type": "ct",
                "target_vmid": 101,
            },
        )

        response = self.client.get(reverse("core:audit_log"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "VMs")
        self.assertContains(response, "Scheduled task created")
        self.assertContains(response, "Scheduled task completed")
        self.assertContains(response, "Night shutdown")
        self.assertContains(response, "Morning start")
        self.assertNotContains(response, "scheduled_action.created")
        self.assertNotContains(response, "scheduled_action.run_completed")

    @override_settings(STORAGE_DOWNLOAD_ACCEL_ENABLED=False)
    def test_storage_file_download_uses_latest_inventory_and_audits_action(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            dump_dir = root / "dump"
            dump_dir.mkdir()
            backup_file = dump_dir / "vzdump-qemu-100.vma.zst"
            backup_file.write_bytes(b"backup data")

            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="dump",
                entry_type=FileInventory.EntryType.DIRECTORY,
                content_category="backup",
                classification=FileInventory.Classification.UNKNOWN,
            )
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="dump/vzdump-qemu-100.vma.zst",
                entry_type=FileInventory.EntryType.FILE,
                size_bytes=backup_file.stat().st_size,
                content_category="backup",
                classification=FileInventory.Classification.UNKNOWN,
            )

            response = self.client.get(
                reverse("core:storage_download", args=["nfs-vm"]),
                {"path": "dump/vzdump-qemu-100.vma.zst"},
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), b"backup data")
            self.assertIn("attachment", response["Content-Disposition"])
            self.assertIn("vzdump-qemu-100.vma.zst", response["Content-Disposition"])

        event = AuditEvent.objects.get(action="file.downloaded")
        self.assertEqual(event.username, "viewer")
        self.assertEqual(event.object_id, f"{storage.mount_ref}:dump/vzdump-qemu-100.vma.zst")

    @override_settings(STORAGE_DOWNLOAD_ACCEL_ENABLED=False)
    def test_storage_file_download_supports_http_ranges(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            dump_dir = root / "dump"
            dump_dir.mkdir()
            backup_file = dump_dir / "vzdump-qemu-100.vma.zst"
            backup_file.write_bytes(b"backup data")

            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="dump/vzdump-qemu-100.vma.zst",
                entry_type=FileInventory.EntryType.FILE,
                size_bytes=backup_file.stat().st_size,
                content_category="backup",
                classification=FileInventory.Classification.UNKNOWN,
            )

            response = self.client.get(
                reverse("core:storage_download", args=["nfs-vm"]),
                {"path": "dump/vzdump-qemu-100.vma.zst"},
                HTTP_RANGE="bytes=7-10",
            )

            self.assertEqual(response.status_code, 206)
            self.assertEqual(b"".join(response.streaming_content), b"data")
            self.assertEqual(response["Content-Range"], "bytes 7-10/11")
            self.assertEqual(response["Accept-Ranges"], "bytes")
            self.assertEqual(response["X-Accel-Buffering"], "no")

    @contextmanager
    def _accel_datastore(self, *, manifest_device: int | None = "self"):
        """A datastore laid out the way the containers actually see one.

        The mount lives at `<container root>/nfs-vm`, which is the directory an
        accelerated URL resolves against and the one nginx recorded, so the device
        comparison has something real to compare. `manifest_device` writes a
        different number to stand in for the host having remounted a different
        filesystem at that path after nginx took its snapshot, or None to write a
        legacy name-only line.
        """
        with TemporaryDirectory() as tmp:
            container_root = Path(tmp)
            root = container_root / "nfs-vm"
            (root / "dump").mkdir(parents=True)
            backup_file = root / "dump" / "backup with space.vma.zst"
            backup_file.write_bytes(b"backup data")

            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                relative_path="nfs-vm",
                expected_consumers=["pve-node-1"],
            )
            scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="dump/backup with space.vma.zst",
                entry_type=FileInventory.EntryType.FILE,
                size_bytes=backup_file.stat().st_size,
                content_category="backup",
                classification=FileInventory.Classification.UNKNOWN,
            )
            manifest = container_root / "accel-mounts"
            if manifest_device is None:
                manifest.write_text("nfs-vm\n", encoding="utf-8")
            else:
                device = os.stat(root).st_dev if manifest_device == "self" else manifest_device
                manifest.write_text(f"nfs-vm\t{device}\n", encoding="utf-8")

            with override_settings(
                STORAGE_DOWNLOAD_ACCEL_MANIFEST_PATH=manifest,
                PVE_HELPER_STORAGE_CONTAINER_ROOT=container_root,
            ):
                yield

    def _download_backup(self):
        return self.client.get(
            reverse("core:storage_download", args=["nfs-vm"]),
            {"path": "dump/backup with space.vma.zst"},
        )

    @override_settings(STORAGE_DOWNLOAD_ACCEL_ENABLED=True)
    def test_storage_file_download_can_use_nginx_internal_redirect(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with self._accel_datastore():
            response = self._download_backup()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["X-Accel-Redirect"], "/_pve_helper_download/nfs-vm/dump/backup%20with%20space.vma.zst"
        )
        self.assertEqual(response["Accept-Ranges"], "bytes")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("backup with space.vma.zst", response["Content-Disposition"])
        self.assertEqual(response.content, b"")

    @override_settings(STORAGE_DOWNLOAD_ACCEL_ENABLED=True)
    def test_a_remounted_datastore_streams_instead_of_trusting_nginx_snapshot(self):
        """nginx keeps the filesystem it started with; the app follows the host.

        After a remount the name still matches, so without the device the sidecar
        would serve bytes out of the detached original while every check ran against
        its replacement — an authorized download of a file nobody inspected, with no
        error raised anywhere. Streaming is the correct answer: those bytes come from
        the filesystem the app itself validated.
        """
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with self._accel_datastore(manifest_device=1):
            response = self._download_backup()

        self.assertNotIn("X-Accel-Redirect", response)
        self.assertEqual(b"".join(response.streaming_content), b"backup data")

    @override_settings(STORAGE_DOWNLOAD_ACCEL_ENABLED=True)
    def test_a_manifest_without_a_device_is_not_trusted_on_the_name_alone(self):
        """The version skew where `web` is newer than the nginx sidecar.

        The old manifest cannot establish identity, and an entry whose identity
        cannot be established is the one this check exists to distrust. The cost is a
        streamed download until the sidecar is restarted.
        """
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with self._accel_datastore(manifest_device=None):
            response = self._download_backup()

        self.assertNotIn("X-Accel-Redirect", response)
        self.assertEqual(b"".join(response.streaming_content), b"backup data")

    def test_storage_file_download_action_is_excluded_from_soft_navigation(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dump").mkdir()
            backup_file = root / "dump" / "backup.vma.zst"
            backup_file.write_bytes(b"backup data")
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="dump",
                entry_type=FileInventory.EntryType.DIRECTORY,
            )
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="dump/backup.vma.zst",
                entry_type=FileInventory.EntryType.FILE,
                size_bytes=backup_file.stat().st_size,
            )

            response = self.client.get(browser_url("nfs-vm"), {"path": "dump"})

            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "data-file-download-action")
            self.assertContains(response, "data-no-soft-navigation")

    def test_storage_file_download_rejects_directories_and_path_traversal(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dump").mkdir()
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="dump",
                entry_type=FileInventory.EntryType.DIRECTORY,
                content_category="backup",
                classification=FileInventory.Classification.UNKNOWN,
            )

            directory_response = self.client.get(
                reverse("core:storage_download", args=["nfs-vm"]),
                {"path": "dump"},
            )
            traversal_response = self.client.get(
                reverse("core:storage_download", args=["nfs-vm"]),
                {"path": "../secret.txt"},
            )

        self.assertEqual(directory_response.status_code, 404)
        self.assertEqual(traversal_response.status_code, 404)

    @override_settings(STORAGE_WRITE_ENABLED=False)
    def test_storage_write_actions_are_hidden_and_blocked_by_global_flag(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=tmp,
                expected_consumers=["pve-node-1"],
            )
            ScanRun.objects.create(status=ScanRun.Status.COMPLETED)

            response = self.client.get(browser_url("nfs-vm"))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, "Upload")
            self.assertNotContains(response, "Move to Recycle Bin")

            response = self.client.post(
                reverse("core:storage_upload", args=[storage.mount_ref]),
                {"file": SimpleUploadedFile("test.txt", b"blocked")},
            )

            content_response = self.client.post(
                reverse("core:storage_content_update", args=[storage.mount_ref]),
                {"content": ["images"]},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(content_response.status_code, 403)

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_storage_write_actions_are_hidden_when_app_mount_is_read_only(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=tmp,
                expected_consumers=["pve-node-1"],
            )
            ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
            expected_browser_url = browser_url(storage.mount_ref)
            read_only_info = StorageSpaceInfo(
                ok=True,
                access_mode="read_only",
                access_label="Read-only",
                access_class="warning",
                can_write=False,
            )

            read_only_health = MountHealth(True, False, "Mount is read-only.")
            with (
                patch("core.views.common.storage_space_info", return_value=read_only_info),
                patch("core.views.storage.registered_mount_health", return_value=read_only_health),
                # The datastore header reads write capability from the catalog's
                # capabilities, which resolve the mount's health themselves.
                patch("core.services.storage_catalog.mount_health", return_value=read_only_health),
            ):
                response = self.client.get(expected_browser_url)

            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "PVE-helper access")
            self.assertContains(response, "Read-only")
            self.assertContains(response, "Upload Files")

            with patch(
                "core.services.storage_actions.registered_mount_health",
                return_value=read_only_health,
            ):
                response = self.client.post(
                    reverse("core:storage_upload", args=[storage.mount_ref]),
                    {
                        "next": expected_browser_url,
                        "file": SimpleUploadedFile("test.txt", b"blocked"),
                    },
                )

            self.assertRedirects(response, expected_browser_url, fetch_redirect_response=False)
            self.assertFalse((Path(tmp) / "test.txt").exists())
            self.assertFalse(AuditEvent.objects.filter(action="file.uploaded", outcome="success").exists())
        self.assertTrue(AuditEvent.objects.filter(action="file.uploaded", outcome="failed").exists())

    @override_settings(STORAGE_WRITE_ENABLED=True, STORAGE_UPLOAD_MAX_SIZE_MB=1)
    def test_storage_upload_writes_file_and_refuses_overwrite(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
            expected_browser_url = browser_url(storage.mount_ref)

            response = self.client.get(expected_browser_url)
            self.assertContains(response, "Upload")
            self.assertNotContains(response, "Register VM")
            self.assertContains(response, "Inflate")
            self.assertContains(response, "Upload Folder")

            response = self.client.post(
                reverse("core:storage_upload", args=[storage.mount_ref]),
                {
                    "path": "",
                    "next": expected_browser_url,
                    "file": SimpleUploadedFile("upload.txt", b"hello"),
                },
            )

            self.assertRedirects(response, expected_browser_url, fetch_redirect_response=False)
            self.assertEqual((root / "upload.txt").read_bytes(), b"hello")
            self.assertTrue(
                FileInventory.objects.filter(scan_run__status=ScanRun.Status.COMPLETED, path="upload.txt").exists()
            )
            event = AuditEvent.objects.get(action="file.uploaded")
            self.assertEqual(event.object_id, f"{storage.mount_ref}:upload.txt")
            self.assertIn("Upload file", [task["name"] for task in recent_task_page(limit=10).tasks])

            response = self.client.post(
                reverse("core:storage_upload", args=[storage.mount_ref]),
                {
                    "path": "",
                    "next": expected_browser_url,
                    "file": SimpleUploadedFile("upload.txt", b"overwrite"),
                },
            )

            self.assertRedirects(response, expected_browser_url, fetch_redirect_response=False)
            self.assertEqual((root / "upload.txt").read_bytes(), b"hello")
            self.assertEqual(AuditEvent.objects.filter(action="file.uploaded", outcome="success").count(), 1)
        # The refused second upload is evidence too: an operator who is told
        # "target file already exists" must be able to find that answer later.
        self.assertEqual(AuditEvent.objects.filter(action="file.uploaded", outcome="failed").count(), 1)

    @override_settings(STORAGE_WRITE_ENABLED=True, STORAGE_UPLOAD_MAX_SIZE_MB=0)
    def test_storage_folder_upload_creates_tree_and_refreshes_directories(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
            expected_browser_url = browser_url(storage.mount_ref)

            response = self.client.post(
                reverse("core:storage_upload_folder", args=[storage.mount_ref]),
                {
                    "path": "",
                    "next": expected_browser_url,
                    "relative_path": ["folder/a.txt", "folder/nested/b.txt"],
                    "files": [
                        SimpleUploadedFile("a.txt", b"a"),
                        SimpleUploadedFile("b.txt", b"b"),
                    ],
                },
            )

            self.assertRedirects(response, expected_browser_url, fetch_redirect_response=False)
            self.assertEqual((root / "folder" / "a.txt").read_bytes(), b"a")
            self.assertEqual((root / "folder" / "nested" / "b.txt").read_bytes(), b"b")
            self.assertTrue(
                FileInventory.objects.filter(
                    scan_run__status=ScanRun.Status.COMPLETED,
                    storage=storage,
                    path="folder/nested",
                    entry_type=FileInventory.EntryType.DIRECTORY,
                ).exists()
            )
            event = AuditEvent.objects.get(action="file.folder_uploaded")
            self.assertEqual(event.details["file_count"], 2)
            self.assertIn("Upload folder", [task["name"] for task in recent_task_page(limit=10).tasks])

    @override_settings(STORAGE_WRITE_ENABLED=True, STORAGE_UPLOAD_MAX_SIZE_MB=0)
    def test_async_storage_folder_upload_returns_json_redirect(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
            expected_browser_url = browser_url(storage.mount_ref)

            response = self.client.post(
                reverse("core:storage_upload_folder", args=[storage.mount_ref]),
                {
                    "path": "",
                    "next": expected_browser_url,
                    "relative_path": ["folder/a.txt"],
                    "files": [SimpleUploadedFile("a.txt", b"a")],
                },
                HTTP_X_PVE_HELPER_ASYNC_UPLOAD="1",
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"ok": True, "redirect": expected_browser_url})
            self.assertEqual((root / "folder" / "a.txt").read_bytes(), b"a")

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_storage_folder_upload_rejects_unsafe_paths_and_rolls_back(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
            expected_browser_url = browser_url(storage.mount_ref)

            response = self.client.post(
                reverse("core:storage_upload_folder", args=[storage.mount_ref]),
                {
                    "path": "",
                    "next": expected_browser_url,
                    "relative_path": ["folder/a.txt", "../outside.txt"],
                    "files": [
                        SimpleUploadedFile("a.txt", b"a"),
                        SimpleUploadedFile("outside.txt", b"bad"),
                    ],
                },
            )

            self.assertRedirects(response, expected_browser_url, fetch_redirect_response=False)
            self.assertFalse((root / "folder" / "a.txt").exists())
            self.assertFalse((root.parent / "outside.txt").exists())
            self.assertFalse(AuditEvent.objects.filter(action="file.folder_uploaded", outcome="success").exists())
        self.assertTrue(AuditEvent.objects.filter(action="file.folder_uploaded", outcome="failed").exists())

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_storage_create_folder_writes_directory(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
            expected_browser_url = browser_url(storage.mount_ref)

            response = self.client.post(
                reverse("core:storage_create_folder", args=[storage.mount_ref]),
                {
                    "path": "",
                    "folder_name": "iso-imports",
                    "next": expected_browser_url,
                },
            )

            self.assertRedirects(response, expected_browser_url, fetch_redirect_response=False)
            self.assertEqual(list(get_messages(response.wsgi_request)), [])
            self.assertTrue((root / "iso-imports").is_dir())
            self.assertTrue(
                FileInventory.objects.filter(
                    scan_run__status=ScanRun.Status.COMPLETED,
                    storage=storage,
                    path="iso-imports",
                    entry_type=FileInventory.EntryType.DIRECTORY,
                ).exists()
            )
            event = AuditEvent.objects.get(action="file.folder_created")
            self.assertEqual(event.object_id, f"{storage.mount_ref}:iso-imports")
            self.assertIn("Create folder", [task["name"] for task in recent_task_page(limit=10).tasks])

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_storage_trash_adopts_discovered_app_trash_files(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            trash_file = root / ".trash" / "pve-helper" / "20260627T184314806419Z" / "template" / "iso" / "junk.txt"
            trash_file.parent.mkdir(parents=True)
            trash_file.write_bytes(b"trash")
            storage = StorageMount.objects.create(
                storage_id="nfs-fs",
                display_name="nfs-fs",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path=".trash/pve-helper/20260627T184314806419Z/template/iso/junk.txt",
                entry_type=FileInventory.EntryType.FILE,
                size_bytes=trash_file.stat().st_size,
                classification=FileInventory.Classification.TRASH,
                content_category="trash",
            )

            trash_url = reverse("core:storage_trash", args=[storage.mount_ref])
            response = self.client.get(trash_url)

            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "template/iso/junk.txt")
            trash_item = TrashItem.objects.get()
            self.assertEqual(trash_item.original_path, "template/iso/junk.txt")
            self.assertTrue(trash_item.metadata["discovered_from_trash_scan"])

            response = self.client.post(
                reverse("core:storage_restore_file", args=[trash_item.id]),
                {"next": trash_url},
            )

            self.assertRedirects(response, trash_url)
            self.assertTrue((root / "template" / "iso" / "junk.txt").exists())
            self.assertFalse(trash_file.exists())
            self.assertFalse(TrashItem.objects.filter(restore_status=TrashItem.RestoreStatus.TRASHED).exists())

            response = self.client.get(trash_url)

            self.assertContains(response, "No restorable files in the Recycle Bin.")

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_recycle_bin_purge_states_what_is_destroyed_and_who_still_references_it(self):
        """The app's only irreversible file operation must say what it is deleting."""
        user = get_user_model().objects.create_user(username="purge-viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
            )
            metadata_generation = uuid.uuid4()
            definition = ClusterStorage.objects.create(
                cluster=self.cluster,
                storage_id="shared-nfs",
                storage_type="dir",
                shared=True,
                present=True,
                observed_metadata_generation=metadata_generation,
            )
            bind_storage_mount(cluster_storage=definition, mount=storage)
            CurrentGuestInventory.objects.create(
                cluster=self.cluster,
                node="pve1",
                object_type="vm",
                vmid=100,
                status="stopped",
                disk_references=["shared-nfs:100/vm-100-disk-0.qcow2"],
                observed_at=timezone.now(),
            )
            TrashItem.objects.create(
                mount=storage,
                storage_id=storage.storage_id,
                original_path="images/100/vm-100-disk-0.qcow2",
                trash_path=".trash/pve-helper/20260719T120000000000Z/images/100/vm-100-disk-0.qcow2",
                restore_status=TrashItem.RestoreStatus.TRASHED,
                moved_at=timezone.now() - timedelta(days=3),
                metadata={"original_size_bytes": 5 * 1024 * 1024},
            )

            response = self.client.get(reverse("core:storage_trash", args=[storage.mount_ref]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "original path images/100/vm-100-disk-0.qcow2")
        self.assertContains(response, "recoverable here for 3 day(s)")
        self.assertContains(response, "still referenced by 1 guest config(s): vm:100 (stopped)")
        self.assertContains(response, "cannot be undone")
        self.assertContains(response, "Are you really sure?")

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_storage_trash_ignores_nfs_silly_rename_files(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            nfs_file = (
                root
                / ".trash"
                / "pve-helper"
                / "20260629T203814393614Z"
                / "images"
                / "505"
                / ".nfs000000000001d51f00000001"
            )
            nfs_file.parent.mkdir(parents=True)
            nfs_file.write_bytes(b"temporary")
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path=".trash/pve-helper/20260629T203814393614Z/images/505/.nfs000000000001d51f00000001",
                entry_type=FileInventory.EntryType.FILE,
                size_bytes=nfs_file.stat().st_size,
                classification=FileInventory.Classification.TRASH,
                content_category="trash",
            )

            response = self.client.get(reverse("core:storage_trash", args=[storage.mount_ref]))

            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "No restorable files in the Recycle Bin.")
            self.assertFalse(TrashItem.objects.exists())

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_storage_trash_cleans_empty_app_trash_directories(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty_leaf = root / ".trash" / "pve-helper" / "20260628T180936830007Z" / "template" / "iso"
            empty_leaf.mkdir(parents=True)
            storage = StorageMount.objects.create(
                storage_id="nfs-fs",
                display_name="nfs-fs",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            ScanRun.objects.create(status=ScanRun.Status.COMPLETED)

            response = self.client.get(reverse("core:storage_trash", args=[storage.mount_ref]))

            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "No restorable files in the Recycle Bin.")
            self.assertFalse((root / ".trash" / "pve-helper" / "20260628T180936830007Z").exists())
            self.assertTrue((root / ".trash" / "pve-helper").exists())

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_trash_item_can_be_purged_permanently(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            trash_file = root / ".trash" / "pve-helper" / "20260629T100000000000Z" / "dump" / "old.vma.zst"
            trash_file.parent.mkdir(parents=True)
            trash_file.write_bytes(b"trash")
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            trash_item = TrashItem.objects.create(
                original_path="dump/old.vma.zst",
                trash_path=".trash/pve-helper/20260629T100000000000Z/dump/old.vma.zst",
                moved_by=user,
                moved_at=timezone.now(),
                metadata={"storage_id": storage.storage_id},
            )
            trash_url = reverse("core:storage_trash", args=[storage.mount_ref])

            response = self.client.post(
                reverse("core:purge_trash_item", args=[trash_item.id]),
                {"next": trash_url, "confirm_basic": "yes"},
            )

            self.assertRedirects(response, trash_url)
            trash_item.refresh_from_db()
            self.assertEqual(trash_item.restore_status, TrashItem.RestoreStatus.PURGED)
            self.assertFalse(trash_file.exists())
            self.assertEqual(
                AuditEvent.objects.get(action="file.purged").object_id, f"{storage.mount_ref}:dump/old.vma.zst"
            )

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_trash_item_purge_requires_confirmation(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "storage"
            trash_file = root / ".trash/pve-helper/20260629T100000000000Z/dump/old.vma.zst"
            trash_file.parent.mkdir(parents=True)
            trash_file.write_bytes(b"trash")
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            trash_item = TrashItem.objects.create(
                original_path="dump/old.vma.zst",
                trash_path=".trash/pve-helper/20260629T100000000000Z/dump/old.vma.zst",
                moved_by=user,
                moved_at=timezone.now(),
                metadata={"storage_id": storage.storage_id},
            )
            trash_url = reverse("core:storage_trash", args=[storage.mount_ref])

            response = self.client.post(
                reverse("core:purge_trash_item", args=[trash_item.id]),
                {"next": trash_url},
            )

            self.assertRedirects(response, trash_url)
            trash_item.refresh_from_db()
            self.assertEqual(trash_item.restore_status, TrashItem.RestoreStatus.TRASHED)
            self.assertTrue(trash_file.exists())
            messages = [str(message) for message in get_messages(response.wsgi_request)]
            self.assertIn("Permanent delete was not confirmed.", messages)

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_trash_item_purge_rejects_path_outside_storage_root(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "storage"
            root.mkdir()
            outside = base / "outside.txt"
            outside.write_bytes(b"keep")
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            trash_item = TrashItem.objects.create(
                original_path="dump/old.vma.zst",
                trash_path="../outside.txt",
                moved_by=user,
                moved_at=timezone.now(),
                metadata={"storage_id": storage.storage_id},
            )
            trash_url = reverse("core:storage_trash", args=[storage.mount_ref])

            response = self.client.post(
                reverse("core:purge_trash_item", args=[trash_item.id]),
                {"next": trash_url, "confirm_basic": "yes"},
            )

            self.assertRedirects(response, trash_url)
            trash_item.refresh_from_db()
            self.assertEqual(trash_item.restore_status, TrashItem.RestoreStatus.TRASHED)
            self.assertTrue(outside.exists())
            self.assertEqual(outside.read_bytes(), b"keep")
            messages = [str(message) for message in get_messages(response.wsgi_request)]
            self.assertIn("Invalid storage path.", messages)

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_scheduled_trash_purge_uses_safe_storage_path(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "storage"
            root.mkdir()
            outside = base / "outside.txt"
            outside.write_bytes(b"keep")
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            TrashItem.objects.create(
                original_path="dump/old.vma.zst",
                trash_path="../outside.txt",
                moved_at=timezone.now() - timedelta(days=10),
                metadata={"storage_id": storage.storage_id},
            )

            purge_expired_trash(max_age_days=1)

            trash_item = TrashItem.objects.get()
            self.assertEqual(trash_item.restore_status, TrashItem.RestoreStatus.TRASHED)
            self.assertTrue(outside.exists())
            self.assertEqual(outside.read_bytes(), b"keep")
            event = AuditEvent.objects.get(action="trash.purge")
            self.assertEqual(event.outcome, "partial")
            self.assertEqual(event.details["errors"][0]["error"], "Invalid storage path.")

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_likely_orphan_can_be_moved_to_trash_and_restored(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            dump_dir = root / "dump"
            dump_dir.mkdir()
            original = dump_dir / "orphan.vma.zst"
            original.write_bytes(b"orphan")
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="dump/orphan.vma.zst",
                entry_type=FileInventory.EntryType.FILE,
                size_bytes=original.stat().st_size,
                classification=FileInventory.Classification.LIKELY_ORPHAN,
                classification_reason="No matching Proxmox disk reference.",
            )
            expected_browser_url = browser_url(storage.mount_ref)

            response = self.client.post(
                reverse("core:storage_trash_file", args=[storage.mount_ref]),
                {"path": "dump/orphan.vma.zst", "confirm_basic": "yes", "next": expected_browser_url},
            )

            self.assertRedirects(response, expected_browser_url, fetch_redirect_response=False)
            self.assertFalse(original.exists())
            trash_item = TrashItem.objects.get()
            trash_path = root / trash_item.trash_path
            self.assertTrue(trash_path.exists())
            self.assertEqual(trash_path.read_bytes(), b"orphan")
            self.assertEqual(
                AuditEvent.objects.get(action="file.trashed").object_id, f"{storage.mount_ref}:dump/orphan.vma.zst"
            )

            trash_url = reverse("core:storage_trash", args=[storage.mount_ref])
            response = self.client.get(trash_url)
            self.assertContains(response, "dump/orphan.vma.zst")

            response = self.client.post(
                reverse("core:storage_restore_file", args=[trash_item.id]),
                {"next": trash_url},
            )

            self.assertRedirects(response, trash_url)
            trash_item.refresh_from_db()
            self.assertEqual(trash_item.restore_status, TrashItem.RestoreStatus.RESTORED)
            self.assertTrue(original.exists())
            self.assertEqual(original.read_bytes(), b"orphan")
            self.assertFalse(trash_path.exists())
            self.assertEqual(
                AuditEvent.objects.get(action="file.restored").object_id, f"{storage.mount_ref}:dump/orphan.vma.zst"
            )
            task_names = [task["name"] for task in recent_task_page(limit=10).tasks]
            self.assertIn("Move file to trash", task_names)
            self.assertIn("Restore file", task_names)

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_directory_can_be_moved_to_trash_and_restored(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "template" / "iso" / "upload-folder"
            folder.mkdir(parents=True)
            child = folder / "disk.qcow2"
            child.write_bytes(b"disk")
            storage = StorageMount.objects.create(
                storage_id="nfs-fs",
                display_name="nfs-fs",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="template/iso/upload-folder",
                entry_type=FileInventory.EntryType.DIRECTORY,
                classification=FileInventory.Classification.UNKNOWN,
                content_category="unknown",
            )
            expected_browser_url = browser_url(storage.mount_ref)

            response = self.client.post(
                reverse("core:storage_trash_file", args=[storage.mount_ref]),
                {
                    "path": "template/iso/upload-folder",
                    "confirm_basic": "yes",
                    "confirm_risk": "yes",
                    "next": expected_browser_url,
                },
            )

            self.assertRedirects(response, expected_browser_url, fetch_redirect_response=False)
            self.assertFalse(folder.exists())
            trash_item = TrashItem.objects.get()
            trash_path = root / trash_item.trash_path
            self.assertTrue((trash_path / "disk.qcow2").exists())
            self.assertEqual(trash_item.metadata["original_entry_type"], FileInventory.EntryType.DIRECTORY)

            trash_url = reverse("core:storage_trash", args=[storage.mount_ref])
            response = self.client.post(
                reverse("core:storage_restore_file", args=[trash_item.id]),
                {"next": trash_url},
            )

            self.assertRedirects(response, trash_url)
            trash_item.refresh_from_db()
            self.assertEqual(trash_item.restore_status, TrashItem.RestoreStatus.RESTORED)
            self.assertTrue(child.exists())
            self.assertFalse(trash_path.exists())

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_empty_guest_image_directory_can_be_moved_to_trash(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "images" / "505"
            folder.mkdir(parents=True)
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="images/505",
                entry_type=FileInventory.EntryType.DIRECTORY,
                classification=FileInventory.Classification.UNKNOWN,
                content_category="vm_image_directory",
            )
            expected_browser_url = browser_url(storage.mount_ref)

            response = self.client.post(
                reverse("core:storage_trash_file", args=[storage.mount_ref]),
                {
                    "path": "images/505",
                    "confirm_basic": "yes",
                    "confirm_risk": "yes",
                    "next": expected_browser_url,
                },
            )

            self.assertRedirects(response, expected_browser_url, fetch_redirect_response=False)
            self.assertFalse(folder.exists())
            trash_item = TrashItem.objects.get()
            self.assertTrue((root / trash_item.trash_path).is_dir())

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_non_empty_guest_image_directory_cannot_be_moved_to_trash(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "images" / "505"
            folder.mkdir(parents=True)
            disk = folder / "vm-505-disk-0.qcow2"
            disk.write_bytes(b"disk")
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="images/505",
                entry_type=FileInventory.EntryType.DIRECTORY,
                classification=FileInventory.Classification.UNKNOWN,
                content_category="vm_image_directory",
            )
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="images/505/vm-505-disk-0.qcow2",
                entry_type=FileInventory.EntryType.FILE,
                classification=FileInventory.Classification.UNKNOWN,
                content_category="vm_disk",
            )
            expected_browser_url = browser_url(storage.mount_ref)

            response = self.client.post(
                reverse("core:storage_trash_file", args=[storage.mount_ref]),
                {
                    "path": "images/505",
                    "confirm_basic": "yes",
                    "confirm_risk": "yes",
                    "next": expected_browser_url,
                },
            )

            self.assertRedirects(response, expected_browser_url, fetch_redirect_response=False)
            self.assertTrue(folder.exists())
            self.assertFalse(TrashItem.objects.exists())
            messages = [str(message) for message in get_messages(response.wsgi_request)]
            self.assertIn("Guest image/private directories must be empty before they can be moved to trash.", messages)

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_multiple_files_can_be_moved_to_trash(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            dump_dir = root / "dump"
            dump_dir.mkdir()
            first = dump_dir / "first.vma.zst"
            second = dump_dir / "second.vma.zst"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
            for path in ["dump/first.vma.zst", "dump/second.vma.zst"]:
                FileInventory.objects.create(
                    scan_run=scan,
                    storage=storage,
                    path=path,
                    entry_type=FileInventory.EntryType.FILE,
                    classification=FileInventory.Classification.LIKELY_ORPHAN,
                )
            expected_browser_url = browser_url(storage.mount_ref)

            response = self.client.post(
                reverse("core:storage_trash_file", args=[storage.mount_ref]),
                {
                    "path": ["dump/first.vma.zst", "dump/second.vma.zst"],
                    "confirm_basic": "yes",
                    "next": expected_browser_url,
                },
            )

            self.assertRedirects(response, expected_browser_url, fetch_redirect_response=False)
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())
            self.assertEqual(TrashItem.objects.count(), 2)
            self.assertEqual(AuditEvent.objects.filter(action="file.trashed", outcome="success").count(), 2)
            self.assertFalse(AuditEvent.objects.filter(action="file.trashed", outcome="failed").exists())

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_partial_bulk_trash_keeps_successes_audited_and_asks_the_operator(self):
        """A fan-out is not atomic: what happened must be recorded and reported."""
        user = get_user_model().objects.create_user(username="bulk-viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            dump_dir = root / "dump"
            dump_dir.mkdir()
            paths = []
            for index in range(3):
                target = dump_dir / f"file{index}.vma.zst"
                target.write_bytes(b"payload")
                paths.append(f"dump/file{index}.vma.zst")
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
            for path in paths:
                FileInventory.objects.create(
                    scan_run=scan,
                    storage=storage,
                    path=path,
                    entry_type=FileInventory.EntryType.FILE,
                    classification=FileInventory.Classification.LIKELY_ORPHAN,
                )
            expected_browser_url = browser_url(storage.mount_ref)

            real_trash = move_file_to_trash

            def fail_the_second(*args, **kwargs):
                if kwargs["entry"].path == paths[1]:
                    raise StorageActionError("Blocked by preflight.")
                return real_trash(*args, **kwargs)

            with patch("core.views.storage.move_file_to_trash", side_effect=fail_the_second):
                response = self.client.post(
                    reverse("core:storage_trash_file", args=[storage.mount_ref]),
                    {"path": paths, "confirm_basic": "yes", "next": expected_browser_url},
                )

            self.assertRedirects(response, expected_browser_url, fetch_redirect_response=False)
            # The two that worked really happened, and are recorded as such.
            self.assertEqual(TrashItem.objects.count(), 2)
            self.assertEqual(AuditEvent.objects.filter(action="file.trashed", outcome="success").count(), 2)
            # The one that failed gets its own row, not only a line inside the
            # aggregate: a per-file question needs a per-file record to answer it.
            failure = AuditEvent.objects.get(action="file.trashed", outcome="failed")
            self.assertEqual(failure.path, paths[1])

            bulk = AuditEvent.objects.get(action="file.bulk_operation")
            self.assertEqual(bulk.outcome, "warning")
            self.assertEqual(bulk.details["succeeded"], [paths[0], paths[2]])
            self.assertEqual(bulk.details["failed"], [{"path": paths[1], "error": "Blocked by preflight."}])
            self.assertTrue(bulk.details["question"])
            self.assertEqual(bulk.details["retry"]["paths"], [paths[1]])

            reported = [str(message) for message in get_messages(response.wsgi_request)]
            self.assertTrue(any("2 of 3 moved to trash" in message for message in reported))
            self.assertTrue(any(paths[1] in message for message in reported))

            # The unanswered question is pinned and counted.
            page = recent_task_page(limit=5)
            self.assertEqual(page.questions_pending, 1)
            self.assertEqual(page.tasks[0]["question"]["kind"], "bulk_file_partial")

            # And it outlives retention. Age it past the cutoff so the only clause
            # that can still keep it is the unanswered-question one: while it was
            # fresh, the timestamp clause matched on its own and hid whether that
            # second clause worked at all.
            AuditEvent.objects.filter(pk=bulk.pk).update(
                timestamp=timezone.now() - timedelta(minutes=RECENT_TASK_RETENTION_MINUTES + 5)
            )
            aged = recent_task_page(limit=5)
            self.assertEqual(aged.questions_pending, 1)
            self.assertEqual(aged.tasks[0]["question"]["kind"], "bulk_file_partial")

            self.client.post(
                reverse("core:dismiss_task_question"),
                {"task_id": f"file:{bulk.id}", "answer": "accepted"},
            )
            bulk.refresh_from_db()
            self.assertTrue(bulk.details["question_dismissed"])
            self.assertEqual(recent_task_page(limit=5).questions_pending, 0)

            # The decision is its own durable fact, not a rewrite of the question.
            answered = AuditEvent.objects.get(action="file.bulk_operation.answered")
            self.assertEqual(answered.details["answer"], "accepted")
            self.assertEqual(answered.details["remaining"], 1)
            self.assertEqual(answered.details["question_event_id"], bulk.id)
            self.assertEqual(answered.module, "storage")

            # Answering twice must not log the decision twice.
            self.client.post(
                reverse("core:dismiss_task_question"),
                {"task_id": f"file:{bulk.id}", "answer": "retried"},
            )
            self.assertEqual(AuditEvent.objects.filter(action="file.bulk_operation.answered").count(), 1)

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_referenced_disk_needs_an_acknowledgement_but_is_never_forbidden(self):
        """A live reference is a reason to ask, not a reason to refuse forever.

        Refusing until the guest is detached in Proxmox assumes Proxmox can still
        be reached and the guest still exists. A node can die for good and be
        replaced by a differently named one; its guests' configs die with it, and
        the disk would then be unreachable through this app permanently. So the
        reference escalates the confirmation and the operator can answer past it —
        but the answer has to be given, and it is recorded.
        """
        """Trashing relocates the file, so a live reference is what matters, not power state.

        A stopped guest is not safety: the file leaves its volid either way and the
        guest breaks on its next boot instead of immediately. The operator's route
        is to detach the disk in Proxmox first, which the refusal has to say.
        """
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_dir = root / "images" / "100"
            image_dir.mkdir(parents=True)
            disk = image_dir / "vm-100-disk-0.qcow2"
            disk.write_bytes(b"disk")
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            ProxmoxEndpoint.objects.create(
                cluster=self.cluster,
                name="pve-node-1",
                url="https://pve-node-1.example.com:8006",
                enabled=True,
                details={"node": "pve-node-1"},
            )
            scan = ScanRun.objects.create(
                status=ScanRun.Status.COMPLETED,
                storage_gate_status={"nfs-vm": {"ok": True, "status": "ok"}},
            )
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="images/100/vm-100-disk-0.qcow2",
                derived_volid="nfs-vm:100/vm-100-disk-0.qcow2",
                entry_type=FileInventory.EntryType.FILE,
                content_category="vm_disk",
                classification=FileInventory.Classification.REFERENCED,
            )
            # A bound datastore, so the preflight reads the catalog rather than
            # falling back, and a guest configuration that still points at the disk.
            files_url = browser_url(storage.mount_ref)
            ClusterStorageNodeState.objects.create(
                cluster_storage=ClusterStorage.objects.get(cluster=self.cluster, storage_id="nfs-vm"),
                node="pve-node-1",
                present=True,
                active=True,
                enabled=True,
            )
            CurrentGuestInventory.objects.create(
                cluster=self.cluster,
                source_scan=scan,
                node="pve-node-1",
                object_type=CurrentGuestInventory.ObjectType.VM,
                vmid=100,
                name="restore-test",
                status="stopped",
                runtime_observed_at=timezone.now(),
                disk_references=["nfs-vm:100/vm-100-disk-0.qcow2"],
                observed_at=timezone.now(),
            )

            trash_url = reverse("core:storage_trash_file", args=[storage.mount_ref])
            with (
                patch("core.services.storage_catalog.refresh_storage_catalog"),
                patch("core.services.proxmox.ProxmoxClient.guest_status", return_value="stopped"),
            ):
                unanswered = self.client.post(
                    trash_url,
                    {
                        "path": "images/100/vm-100-disk-0.qcow2",
                        "confirm_basic": "yes",
                        "next": files_url,
                    },
                    follow=True,
                )

            # The reference is escalated, not waived: skipping the question keeps
            # the file exactly where it is, and the refusal is auditable.
            self.assertEqual(unanswered.status_code, 200)
            self.assertTrue(disk.exists())
            self.assertFalse(TrashItem.objects.exists())
            self.assertFalse(AuditEvent.objects.filter(action="file.trashed", outcome="success").exists())
            self.assertTrue(AuditEvent.objects.filter(action="file.trashed", outcome="failed").exists())

            with (
                patch("core.services.storage_catalog.refresh_storage_catalog"),
                patch("core.services.proxmox.ProxmoxClient.guest_status", return_value="stopped"),
            ):
                answered = self.client.post(
                    trash_url,
                    {
                        "path": "images/100/vm-100-disk-0.qcow2",
                        "confirm_basic": "yes",
                        "confirm_risk": "yes",
                        "next": files_url,
                    },
                    follow=True,
                )

            self.assertEqual(answered.status_code, 200)
            self.assertFalse(disk.exists())
            self.assertTrue(TrashItem.objects.exists())
            self.assertTrue(AuditEvent.objects.filter(action="file.trashed", outcome="success").exists())

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_running_referenced_file_cannot_be_moved_to_trash(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_dir = root / "images" / "100"
            image_dir.mkdir(parents=True)
            disk = image_dir / "vm-100-disk-0.qcow2"
            disk.write_bytes(b"disk")
            storage = StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="nfs-vm",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            scan = ScanRun.objects.create(
                status=ScanRun.Status.COMPLETED,
                storage_gate_status={"nfs-vm": {"ok": True, "status": "ok"}},
            )
            ProxmoxInventory.objects.create(
                scan_run=scan,
                node="pve-node-1",
                object_type=ProxmoxInventory.ObjectType.VM,
                vmid=100,
                name="running-test",
                status="running",
                disk_references=["nfs-vm:100/vm-100-disk-0.qcow2"],
            )
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="images/100/vm-100-disk-0.qcow2",
                derived_volid="nfs-vm:100/vm-100-disk-0.qcow2",
                entry_type=FileInventory.EntryType.FILE,
                content_category="vm_disk",
                classification=FileInventory.Classification.REFERENCED,
            )

            response = self.client.post(
                reverse("core:storage_trash_file", args=[storage.mount_ref]),
                {
                    "path": "images/100/vm-100-disk-0.qcow2",
                    "confirm_basic": "yes",
                    "confirm_risk": "yes",
                    "next": browser_url(storage.mount_ref),
                },
            )

            self.assertEqual(response.status_code, 302)
            self.assertTrue(disk.exists())
            self.assertFalse(TrashItem.objects.exists())
            self.assertFalse(AuditEvent.objects.filter(action="file.trashed", outcome="success").exists())
            # The refusal is recorded with the reason the operator was given, so
            # "why is this file still here" has an answer that outlives the toast.
            refusal = AuditEvent.objects.get(action="file.trashed", outcome="failed")
            self.assertTrue(refusal.details["error"])
            self.assertEqual(refusal.path, "images/100/vm-100-disk-0.qcow2")

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_file_can_be_renamed_with_confirmation_and_directory_refresh(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso_dir = root / "template" / "iso"
            iso_dir.mkdir(parents=True)
            original = iso_dir / "old.iso"
            original.write_bytes(b"iso")
            storage = StorageMount.objects.create(
                storage_id="nfs-fs",
                display_name="nfs-fs",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            scan = ScanRun.objects.create(
                status=ScanRun.Status.COMPLETED,
                storage_gate_status={"nfs-fs": {"ok": True, "status": "ok"}},
            )
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="template",
                entry_type=FileInventory.EntryType.DIRECTORY,
                content_category="template_directory",
            )
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="template/iso",
                entry_type=FileInventory.EntryType.DIRECTORY,
                content_category="iso",
            )
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="template/iso/old.iso",
                entry_type=FileInventory.EntryType.FILE,
                content_category="iso",
                classification=FileInventory.Classification.UNKNOWN,
            )
            expected_browser_url = f"{browser_url(storage.mount_ref)}?path=template%2Fiso"

            response = self.client.post(
                reverse("core:storage_rename_file", args=[storage.mount_ref]),
                {
                    "path": "template/iso/old.iso",
                    "new_name": "new.iso",
                    "confirm_basic": "yes",
                    "next": expected_browser_url,
                },
            )

            self.assertRedirects(response, expected_browser_url, fetch_redirect_response=False)
            self.assertFalse(original.exists())
            self.assertEqual((iso_dir / "new.iso").read_bytes(), b"iso")
            self.assertFalse(FileInventory.objects.filter(scan_run=scan, path="template/iso/old.iso").exists())
            self.assertTrue(FileInventory.objects.filter(scan_run=scan, path="template/iso/new.iso").exists())
            self.assertEqual(AuditEvent.objects.get(action="file.renamed").details["old_path"], "template/iso/old.iso")

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_file_can_be_moved_with_confirmation_and_directory_refresh(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "template" / "iso"
            target_dir = root / "snippets"
            source_dir.mkdir(parents=True)
            target_dir.mkdir()
            original = source_dir / "move-me.iso"
            original.write_bytes(b"move")
            storage = StorageMount.objects.create(
                storage_id="nfs-fs",
                display_name="nfs-fs",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            scan = ScanRun.objects.create(
                status=ScanRun.Status.COMPLETED,
                storage_gate_status={"nfs-fs": {"ok": True, "status": "ok"}},
            )
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="template",
                entry_type=FileInventory.EntryType.DIRECTORY,
                content_category="template_directory",
            )
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="template/iso",
                entry_type=FileInventory.EntryType.DIRECTORY,
                content_category="iso",
            )
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="snippets",
                entry_type=FileInventory.EntryType.DIRECTORY,
                content_category="snippets",
            )
            FileInventory.objects.create(
                scan_run=scan,
                storage=storage,
                path="template/iso/move-me.iso",
                entry_type=FileInventory.EntryType.FILE,
                content_category="iso",
                classification=FileInventory.Classification.UNKNOWN,
            )
            expected_browser_url = f"{browser_url(storage.mount_ref)}?path=template%2Fiso"

            response = self.client.post(
                reverse("core:storage_move_file", args=[storage.mount_ref]),
                {
                    "path": "template/iso/move-me.iso",
                    "new_path": "snippets",
                    "confirm_basic": "yes",
                    "next": expected_browser_url,
                },
            )

            self.assertRedirects(response, expected_browser_url, fetch_redirect_response=False)
            self.assertFalse(original.exists())
            self.assertEqual((target_dir / "move-me.iso").read_bytes(), b"move")
            self.assertFalse(FileInventory.objects.filter(scan_run=scan, path="template/iso/move-me.iso").exists())
            self.assertTrue(FileInventory.objects.filter(scan_run=scan, path="snippets/move-me.iso").exists())
            self.assertEqual(
                AuditEvent.objects.get(action="file.moved").details["old_path"], "template/iso/move-me.iso"
            )

    @override_settings(STORAGE_WRITE_ENABLED=True)
    def test_multiple_files_can_be_moved_to_the_same_directory(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "template" / "iso"
            target_dir = root / "snippets"
            source_dir.mkdir(parents=True)
            target_dir.mkdir()
            first = source_dir / "first.iso"
            second = source_dir / "second.iso"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            storage = StorageMount.objects.create(
                storage_id="nfs-fs",
                display_name="nfs-fs",
                path=root.as_posix(),
                expected_consumers=["pve-node-1"],
            )
            scan = ScanRun.objects.create(
                status=ScanRun.Status.COMPLETED,
                storage_gate_status={"nfs-fs": {"ok": True, "status": "ok"}},
            )
            for path, entry_type, category in [
                ("template", FileInventory.EntryType.DIRECTORY, "template_directory"),
                ("template/iso", FileInventory.EntryType.DIRECTORY, "iso"),
                ("snippets", FileInventory.EntryType.DIRECTORY, "snippets"),
                ("template/iso/first.iso", FileInventory.EntryType.FILE, "iso"),
                ("template/iso/second.iso", FileInventory.EntryType.FILE, "iso"),
            ]:
                FileInventory.objects.create(
                    scan_run=scan,
                    storage=storage,
                    path=path,
                    entry_type=entry_type,
                    content_category=category,
                    classification=FileInventory.Classification.UNKNOWN,
                )
            expected_browser_url = f"{browser_url(storage.mount_ref)}?path=template%2Fiso"

            response = self.client.post(
                reverse("core:storage_move_file", args=[storage.mount_ref]),
                {
                    "path": ["template/iso/first.iso", "template/iso/second.iso"],
                    "new_path": "snippets",
                    "confirm_basic": "yes",
                    "next": expected_browser_url,
                },
            )

            self.assertRedirects(response, expected_browser_url, fetch_redirect_response=False)
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())
            self.assertEqual((target_dir / "first.iso").read_bytes(), b"first")
            self.assertEqual((target_dir / "second.iso").read_bytes(), b"second")
            self.assertEqual(AuditEvent.objects.filter(action="file.moved").count(), 2)
            self.assertTrue(FileInventory.objects.filter(scan_run=scan, path="snippets/first.iso").exists())
            self.assertTrue(FileInventory.objects.filter(scan_run=scan, path="snippets/second.iso").exists())

    def test_dashboard_keeps_last_completed_gate_while_new_scan_is_queued(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)
        storage = StorageMount.objects.create(
            storage_id="nfs-fs",
            display_name="nfs-fs",
            path="/storages/truenas-fs",
            expected_consumers=["pve-node-1"],
        )
        completed_scan = ScanRun.objects.create(
            status=ScanRun.Status.COMPLETED,
            progress_message="Completed scan",
            storage_gate_status={
                "nfs-fs": {
                    "ok": True,
                    "status": "ok",
                    "expected_consumers": ["pve-node-1"],
                    "missing_consumers": [],
                }
            },
        )
        FileInventory.objects.create(
            scan_run=completed_scan,
            storage=storage,
            path="images",
            entry_type=FileInventory.EntryType.DIRECTORY,
        )
        ProxmoxInventory.objects.create(
            scan_run=completed_scan,
            node="pve-node-1",
            object_type=ProxmoxInventory.ObjectType.STORAGE,
            name="nfs-fs",
            status="1",
            config={
                "storage": "nfs-fs",
                "type": "nfs",
                "server": "truenas.example.com",
                "export": "/mnt/tank/proxmox-fs",
                "path": "/mnt/pve/nfs-fs",
                "options": "vers=4.2,nconnect=4",
                "preallocation": "default",
                "content": "rootdir,images",
                "shared": 1,
                "active": 1,
            },
        )
        ScanRun.objects.create(status=ScanRun.Status.QUEUED, progress_message="Queued scan")
        ScanRun.objects.filter(pk=completed_scan.pk).update(
            filesystem_scan_at=datetime(2026, 6, 26, 8, 15, 30, tzinfo=timezone.get_current_timezone()),
            finished_at=datetime(2026, 6, 26, 8, 15, 31, tzinfo=timezone.get_current_timezone()),
        )

        with patch(
            "core.views.common.storage_space_info",
            return_value=StorageSpaceInfo(
                ok=True,
                total_bytes=10 * 1024**4,
                available_bytes=4 * 1024**4,
                used_bytes=6 * 1024**4,
                used_percent=60.0,
                filesystem_type="nfs4",
                source="truenas.example.com:/mnt/tank/proxmox-fs",
                mount_point="/storages/truenas-fs",
                access_mode="read_write",
                access_label="Read/write",
                access_class="success",
                can_write=True,
            ),
        ):
            response = self.client.get(reverse("core:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "nfs-fs")
        self.assertContains(response, "ok")
        self.assertContains(response, "Free / Total")
        self.assertContains(response, "4.0")
        self.assertContains(response, "10.0")
        self.assertContains(response, "truenas.example.com")
        self.assertContains(response, "vers=4.2,nconnect=4")
        self.assertContains(response, "nfs4")
        self.assertContains(response, "PVE-helper access")
        self.assertContains(response, "Read/write")
        self.assertContains(response, "Latest Scan")
        self.assertContains(response, "2026-06-26 08:15:30")
        self.assertContains(response, "Queued scan")

    def test_storage_overview_contains_catalog_and_links_to_pve_helper_settings(self):
        metadata_generation = uuid.uuid4()
        definition = ClusterStorage.objects.create(
            cluster=self.cluster,
            storage_id="shared-nfs",
            storage_type="nfs",
            shared=True,
            present=True,
            observed_metadata_generation=metadata_generation,
        )
        ClusterStorageNodeState.objects.create(
            cluster_storage=definition,
            node="pve1",
            active=True,
            enabled=True,
            present=True,
            observed_metadata_generation=metadata_generation,
        )

        overview = self.client.get(reverse("core:dashboard"))
        self.assertEqual(overview.status_code, 200)
        self.assertContains(overview, 'id="storage-catalog"')
        self.assertContains(overview, "Storage catalog")
        self.assertContains(overview, "shared-nfs")
        self.assertContains(overview, "Storage definitions")
        self.assertContains(overview, "Accessible mounts")
        self.assertContains(overview, reverse("core:settings_storage"))
        self.assertNotContains(overview, "Registered associations")
        # The manual's two-layer model must be named by the pages implementing it.
        self.assertContains(overview, "Layer 1 — what Proxmox reports through the API")

        settings_root = self.client.get(reverse("core:pve_helper_settings"))
        self.assertRedirects(settings_root, reverse("core:settings_storage"))
        storage_settings = self.client.get(reverse("core:settings_storage"))
        self.assertEqual(storage_settings.status_code, 200)
        self.assertContains(storage_settings, "PVE-helper Settings")
        self.assertContains(storage_settings, "Storage access")
        self.assertContains(storage_settings, "Registered associations")
        self.assertContains(storage_settings, "Layer 2 —")
        self.assertContains(storage_settings, "Register host mount")
        self.assertContains(storage_settings, "PVE-helper Settings", count=2)
        self.assertContains(storage_settings, f"{reverse('core:dashboard')}#storage-catalog")
        self.assertNotContains(storage_settings, "Storage definitions")

    def test_mount_registration_derives_identity_and_refuses_a_silent_near_match(self):
        metadata_generation = uuid.uuid4()
        definition = ClusterStorage.objects.create(
            cluster=self.cluster,
            storage_id="shared-nfs",
            storage_type="nfs",
            shared=True,
            present=True,
            config={"server": "nas.hq.local", "export": "/mnt/tank/vm"},
            observed_metadata_generation=metadata_generation,
        )
        ClusterStorageNodeState.objects.create(
            cluster_storage=definition,
            node="pve1",
            active=True,
            enabled=True,
            present=True,
            observed_metadata_generation=metadata_generation,
        )
        StorageMount.objects.create(
            storage_id="mount-existing",
            display_name="Same NAS, short name",
            path="/storages/other",
            relative_path="other",
            backend_identity="nas:/mnt/tank/vm",
        )

        page = self.client.get(reverse("core:settings_storage"))
        self.assertContains(page, 'data-derived-identity="nas.hq.local:/mnt/tank/vm"')
        # Scope and node instances are stated before the choice, not after submit.
        self.assertContains(page, "(nfs) — Shared")
        self.assertContains(page, 'data-nodes="pve1"')

        candidates = [{"relative_path": "nas", "filesystem_type": "nfs4"}]
        payload = {
            "cluster_storage": str(definition.pk),
            "relative_path": "nas",
            "display_name": "Production NFS",
            "backend_identity": "nas.hq.local:/mnt/tank/vm",
        }
        with patch("core.views.storage._mount_candidates", return_value=candidates):
            blocked = self.client.post(reverse("core:settings_storage"), payload)

        self.assertContains(blocked, "spelled differently")
        self.assertContains(blocked, "Same NAS, short name")
        self.assertContains(blocked, "Register as a different backend")
        # The rejected values survive so the operator can correct rather than retype.
        self.assertContains(blocked, 'value="nas.hq.local:/mnt/tank/vm"')
        self.assertFalse(ClusterStorageMount.objects.exists())

        health = MountHealth(available=True, writable=True, filesystem_type="nfs4")
        with (
            patch("core.views.storage._mount_candidates", return_value=candidates),
            patch("core.views.storage.mount_health", return_value=health),
        ):
            confirmed = self.client.post(
                reverse("core:settings_storage"),
                {**payload, "confirm_distinct_backend": "1"},
            )

        self.assertEqual(confirmed.status_code, 200)
        binding = ClusterStorageMount.objects.get()
        self.assertEqual(binding.cluster_storage, definition)
        self.assertEqual(binding.mount.identity_source, StorageMount.IdentitySource.DERIVED)

    def test_removing_a_mount_association_states_current_use_and_is_audited(self):
        """The page's only destructive action: prove it works and that it warns with facts."""
        metadata_generation = uuid.uuid4()
        definition = ClusterStorage.objects.create(
            cluster=self.cluster,
            storage_id="shared-nfs",
            storage_type="nfs",
            shared=True,
            present=True,
            content=["images", "iso"],
            config={"server": "nas.hq.local", "export": "/mnt/tank/vm"},
            observed_metadata_generation=metadata_generation,
        )
        ClusterStorageNodeState.objects.create(
            cluster_storage=definition,
            node="pve1",
            present=True,
            active=True,
            enabled=True,
            observed_metadata_generation=metadata_generation,
        )
        ClusterStorageVolumeObservation.objects.create(
            cluster_storage=definition,
            node="pve1",
            volid="shared-nfs:100/vm-100-disk-0.qcow2",
            vmid=100,
            content="images",
            observed_volume_generation=uuid.uuid4(),
            based_on_metadata_generation=metadata_generation,
            last_seen_at=timezone.now(),
        )
        # One running, one stopped, plus enough others to exercise the display cap.
        CurrentGuestInventory.objects.create(
            cluster=self.cluster,
            node="pve1",
            object_type="vm",
            vmid=100,
            status="running",
            disk_references=["shared-nfs:100/vm-100-disk-0.qcow2"],
            observed_at=timezone.now(),
        )
        for vmid in range(200, 208):
            CurrentGuestInventory.objects.create(
                cluster=self.cluster,
                node="pve1",
                object_type="vm",
                vmid=vmid,
                status="stopped",
                disk_references=[f"shared-nfs:{vmid}/vm-{vmid}-disk-0.qcow2"],
                observed_at=timezone.now(),
            )
        mount = StorageMount.objects.create(
            storage_id="mount-nas",
            display_name="NAS",
            path="/storages/nas",
            relative_path="nas",
            backend_identity="nas.hq.local:/mnt/tank/vm",
        )
        binding = bind_storage_mount(cluster_storage=definition, mount=mount)

        page = self.client.get(reverse("core:settings_storage"))
        self.assertContains(page, "content types images, iso")
        self.assertContains(page, "1 catalog volume(s)")
        # Running guests are named first so the cap can never hide them.
        self.assertContains(page, "referenced by 9 guest(s), 1 of them running: vm:100 (running)")
        self.assertContains(page, "and 3 more")
        self.assertContains(page, "Are you really sure?")

        # A non-numeric id is a stale form, not a server error.
        bad = self.client.post(
            reverse("core:settings_storage"),
            {"action": "remove_binding", "binding_id": "not-a-number"},
        )
        self.assertEqual(bad.status_code, 200)
        self.assertContains(bad, "Mount association no longer exists.")
        self.assertTrue(ClusterStorageMount.objects.filter(pk=binding.pk).exists())

        removed = self.client.post(
            reverse("core:settings_storage"),
            {"action": "remove_binding", "binding_id": str(binding.pk)},
        )

        self.assertEqual(removed.status_code, 200)
        self.assertFalse(ClusterStorageMount.objects.filter(pk=binding.pk).exists())
        # The mount itself survives; only the association is undone.
        self.assertTrue(StorageMount.objects.filter(pk=mount.pk).exists())
        event = AuditEvent.objects.get(action="storage.mount.unregistered")
        self.assertEqual(event.details["storage_id"], "shared-nfs")
        self.assertEqual(event.details["scope"], "shared")
        # And the datastore has genuinely lost its file access.
        view = storage_view(ClusterStorage.objects.get(pk=definition.pk))
        self.assertFalse(view.capabilities.can_browse_files)
        self.assertIn("No host mount is registered", view.capabilities.browse_files_reason)

    def test_node_local_datastore_without_an_active_instance_cannot_be_chosen(self):
        metadata_generation = uuid.uuid4()
        definition = ClusterStorage.objects.create(
            cluster=self.cluster,
            storage_id="local-dir",
            storage_type="dir",
            shared=False,
            present=True,
            config={"path": "/var/lib/vz"},
            observed_metadata_generation=metadata_generation,
        )
        ClusterStorageNodeState.objects.create(
            cluster_storage=definition,
            node="pve1",
            present=True,
            active=False,
            enabled=True,
            observed_metadata_generation=metadata_generation,
        )

        page = self.client.get(reverse("core:settings_storage"))

        self.assertContains(page, "no active node instance")
        self.assertContains(page, "(dir) — Node-local")

    def test_recent_tasks_endpoint_paginates_scans(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        scans = [
            ScanRun.objects.create(status=ScanRun.Status.COMPLETED, progress_message=f"Scan {index}")
            for index in range(6)
        ]
        AuditEvent.objects.create(
            user=user,
            username="viewer",
            action="scan.queued",
            object_type="scan_run",
            object_id=str(scans[-1].id),
        )

        response = self.client.get(reverse("core:recent_tasks"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["tasks"]), 5)
        self.assertEqual(payload["total"], 6)
        self.assertTrue(payload["has_next"])
        self.assertFalse(payload["has_previous"])
        self.assertEqual(payload["tasks"][0]["initiator"], "viewer")

        response = self.client.get(reverse("core:recent_tasks"), {"page": "1"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["tasks"]), 1)
        self.assertFalse(payload["has_next"])
        self.assertTrue(payload["has_previous"])

    def test_recent_tasks_hide_completed_scans_after_retention_window(self):
        old_scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED, progress_message="Old scan")
        ScanRun.objects.filter(pk=old_scan.pk).update(
            created_at=timezone.now() - timedelta(hours=2),
            finished_at=timezone.now() - timedelta(minutes=61),
        )
        ScanRun.objects.create(
            status=ScanRun.Status.COMPLETED,
            progress_message="Fresh scan",
            finished_at=timezone.now() - timedelta(minutes=59),
        )

        task_page = recent_task_page()

        self.assertEqual(task_page.total, 1)
        self.assertIn("Fresh scan", task_page.tasks[0]["details"])

    def test_recent_tasks_show_queued_inflate_until_terminal_event_exists(self):
        queued = AuditEvent.objects.create(
            username="viewer",
            action="file.inflate_queued",
            object_type="file",
            object_id="nfs-vm:images/502/vm-502-disk-0.qcow2",
            details={
                "storage_id": "nfs-vm",
                "storage_name": "nfs-vm",
                "path": "images/502/vm-502-disk-0.qcow2",
                "target_preallocation": "full",
            },
        )

        task_page = recent_task_page(limit=10)

        self.assertEqual(task_page.total, 1)
        self.assertEqual(task_page.tasks[0]["name"], "Inflate disk (full)")
        self.assertEqual(task_page.tasks[0]["status"], "Queued")
        self.assertIsNone(task_page.tasks[0]["finished_at"])

        AuditEvent.objects.filter(pk=queued.pk).update(timestamp=timezone.now() - timedelta(seconds=30))
        AuditEvent.objects.create(
            username="viewer",
            action="file.inflated",
            object_type="file",
            object_id="nfs-vm:images/502/vm-502-disk-0.qcow2",
            details={
                "storage_id": "nfs-vm",
                "storage_name": "nfs-vm",
                "path": "images/502/vm-502-disk-0.qcow2",
                "target_preallocation": "full",
            },
        )

        task_page = recent_task_page(limit=10)

        self.assertEqual(task_page.total, 1)
        self.assertEqual(task_page.tasks[0]["name"], "Inflate disk (full)")
        self.assertEqual(task_page.tasks[0]["status"], "Completed")
        self.assertIsNotNone(task_page.tasks[0]["finished_at"])

    def test_recent_tasks_show_running_guest_upid_event(self):
        AuditEvent.objects.create(
            username="operator",
            action="guest.power.start",
            object_type="guest",
            object_id="vm:500",
            outcome="running",
            details={
                "node": "pve1",
                "vmid": 500,
                "target_type": "vm",
                "name": "Lab VM",
                "proxmox_task_upid": "UPID:pve1:start:500:root@pam:",
                "proxmox_task_node": "pve1",
            },
        )

        task_page = recent_task_page(limit=10)

        self.assertEqual(task_page.total, 1)
        self.assertEqual(task_page.tasks[0]["name"], "Power on")
        self.assertEqual(task_page.tasks[0]["status"], "Running")
        self.assertEqual(task_page.tasks[0]["status_class"], "running")
        self.assertIsNone(task_page.tasks[0]["finished_at"])
        self.assertEqual(task_page.tasks[0]["server"], "pve1")
        self.assertTrue(task_page.tasks[0]["cancelable"])

    def test_cancel_recent_guest_task_stops_proxmox_task(self):
        user = get_user_model().objects.create_user(username="operator", password="unused")
        self.client.force_login(user)
        event = AuditEvent.objects.create(
            cluster=self.cluster,
            user=user,
            username="operator",
            action="guest.power.shutdown",
            object_type="guest",
            object_id="vm:500",
            outcome="running",
            details={
                "node": "pve1",
                "vmid": 500,
                "target_type": "vm",
                "name": "Lab VM",
                "proxmox_endpoint": "https://pve1.example.invalid:8006/api2/json",
                "proxmox_task_upid": "UPID:pve1:shutdown:500:root@pam:",
                "proxmox_task_node": "pve1",
            },
        )
        stopped_tasks = []

        class FakeProxmoxClient:
            endpoint = "https://pve1.example.invalid:8006/api2/json"

            def stop_task(self, *, node, upid):
                stopped_tasks.append((self.endpoint, node, upid))

        ProxmoxEndpoint.objects.create(
            cluster=self.cluster,
            name="pve1",
            url="https://pve1.example.invalid:8006/api2/json",
            enabled=True,
        )
        with patch(
            "core.services.cluster_resolver.client_for_endpoint",
            return_value=FakeProxmoxClient(),
        ):
            response = self.client.post(reverse("core:cancel_recent_task"), {"task_id": f"guest:{event.id}"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        event.refresh_from_db()
        self.assertEqual(event.outcome, "cancelled")
        self.assertEqual(event.details["cancelled_by"], "operator")
        self.assertEqual(
            stopped_tasks, [("https://pve1.example.invalid:8006/api2/json", "pve1", "UPID:pve1:shutdown:500:root@pam:")]
        )
        self.assertTrue(AuditEvent.objects.filter(action="task.cancelled", outcome="success").exists())

    def test_cancel_recent_completed_guest_task_is_rejected(self):
        user = get_user_model().objects.create_user(username="operator", password="unused")
        self.client.force_login(user)
        event = AuditEvent.objects.create(
            user=user,
            username="operator",
            action="guest.power.shutdown",
            object_type="guest",
            object_id="vm:500",
            outcome="success",
            details={
                "node": "pve1",
                "proxmox_task_upid": "UPID:pve1:shutdown:500:root@pam:",
                "proxmox_task_node": "pve1",
            },
        )

        response = self.client.post(reverse("core:cancel_recent_task"), {"task_id": f"guest:{event.id}"})

        self.assertEqual(response.status_code, 409)
        event.refresh_from_db()
        self.assertEqual(event.outcome, "success")

    def test_recent_tasks_serializes_file_refresh_metadata(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        event = AuditEvent.objects.create(
            username="viewer",
            action="file.inflated",
            object_type="file",
            object_id="nfs-vm:images/502/vm-502-disk-0.qcow2",
            details={
                "storage_id": "nfs-vm",
                "storage_name": "nfs-vm",
                "path": "images/502/vm-502-disk-0.qcow2",
                "target_preallocation": "metadata",
            },
        )

        response = self.client.get(reverse("core:recent_tasks"))

        self.assertEqual(response.status_code, 200)
        task = response.json()["tasks"][0]
        self.assertEqual(task["id"], f"file:{event.id}")
        self.assertEqual(task["action"], "file.inflated")
        self.assertEqual(task["storage_id"], "nfs-vm")
        self.assertEqual(task["path"], "images/502/vm-502-disk-0.qcow2")
        self.assertEqual(task["path_parent"], "images/502")
        self.assertGreater(task["finished_at_ms"], 0)

    def test_recent_tasks_include_download_file_actions(self):
        AuditEvent.objects.create(
            username="viewer",
            action="file.downloaded",
            object_type="file",
            object_id="nfs-fs:template/iso/test.iso",
            details={
                "storage_id": "nfs-fs",
                "storage_name": "nfs-fs",
                "path": "template/iso/test.iso",
            },
        )

        task_page = recent_task_page(limit=10)

        self.assertEqual(task_page.total, 1)
        self.assertEqual(task_page.tasks[0]["name"], "Download file")
        self.assertEqual(task_page.tasks[0]["status"], "Completed")

    def test_recent_tasks_include_scheduled_action_runs(self):
        user = get_user_model().objects.create_user(username="scheduler", password="unused")
        action = ScheduledAction.objects.create(
            cluster=self.cluster,
            name="Night shutdown",
            action_type=ScheduledAction.ActionType.SHUTDOWN,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=500,
            target_name_snapshot="Lab VM",
            target_node="pve1",
            created_by=user,
        )
        run = ScheduledActionRun.objects.create(
            scheduled_action=action,
            planned_for=timezone.now(),
            occurrence_key="recent",
            status=ScheduledActionRun.Status.COMPLETED,
            outcome=ScheduledActionRun.Outcome.SUCCESS_NOOP,
            started_at=timezone.now(),
            finished_at=timezone.now(),
        )

        task_page = recent_task_page(limit=10)

        self.assertEqual(task_page.total, 1)
        task = task_page.tasks[0]
        self.assertEqual(task["id"], f"scheduled_action:{run.id}")
        self.assertEqual(task["kind"], "scheduled_action")
        self.assertEqual(task["name"], "Scheduled shutdown")
        self.assertEqual(task["target"], "VM 500 (Lab VM)")
        self.assertEqual(task["status"], "Completed - no action needed")
        self.assertEqual(task["status_class"], "completed")
        self.assertEqual(task["initiator"], "scheduler")
        self.assertEqual(task["server"], "pve1")

    def test_vms_overview_uses_full_width_table_layout(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        self._live_guest(status="running")
        with (
            patch(
                "core.views.common.fetch_live_guest_inventory",
                side_effect=AssertionError("passive overview must not read provider guest inventory"),
            ),
            patch(
                "core.views.common.fetch_live_guest_status",
                side_effect=AssertionError("passive overview must not read provider guest status"),
            ),
        ):
            response = self.client.get(reverse("core:vms_overview"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-vm-overview")
        self.assertContains(response, "data-vm-select")
        self.assertContains(response, "data-vm-bulk-form")
        self.assertContains(response, "data-vm-agent-info-url")
        self.assertEqual(LIVE_GUEST_INVENTORY_CACHE_SECONDS, 30)
        self.assertContains(response, "Power state updates right after an action and otherwise refreshes periodically.")
        self.assertContains(response, "Has snapshot")
        self.assertNotContains(response, "guest-list-pane")

    def test_vms_workspace_guest_list_sorts_by_name(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)
        live_guests = [
            self._live_guest(vmid=500, name="ubuntu-test", node="pve1"),
            self._live_guest(vmid=100, name="pve3-veeam-worker", node="pve1"),
            self._live_guest(vmid=501, name="SophosTest", node="pve1"),
        ]

        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=live_guests),
            patch("core.views.common.fetch_live_guest_status", return_value={}),
        ):
            response = self.client.get(reverse("core:vms"))

        html = response.content.decode()
        self.assertLess(html.index('data-guest-name="pve3-veeam-worker"'), html.index('data-guest-name="SophosTest"'))
        self.assertLess(html.index('data-guest-name="SophosTest"'), html.index('data-guest-name="ubuntu-test"'))

    def test_vms_overview_snapshot_info_uses_cache_without_live_provider_read(self):
        cache.clear()
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        ProxmoxInventory.objects.create(
            scan_run=scan,
            node="pve1",
            object_type=ProxmoxInventory.ObjectType.VM,
            vmid=500,
            name="Lab VM",
            status="running",
            config={},
        )

        cache.set(
            cluster_cache_key("guest-snapshot-present:v2", self.cluster, "pve1", "vm", 500),
            True,
            60,
        )
        clients = Mock(return_value=[])

        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=[self._live_guest(status="running")]),
            patch("core.views.common.fetch_live_guest_status", return_value={("vm", 500): "running"}),
            patch("core.views.common.cluster_scoped_clients", clients),
        ):
            response = self.client.get(reverse("core:vms_overview_snapshot_info"))

        self.assertEqual(response.status_code, 200)
        clients.assert_not_called()
        self.assertEqual(
            response.json()["guests"],
            [
                {
                    "target": "gr1:default:vm:500@pve1",
                    "guest_ref": "gr1:default:vm:500@pve1",
                    "has_snapshot": True,
                    "has_snapshot_label": "Yes",
                }
            ],
        )

    def test_vms_overview_snapshot_info_reports_unknown_when_probe_unavailable(self):
        cache.clear()  # the snapshot probe caches per guest; isolate from siblings
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        class FakeClient:
            def get(self, path, *, timeout=None):
                raise ProxmoxAPIError(path)

        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=[self._live_guest(status="running")]),
            patch("core.views.common.fetch_live_guest_status", return_value={("vm", 500): "running"}),
            patch("core.views.common.cluster_scoped_clients", return_value=[FakeClient()]),
        ):
            response = self.client.get(reverse("core:vms_overview_snapshot_info"))

        self.assertEqual(response.status_code, 200)
        # Probe could not answer -> unknown "-", never a misleading "No".
        self.assertEqual(response.json()["guests"][0]["has_snapshot_label"], "-")

    def test_guest_snapshots_orders_current_after_real_snapshots_and_shows_delete_all(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        class FakeClient:
            def guest_current(self, *, node, object_type, vmid):
                return {"status": "running"}

            def guest_config(self, *, node, object_type, vmid):
                return {"name": "Lab VM"}

            def get(self, path, *, timeout=None):
                if path == "nodes/pve1/qemu/500/snapshot":
                    return [
                        {"name": "manual_1", "snaptime": 100},
                        {"name": "current", "parent": "manual_1"},
                        {"name": "manual_2", "parent": "manual_1", "snaptime": 200},
                    ]
                raise ProxmoxAPIError(path)

        live_guest = self._live_guest(object_type="vm", vmid=500, name="Lab VM", node="pve1", status="running")
        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=[live_guest]),
            patch("core.views.common.cluster_scoped_clients", return_value=[FakeClient()]),
        ):
            response = self.client.get(reverse("core:guest_snapshots", args=[self.cluster.key, "vm", 500]))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertLess(content.index("manual_1"), content.index("manual_2"))
        self.assertLess(content.index("manual_2"), content.index("NOW"))
        self.assertContains(response, "Delete all")

    def test_vms_overview_agent_info_uses_cache_without_live_provider_read(self):
        cache.clear()
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        ProxmoxInventory.objects.create(
            scan_run=scan,
            node="pve1",
            object_type=ProxmoxInventory.ObjectType.VM,
            vmid=500,
            name="Lab VM",
            status="running",
            config={"agent": "1", "ostype": "l26"},
        )
        CurrentGuestInventory.objects.create(
            cluster=self.cluster,
            source_scan=scan,
            node="pve1",
            object_type=CurrentGuestInventory.ObjectType.VM,
            vmid=500,
            name="Lab VM",
            status="running",
            config={"agent": "1", "ostype": "l26"},
            observed_at=timezone.now(),
        )

        cache.set(
            cluster_cache_key("guest-agent-summary:v2", self.cluster, "pve1", "vm", 500),
            {
                "enabled": True,
                "running": True,
                "os_pretty_name": "Ubuntu 24.04.4 LTS",
                "ips": ["192.0.2.50"],
            },
            60,
        )
        clients = Mock(return_value=[])

        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=[self._live_guest(status="running")]),
            patch("core.views.common.fetch_live_guest_status", return_value={("vm", 500): "running"}),
            patch("core.views.common.cluster_scoped_clients", clients),
        ):
            response = self.client.get(reverse("core:vms_overview_agent_info"))

        self.assertEqual(response.status_code, 200)
        clients.assert_not_called()
        payload = response.json()["guests"][0]
        self.assertEqual(payload["target"], "gr1:default:vm:500@pve1")
        self.assertEqual(payload["guest_os"], "Ubuntu 24.04.4 LTS")
        self.assertEqual(payload["ip_label"], "192.0.2.50")
        self.assertEqual(payload["agent"], "Running")

    def test_vms_status_uses_cluster_qualified_current_inventory(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)
        self._live_guest(status="running")

        response = self.client.get(reverse("core:vms_status"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["guests"][0]["guest_ref"], "gr1:default:vm:500@pve1")
        self.assertEqual(response.json()["guests"][0]["target"], "gr1:default:vm:500@pve1")

    def test_ct_summary_uses_container_labels_and_cpu_topology_card(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        class FakeClient:
            def guest_current(self, *, node, object_type, vmid):
                return {"status": "stopped"}

            def guest_config(self, *, node, object_type, vmid):
                return {
                    "hostname": "ct-lab",
                    "cores": "2",
                    "memory": "1024",
                    "rootfs": "nfs-ct:subvol-601-disk-0,size=8G",
                }

        live_guest = self._live_guest(object_type="ct", vmid=601, name="ct-lab", node="pve1", status="stopped")
        CurrentGuestInventory.objects.filter(object_type="ct", vmid=601).update(
            config={
                "hostname": "ct-lab",
                "cores": "2",
                "memory": "1024",
                "rootfs": "nfs-ct:subvol-601-disk-0,size=8G",
            },
            config_complete=True,
            config_observed_at=timezone.now(),
        )
        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=[live_guest]),
            patch("core.views.common.fetch_live_guest_status", return_value={("ct", 601): "stopped"}),
            patch("core.views.common.cluster_scoped_clients", return_value=[FakeClient()]),
        ):
            response = self.client.get(reverse("core:guest_summary", args=[self.cluster.key, "ct", 601]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Container Details")
        self.assertContains(response, "Container Hardware")
        self.assertContains(response, "CPU Topology")
        self.assertContains(response, "<dt>vCPUs</dt><dd>2</dd>", html=True)
        self.assertNotContains(response, "Virtual Machine Details")

    def test_guest_summary_shows_not_managed_ha_state_for_quorate_cluster(self):
        cache.clear()
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        class FakeClient:
            def guest_current(self, *, node, object_type, vmid):
                return {"status": "running"}

            def guest_config(self, *, node, object_type, vmid):
                return {"name": "Lab VM", "memory": "2048", "cores": "2"}

            def get(self, path, *, timeout=None):
                if path == "cluster/status":
                    return [{"type": "cluster", "name": "Lab Cluster", "nodes": 2, "quorate": 1}]
                if path == "cluster/ha/resources":
                    return []
                raise ProxmoxAPIError(path)

        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=[self._live_guest(status="running")]),
            patch("core.views.common.cluster_scoped_clients", return_value=[FakeClient()]),
        ):
            response = self.client.get(reverse("core:guest_summary", args=[self.cluster.key, "vm", 500]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "High Availability")
        self.assertContains(response, "Not managed")
        self.assertContains(response, "Lab Cluster")
        self.assertContains(response, "This guest is not configured as a Proxmox HA resource.")

    def test_guest_summary_shows_managed_ha_resource_details(self):
        cache.clear()
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        class FakeClient:
            def guest_current(self, *, node, object_type, vmid):
                return {"status": "running"}

            def guest_config(self, *, node, object_type, vmid):
                return {"name": "Lab VM", "memory": "2048", "cores": "2"}

            def get(self, path, *, timeout=None):
                if path == "cluster/status":
                    return [{"type": "cluster", "name": "Lab Cluster", "nodes": 2, "quorate": 1}]
                if path == "cluster/ha/resources":
                    return [
                        {
                            "sid": "vm:500",
                            "state": "started",
                            "max_restart": 3,
                            "max_relocate": 2,
                            "group": "preferred-nodes",
                        }
                    ]
                raise ProxmoxAPIError(path)

        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=[self._live_guest(status="running")]),
            patch("core.views.common.cluster_scoped_clients", return_value=[FakeClient()]),
        ):
            response = self.client.get(reverse("core:guest_summary", args=[self.cluster.key, "vm", 500]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Managed")
        self.assertContains(response, "Desired state")
        self.assertContains(response, "Started")
        self.assertContains(response, "preferred-nodes")

    def test_guest_hardware_edit_renders_for_vm_disks_and_networks(self):
        user = get_user_model().objects.create_user(username="operator", password="unused")
        self.client.force_login(user)

        class FakeClient:
            def node_names(self, *, fallback=""):
                return ["pve1"]

            def guest_current(self, *, node, object_type, vmid):
                return {"status": "stopped"}

            def guest_config(self, *, node, object_type, vmid):
                return {
                    "name": "Lab VM",
                    "cores": "2",
                    "sockets": "1",
                    "memory": "2048",
                    "scsi0": "nfs-vm:vm-500-disk-0.qcow2,size=32G",
                    "ide2": "none,media=cdrom",
                    "net0": "virtio=BC:24:11:22:33:44,bridge=vmbr0",
                }

            def get(self, path, *, timeout=None):
                if path == "cluster/nextid":
                    return 501
                if path == "nodes/pve1/storage":
                    return [
                        {"storage": "nfs-vm", "content": "images,iso"},
                        {"storage": "local", "content": "iso"},
                    ]
                if path == "nodes/pve1/network":
                    return [{"type": "bridge", "iface": "vmbr0"}]
                if path == "cluster/sdn/vnets":
                    return []
                if path == "nodes/pve1/storage/local/content?content=iso":
                    return [{"volid": "local:iso/ubuntu.iso"}]
                if path == "nodes/pve1/storage/nfs-vm/content?content=iso":
                    return []
                raise ProxmoxAPIError(path)

        fake_client = self._patch_provider_client(FakeClient())
        self._seed_volume_catalog("nfs-vm", [])
        live_guest = self._live_guest(object_type="vm", vmid=500, name="Lab VM", node="pve1", status="stopped")
        CurrentGuestInventory.objects.filter(object_type="vm", vmid=500).update(
            config=fake_client.guest_config(node="pve1", object_type="vm", vmid=500),
            config_complete=True,
            config_observed_at=timezone.now(),
        )
        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=[live_guest]),
            patch("core.views.common.cluster_scoped_clients", return_value=[fake_client]),
        ):
            response = self.client.get(reverse("core:guest_hardware_edit", args=[self.cluster.key, "vm", 500]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Virtual Hardware")
        self.assertContains(response, "VM Options")
        self.assertContains(response, "Boot Options")
        self.assertContains(response, "data-boot-order-editor")
        self.assertContains(response, "data-hotplug-editor")
        self.assertContains(response, "scsi0")
        self.assertContains(response, "vmbr0")

    def test_guest_create_vm_lists_windows_server_2025_as_win11_family(self):
        user = get_user_model().objects.create_user(username="operator", password="unused")
        self.client.force_login(user)

        class FakeClient:
            def node_names(self, *, fallback=""):
                return ["pve1"]

            def get(self, path, *, timeout=None):
                if path == "cluster/nextid":
                    return 500
                if path == "nodes/pve1/storage":
                    return [{"storage": "nfs-vm", "content": "images,iso"}]
                if path == "nodes/pve1/network":
                    return [{"type": "bridge", "iface": "vmbr0"}]
                if path == "cluster/sdn/vnets":
                    return []
                if path == "nodes/pve1/storage/nfs-vm/content?content=iso":
                    return []
                raise ProxmoxAPIError(path)

        fake_client = self._patch_provider_client(FakeClient())
        with (
            patch("core.views.common.cluster_scoped_clients", return_value=[fake_client]),
        ):
            response = self.client.get(reverse("core:guest_create", args=[self.cluster.key, "vm"]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<option value="win11">Windows 11/2022/2025</option>', html=True)

    def test_guest_hardware_edit_updates_vm_options(self):
        user = get_user_model().objects.create_user(username="operator", password="unused")
        self.client.force_login(user)

        class FakeClient:
            def __init__(self):
                self.updates = None
                self.delete = None
                self.digest = None

            def guest_current(self, *, node, object_type, vmid):
                return {"status": "stopped"}

            def guest_config(self, *, node, object_type, vmid):
                return {
                    "digest": "abc123",
                    "name": "Lab VM",
                    "cores": "2",
                    "sockets": "1",
                    "memory": "2048",
                    "agent": "1,fstrim_cloned_disks=1",
                    "boot": "order=scsi0",
                    "ostype": "l26",
                    "bios": "seabios",
                }

            def set_guest_config(self, *, node, object_type, vmid, updates, delete=None, digest=None):
                self.updates = updates
                self.delete = delete or []
                self.digest = digest
                return None

        fake_client = self._patch_provider_client(FakeClient())
        self._seed_volume_catalog("nfs-vm", [])
        live_guest = self._live_guest(object_type="vm", vmid=500, name="Lab VM", node="pve1", status="stopped")
        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=[live_guest]),
            patch("core.views.common.cluster_scoped_clients", return_value=[fake_client]),
        ):
            response = self.client.post(
                reverse("core:guest_hardware_edit", args=[self.cluster.key, "vm", 500]),
                {
                    "vm_name": "Renamed VM",
                    "vm_description": "Lab notes",
                    "vm_onboot": "on",
                    "vm_protection": "on",
                    "vm_agent": "on",
                    "vm_tablet": "on",
                    "vm_acpi": "on",
                    "vm_boot": "order=scsi0;ide2;net0",
                    "vm_ostype": "win11",
                    "vm_bios": "ovmf",
                    "vm_machine": "q35",
                    "vm_scsihw": "virtio-scsi-single",
                    "vm_cpu": "host",
                    "vm_hotplug": "disk,network,memory",
                    "vm_numa": "on",
                    "vm_balloon_enabled": "on",
                    "vm_allow_ksm": "on",
                    "startup_order": "2",
                    "startup_up": "30",
                    "startup_down": "60",
                    "cores": "2",
                    "sockets": "1",
                    "memory": "2048",
                },
            )

        self.assertRedirects(
            response, reverse("core:guest_summary", args=[self.cluster.key, "vm", 500]), fetch_redirect_response=False
        )
        self.assertEqual(fake_client.digest, "abc123")
        self.assertEqual(fake_client.delete, [])
        self.assertEqual(
            fake_client.updates,
            {
                "name": "Renamed VM",
                "description": "Lab notes",
                "onboot": "1",
                "protection": "1",
                "boot": "order=scsi0;ide2;net0",
                "ostype": "win11",
                "bios": "ovmf",
                "machine": "q35",
                "scsihw": "virtio-scsi-single",
                "cpu": "host",
                "hotplug": "disk,network,memory",
                "numa": "1",
                "startup": "order=2,up=30,down=60",
            },
        )

    def test_guest_hardware_edit_adds_multiple_devices_of_same_type(self):
        user = get_user_model().objects.create_user(username="operator", password="unused")
        self.client.force_login(user)

        class FakeClient:
            def __init__(self):
                self.updates = None

            def guest_current(self, *, node, object_type, vmid):
                return {"status": "stopped"}

            def guest_config(self, *, node, object_type, vmid):
                return {
                    "digest": "abc123",
                    "name": "Lab VM",
                    "cores": "2",
                    "sockets": "1",
                    "memory": "2048",
                }

            def set_guest_config(self, *, node, object_type, vmid, updates, delete=None, digest=None):
                self.updates = updates
                return None

        fake_client = self._patch_provider_client(FakeClient())
        live_guest = self._live_guest(object_type="vm", vmid=500, name="Lab VM", node="pve1", status="stopped")
        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=[live_guest]),
            patch("core.views.common.cluster_scoped_clients", return_value=[fake_client]),
        ):
            response = self.client.post(
                reverse("core:guest_hardware_edit", args=[self.cluster.key, "vm", 500]),
                {
                    "vm_name": "Lab VM",
                    "vm_tablet": "on",
                    "vm_acpi": "on",
                    "vm_balloon_enabled": "on",
                    "vm_allow_ksm": "on",
                    "cores": "2",
                    "sockets": "1",
                    "memory": "2048",
                    "newdisk_storage": ["nfs-vm", "nfs-vm"],
                    "newdisk_size": ["10", "20"],
                    "newnic_bridge": ["vmbr0", "vmbr1"],
                    "newnic_vlan": ["", "42"],
                },
            )

        self.assertRedirects(
            response, reverse("core:guest_summary", args=[self.cluster.key, "vm", 500]), fetch_redirect_response=False
        )
        self.assertEqual(
            fake_client.updates,
            {
                "scsi0": "nfs-vm:10",
                "scsi1": "nfs-vm:20",
                "net0": "virtio,bridge=vmbr0",
                "net1": "virtio,bridge=vmbr1,tag=42",
            },
        )

    def test_guest_hardware_edit_renders_for_ct_mounts_and_networks(self):
        user = get_user_model().objects.create_user(username="operator", password="unused")
        self.client.force_login(user)

        class FakeClient:
            def node_names(self, *, fallback=""):
                return ["pve1"]

            def guest_current(self, *, node, object_type, vmid):
                return {"status": "running"}

            def guest_config(self, *, node, object_type, vmid):
                return {
                    "hostname": "ct-lab",
                    "cores": "2",
                    "memory": "1024",
                    "swap": "512",
                    "rootfs": "nfs-ct:subvol-601-disk-0,size=8G,acl=1",
                    "mp0": "nfs-ct:4,mp=/srv/data,backup=1,size=4G",
                    "net0": "name=eth0,bridge=vmbr0,ip=dhcp,firewall=1,type=veth",
                    "features": "nesting=1,mount=nfs;cifs",
                    "ostype": "ubuntu",
                    "arch": "amd64",
                }

            def get(self, path, *, timeout=None):
                if path == "cluster/nextid":
                    return 602
                if path == "nodes/pve1/storage":
                    return [{"storage": "nfs-ct", "content": "rootdir,vztmpl"}]
                if path == "nodes/pve1/network":
                    return [{"type": "bridge", "iface": "vmbr0"}]
                if path == "cluster/sdn/vnets":
                    return []
                if path == "nodes/pve1/storage/nfs-ct/content?content=vztmpl":
                    return []
                raise ProxmoxAPIError(path)

        fake_client = self._patch_provider_client(FakeClient())
        live_guest = self._live_guest(object_type="ct", vmid=601, name="ct-lab", node="pve1", status="running")
        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=[live_guest]),
            patch("core.views.common.cluster_scoped_clients", return_value=[fake_client]),
        ):
            response = self.client.get(reverse("core:guest_hardware_edit", args=[self.cluster.key, "ct", 601]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Container Hardware")
        self.assertContains(response, "CT Options")
        self.assertContains(response, "Root Disk")
        self.assertContains(response, "Mount Point")
        self.assertContains(response, "Network Adapter")
        self.assertContains(response, "ct-lab")
        self.assertContains(response, "nfs;cifs")
        self.assertContains(response, 'data-add-device="mount"')

    def test_guest_hardware_edit_updates_ct_config_and_resizes(self):
        user = get_user_model().objects.create_user(username="operator", password="unused")
        self.client.force_login(user)

        class FakeClient:
            def __init__(self):
                self.updates = None
                self.delete = None
                self.digest = None
                self.resize_calls = []

            def guest_current(self, *, node, object_type, vmid):
                return {"status": "stopped"}

            def guest_config(self, *, node, object_type, vmid):
                return {
                    "digest": "ctdigest",
                    "hostname": "ct-lab",
                    "cores": "2",
                    "memory": "1024",
                    "swap": "512",
                    "rootfs": "nfs-ct:subvol-601-disk-0,size=8G,acl=1",
                    "mp0": "nfs-ct:4,mp=/srv/data,backup=1,size=4G",
                    "net0": "name=eth0,bridge=vmbr0,ip=dhcp,type=veth",
                    "features": "nesting=1",
                }

            def set_guest_config(self, *, node, object_type, vmid, updates, delete=None, digest=None):
                self.updates = updates
                self.delete = delete or []
                self.digest = digest
                return None

            def put(self, path, *, data=None):
                self.resize_calls.append((path, data or {}))
                return None

        fake_client = self._patch_provider_client(FakeClient())
        live_guest = self._live_guest(object_type="ct", vmid=601, name="ct-lab", node="pve1", status="stopped")
        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=[live_guest]),
            patch("core.views.common.cluster_scoped_clients", return_value=[fake_client]),
        ):
            response = self.client.post(
                reverse("core:guest_hardware_edit", args=[self.cluster.key, "ct", 601]),
                {
                    "ct_hostname": "ct-renamed",
                    "ct_onboot": "on",
                    "ct_protection": "on",
                    "ct_nameserver": "192.0.2.1",
                    "cores": "4",
                    "memory": "2048",
                    "swap": "0",
                    "rootfs_size": "12",
                    "rootfs_acl": "on",
                    "feature_nesting": "on",
                    "feature_fuse": "on",
                    "feature_mount": "nfs;cifs",
                    "startup_order": "1",
                    "startup_up": "10",
                    "startup_down": "20",
                    "mp0_source": "nfs-ct:4",
                    "mp0_path": "/srv/new",
                    "mp0_backup": "on",
                    "mp0_size": "6",
                    "net0_name": "eth0",
                    "net0_bridge": "vmbr1",
                    "net0_ip": "192.0.2.60/24",
                    "net0_ip6": "",
                    "net0_gw": "192.0.2.1",
                    "net0_gw6": "",
                    "net0_hwaddr": "",
                    "net0_mtu": "",
                    "net0_rate": "",
                    "net0_tag": "42",
                    "net0_trunks": "",
                    "net0_type": "veth",
                    "net0_firewall": "on",
                    "newmp_storage": ["nfs-ct"],
                    "newmp_size": ["2"],
                    "newmp_path": ["/opt/app"],
                    "newnet_name": ["eth1"],
                    "newnet_bridge": ["vmbr0"],
                    "newnet_ip": ["dhcp"],
                    "newnet_ip6": [""],
                    "newnet_vlan": [""],
                    "newnet_firewall": ["on"],
                },
            )

        self.assertRedirects(
            response, reverse("core:guest_summary", args=[self.cluster.key, "ct", 601]), fetch_redirect_response=False
        )
        self.assertEqual(fake_client.digest, "ctdigest")
        self.assertEqual(fake_client.delete, [])
        self.assertEqual(fake_client.updates["hostname"], "ct-renamed")
        self.assertEqual(fake_client.updates["cores"], "4")
        self.assertEqual(fake_client.updates["memory"], "2048")
        self.assertEqual(fake_client.updates["swap"], "0")
        self.assertEqual(fake_client.updates["features"], "nesting=1,fuse=1,mount=nfs;cifs")
        self.assertEqual(fake_client.updates["startup"], "order=1,up=10,down=20")
        self.assertEqual(fake_client.updates["mp0"], "nfs-ct:4,mp=/srv/new,backup=1,size=4G")
        self.assertEqual(
            fake_client.updates["net0"],
            "name=eth0,bridge=vmbr1,firewall=1,gw=192.0.2.1,ip=192.0.2.60/24,tag=42,type=veth",
        )
        self.assertEqual(fake_client.updates["mp1"], "nfs-ct:2,mp=/opt/app")
        self.assertEqual(fake_client.updates["net1"], "name=eth1,bridge=vmbr0,firewall=1,ip=dhcp,type=veth")
        self.assertEqual(
            fake_client.resize_calls,
            [
                ("nodes/pve1/lxc/601/resize", {"disk": "rootfs", "size": "12G"}),
                ("nodes/pve1/lxc/601/resize", {"disk": "mp0", "size": "6G"}),
            ],
        )

    def test_guest_create_vm_requires_name_before_posting_to_proxmox(self):
        user = get_user_model().objects.create_user(username="operator", password="unused")
        self.client.force_login(user)

        class FakeClient:
            def __init__(self):
                self.posts = []

            def node_names(self, *, fallback=""):
                return ["pve1"]

            def get(self, path, *, timeout=None):
                if path == "cluster/nextid":
                    return 500
                if path == "nodes/pve1/storage":
                    return [{"storage": "nfs-vm", "content": "images,iso"}]
                if path == "nodes/pve1/network":
                    return [{"type": "bridge", "iface": "vmbr0"}]
                if path == "cluster/sdn/vnets":
                    return []
                if path == "nodes/pve1/storage/nfs-vm/content?content=iso":
                    return []
                raise ProxmoxAPIError(path)

            def post(self, path, data=None):
                self.posts.append((path, data or {}))
                return "UPID:pve1:create:500:root@pam:"

        fake_client = self._patch_provider_client(FakeClient())
        response = self.client.post(
            reverse("core:guest_create", args=[self.cluster.key, "vm"]),
            {
                "node": "pve1",
                "vmid": "500",
                "name": "",
                "ostype": "l26",
                "cores": "1",
                "sockets": "1",
                "memory": "2048",
                "disk_storage": "nfs-vm",
                "disk_size": "32",
                "bridge": "vmbr0",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Name is required.")
        self.assertEqual(fake_client.posts, [])

    def test_vms_bulk_power_action_posts_to_selected_guest_and_audit_uses_guest_label(self):
        user = get_user_model().objects.create_user(username="operator", password="unused")
        self.client.force_login(user)

        class FakeClient:
            def __init__(self):
                self.posts = []

            def guest_current(self, *, node, object_type, vmid):
                return {"status": "stopped"}

            def guest_config(self, *, node, object_type, vmid):
                return {"name": "Lab VM"}

            def post(self, path, data=None):
                self.posts.append((path, data or {}))
                return "UPID:pve1:00000001:00000002:00000003:qmstart:500:root@pam:"

        fake_client = self._patch_provider_client(FakeClient())
        live_guest = self._live_guest(object_type="vm", vmid=500, name="Lab VM", node="pve1", status="stopped")
        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=[live_guest]),
            patch("core.views.common.cluster_scoped_clients", return_value=[fake_client]),
        ):
            response = self.client.post(
                reverse("core:vms_bulk_action"),
                {"bulk_action": "start", "guest": [GuestRef(self.cluster.key, "vm", 500).serialize()]},
            )

        self.assertRedirects(response, reverse("core:vms_overview"), fetch_redirect_response=False)
        self.assertEqual(list(get_messages(response.wsgi_request)), [])
        self.assertEqual(fake_client.posts, [("nodes/pve1/qemu/500/status/start", {})])

        event = AuditEvent.objects.get(action="guest.power.start")
        self.assertEqual(event.username, "operator")
        self.assertEqual(event.details["name"], "Lab VM")

        audit_response = self.client.get(reverse("core:audit_log"))
        self.assertContains(audit_response, "Power on guest")
        self.assertContains(audit_response, "guest-label")
        self.assertContains(audit_response, '<span class="guest-vmid">500</span>', html=True)
        self.assertContains(audit_response, '<span class="guest-name">Lab VM</span>', html=True)
        self.assertNotContains(audit_response, "guest.power.start")

    def test_vms_bulk_clone_posts_clone_request(self):
        user = get_user_model().objects.create_user(username="operator", password="unused")
        self.client.force_login(user)

        class FakeClient:
            def __init__(self):
                self.posts = []

            def guest_current(self, *, node, object_type, vmid):
                return {"status": "stopped"}

            def guest_config(self, *, node, object_type, vmid):
                return {"name": "Lab VM"}

            def post(self, path, data=None):
                self.posts.append((path, data or {}))
                return "UPID:pve1:clone:500:root@pam:"

        fake_client = self._patch_provider_client(FakeClient())
        live_guest = self._live_guest(object_type="vm", vmid=500, name="Lab VM", node="pve1", status="stopped")
        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=[live_guest]),
            patch("core.views.common.cluster_scoped_clients", return_value=[fake_client]),
        ):
            response = self.client.post(
                reverse("core:vms_bulk_action"),
                {
                    "bulk_action": "clone",
                    "guest": [GuestRef(self.cluster.key, "vm", 500).serialize()],
                    "clone_newid": "600",
                    "clone_name": "Lab Clone",
                    "clone_full": "1",
                    "clone_storage": "nfs-vm",
                },
            )

        self.assertRedirects(response, reverse("core:vms_overview"), fetch_redirect_response=False)
        self.assertEqual(
            fake_client.posts,
            [
                (
                    "nodes/pve1/qemu/500/clone",
                    {"newid": "600", "full": 1, "name": "Lab Clone", "storage": "nfs-vm"},
                )
            ],
        )
        event = AuditEvent.objects.get(action="guest.clone.create")
        self.assertEqual(event.details["new_vmid"], 600)
        self.assertEqual(event.details["new_name"], "Lab Clone")

    def test_vms_bulk_untemplate_clears_template_flag_for_standalone_template(self):
        user = get_user_model().objects.create_user(username="operator", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as storage_path:
            StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="NFS VM",
                path=storage_path,
                enabled=True,
            )
            self._seed_volume_catalog(
                "nfs-vm",
                [{"vmid": 500, "volid": "nfs-vm:500/base-500-disk-0.qcow2"}],
            )

            class FakeClient:
                def __init__(self):
                    self.updates = []

                def guest_current(self, *, node, object_type, vmid):
                    return {"status": "stopped"}

                def guest_config(self, *, node, object_type, vmid):
                    return {
                        "name": "Lab Template",
                        "template": 1,
                        "digest": "template-digest",
                        "scsi0": "nfs-vm:500/base-500-disk-0.qcow2,size=32G",
                    }

                def get(self, path, *, timeout=None):
                    if path == "nodes/pve1/qemu/500/snapshot":
                        return [{"name": "current"}]
                    if path == "nodes/pve1/storage/nfs-vm/content":
                        return [{"vmid": 500, "volid": "nfs-vm:500/base-500-disk-0.qcow2"}]
                    raise ProxmoxAPIError(path)

                def set_guest_config(self, *, node, object_type, vmid, updates, delete=None, digest=None):
                    self.updates.append((node, object_type, vmid, updates, delete, digest))

            fake_client = self._patch_provider_client(FakeClient())
            live_guest = self._live_guest(
                object_type="vm", vmid=500, name="Lab Template", node="pve1", status="stopped"
            )
            with (
                patch("core.views.common.fetch_live_guest_inventory", return_value=[live_guest]),
                patch("core.views.common.cluster_scoped_clients", return_value=[fake_client]),
            ):
                response = self.client.post(
                    reverse("core:vms_bulk_action"),
                    {
                        "bulk_action": "untemplate",
                        "guest": [GuestRef(self.cluster.key, "vm", 500).serialize()],
                        "untemplate_confirm_vmid": "500",
                        "untemplate_acknowledge": "convert",
                    },
                )

        self.assertRedirects(response, reverse("core:vms_overview"), fetch_redirect_response=False)
        self.assertEqual(
            fake_client.updates,
            [("pve1", "vm", 500, {"template": "0"}, [], "template-digest")],
        )
        event = AuditEvent.objects.get(action="guest.template.revert")
        self.assertEqual(event.outcome, "success")
        self.assertEqual(event.details["storage_ids"], ["nfs-vm"])
        self.assertIn("Convert template to VM", [task["name"] for task in recent_task_page(limit=10).tasks])

    def test_vms_bulk_untemplate_blocks_linked_clone(self):
        user = get_user_model().objects.create_user(username="operator", password="unused")
        self.client.force_login(user)

        with TemporaryDirectory() as storage_path:
            StorageMount.objects.create(
                storage_id="nfs-vm",
                display_name="NFS VM",
                path=storage_path,
                enabled=True,
            )
            self._seed_volume_catalog(
                "nfs-vm",
                [
                    {"vmid": 500, "volid": "nfs-vm:500/base-500-disk-0.qcow2"},
                    {
                        "vmid": 600,
                        "volid": "nfs-vm:600/vm-600-disk-0.qcow2",
                        "parent": "../500/base-500-disk-0.qcow2",
                    },
                ],
            )

            class FakeClient:
                def __init__(self):
                    self.updated = False

                def guest_current(self, *, node, object_type, vmid):
                    return {"status": "stopped"}

                def guest_config(self, *, node, object_type, vmid):
                    return {
                        "name": "Lab Template",
                        "template": 1,
                        "digest": "template-digest",
                        "scsi0": "nfs-vm:500/base-500-disk-0.qcow2,size=32G",
                    }

                def get(self, path, *, timeout=None):
                    if path == "nodes/pve1/qemu/500/snapshot":
                        return [{"name": "current"}]
                    if path == "nodes/pve1/storage/nfs-vm/content":
                        return [
                            {"vmid": 500, "volid": "nfs-vm:500/base-500-disk-0.qcow2"},
                            {
                                "vmid": 600,
                                "volid": "nfs-vm:600/vm-600-disk-0.qcow2",
                                "parent": "../500/base-500-disk-0.qcow2",
                            },
                        ]
                    raise ProxmoxAPIError(path)

                def set_guest_config(self, **_kwargs):
                    self.updated = True

            fake_client = self._patch_provider_client(FakeClient())
            live_guest = self._live_guest(
                object_type="vm", vmid=500, name="Lab Template", node="pve1", status="stopped"
            )
            with (
                patch("core.views.common.fetch_live_guest_inventory", return_value=[live_guest]),
                patch("core.views.common.cluster_scoped_clients", return_value=[fake_client]),
            ):
                response = self.client.post(
                    reverse("core:vms_bulk_action"),
                    {
                        "bulk_action": "untemplate",
                        "guest": [GuestRef(self.cluster.key, "vm", 500).serialize()],
                        "untemplate_confirm_vmid": "500",
                        "untemplate_acknowledge": "convert",
                    },
                    HTTP_ACCEPT="application/json",
                    HTTP_X_REQUESTED_WITH="fetch",
                )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ok"])
        self.assertIn("linked clone", response.json()["errors"][0])
        self.assertFalse(fake_client.updated)
        event = AuditEvent.objects.get(action="guest.template.revert")
        self.assertEqual(event.outcome, "failed")
        self.assertEqual(event.details["linked_children"][0]["vmid"], 600)

    def test_vms_bulk_pool_move_reassigns_membership_and_records_task(self):
        user = get_user_model().objects.create_user(username="operator", password="unused")
        self.client.force_login(user)

        class FakeClient:
            def __init__(self):
                self.puts = []

            def guest_current(self, *, node, object_type, vmid):
                return {"status": "running"}

            def guest_config(self, *, node, object_type, vmid):
                return {"name": "Lab VM"}

            def get(self, path, *, timeout=None):
                if path == "pools":
                    return [{"poolid": "old-pool"}, {"poolid": "new-pool"}]
                if path == "pools/old-pool":
                    return {"poolid": "old-pool", "members": [{"type": "qemu", "vmid": 500}]}
                if path == "pools/new-pool":
                    return {"poolid": "new-pool", "members": []}
                raise ProxmoxAPIError(path)

            def put(self, path, *, data=None):
                self.puts.append((path, data or {}))

        fake_client = self._patch_provider_client(FakeClient())
        live_guest = self._live_guest(object_type="vm", vmid=500, name="Lab VM", node="pve1", status="running")
        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=[live_guest]),
            patch("core.views.common.cluster_scoped_clients", return_value=[fake_client]),
        ):
            response = self.client.post(
                reverse("core:vms_bulk_action"),
                {
                    "bulk_action": "pool",
                    "guest": [GuestRef(self.cluster.key, "vm", 500).serialize()],
                    "pool_id": "new-pool",
                },
            )

        self.assertRedirects(response, reverse("core:vms_overview"), fetch_redirect_response=False)
        self.assertEqual(
            fake_client.puts,
            [
                ("pools/old-pool", {"vms": "500", "delete": 1}),
                ("pools/new-pool", {"vms": "500"}),
            ],
        )
        event = AuditEvent.objects.get(action="guest.pool.updated")
        self.assertEqual(event.outcome, "success")
        self.assertEqual(event.details["previous_pool"], "old-pool")
        self.assertEqual(event.details["target_pool"], "new-pool")
        self.assertIn("Move to pool", [task["name"] for task in recent_task_page(limit=10).tasks])

    def test_guest_pool_options_returns_current_membership(self):
        user = get_user_model().objects.create_user(username="operator", password="unused")
        self.client.force_login(user)

        class FakeClient:
            def guest_current(self, *, node, object_type, vmid):
                return {"status": "stopped"}

            def guest_config(self, *, node, object_type, vmid):
                return {"name": "Lab CT"}

            def get(self, path, *, timeout=None):
                if path == "pools":
                    return [{"poolid": "operations"}]
                if path == "pools/operations":
                    return {"poolid": "operations", "members": [{"type": "lxc", "vmid": 601}]}
                raise ProxmoxAPIError(path)

        fake_client = self._patch_provider_client(FakeClient())
        live_guest = self._live_guest(object_type="ct", vmid=601, name="Lab CT", node="pve1", status="stopped")
        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=[live_guest]),
            patch("core.views.common.cluster_scoped_clients", return_value=[fake_client]),
        ):
            response = self.client.get(reverse("core:guest_pool_options", args=[self.cluster.key, "ct", 601]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "pools": [{"id": "operations", "label": "operations"}],
                "current_pool": "operations",
                "multiple_memberships": [],
            },
        )

    def test_vms_bulk_delete_all_snapshots_deletes_leaf_first(self):
        user = get_user_model().objects.create_user(username="operator", password="unused")
        self.client.force_login(user)

        class FakeClient:
            def __init__(self):
                self.deletes = []

            def guest_current(self, *, node, object_type, vmid):
                return {"status": "running"}

            def guest_config(self, *, node, object_type, vmid):
                return {"name": "Lab VM"}

            def get(self, path, *, timeout=None):
                if path == "nodes/pve1/qemu/500/snapshot":
                    return [
                        {"name": "snap-a", "snaptime": 100},
                        {"name": "snap-b", "parent": "snap-a", "snaptime": 200},
                        {"name": "current", "parent": "snap-b"},
                    ]
                raise ProxmoxAPIError(path)

            def delete(self, path):
                self.deletes.append(path)
                return "UPID:pve1:snapshot-delete:500:root@pam:"

        fake_client = self._patch_provider_client(FakeClient())
        live_guest = self._live_guest(object_type="vm", vmid=500, name="Lab VM", node="pve1", status="running")
        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=[live_guest]),
            patch("core.views.common.cluster_scoped_clients", return_value=[fake_client]),
        ):
            response = self.client.post(
                reverse("core:vms_bulk_action"),
                {"bulk_action": "delete_snapshots", "guest": [GuestRef(self.cluster.key, "vm", 500).serialize()]},
            )

        self.assertRedirects(response, reverse("core:vms_overview"), fetch_redirect_response=False)
        self.assertEqual(
            fake_client.deletes,
            [
                "nodes/pve1/qemu/500/snapshot/snap-b",
                "nodes/pve1/qemu/500/snapshot/snap-a",
            ],
        )
        event = AuditEvent.objects.get(action="guest.snapshot.delete_all")
        self.assertEqual(event.details["deleted"], 2)

    def test_guest_snapshot_delete_waits_for_proxmox_task(self):
        user = get_user_model().objects.create_user(username="operator", password="unused")
        self.client.force_login(user)

        class FakeClient:
            def __init__(self):
                self.deletes = []
                self.waits = []

            def guest_current(self, *, node, object_type, vmid):
                return {"status": "running"}

            def guest_config(self, *, node, object_type, vmid):
                return {"name": "Lab VM"}

            def delete(self, path):
                self.deletes.append(path)
                return "UPID:pve1:snapshot-delete:500:root@pam:"

            def wait_for_task(self, *, node, upid, timeout_seconds=None):
                self.waits.append((node, upid, timeout_seconds))
                return ProxmoxTaskResult(
                    node=node,
                    upid=upid,
                    status="stopped",
                    exitstatus="OK",
                    raw={"status": "stopped", "exitstatus": "OK"},
                )

        fake_client = self._patch_provider_client(FakeClient())
        live_guest = self._live_guest(object_type="vm", vmid=500, name="Lab VM", node="pve1", status="running")
        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=[live_guest]),
            patch("core.views.common.cluster_scoped_clients", return_value=[fake_client]),
            patch("core.views.common.async_task", return_value="poll-task-1") as poll_mock,
        ):
            response = self.client.post(
                reverse("core:guest_snapshot_delete", args=[self.cluster.key, "vm", 500, "snap-a"])
            )

        self.assertRedirects(
            response, reverse("core:guest_snapshots", args=[self.cluster.key, "vm", 500]), fetch_redirect_response=False
        )
        self.assertEqual(fake_client.deletes, ["nodes/pve1/qemu/500/snapshot/snap-a"])
        # The delete returns a UPID; the task is polled in the background (async
        # audit), not by blocking the request on wait_for_task.
        self.assertEqual(fake_client.waits, [])
        poll_mock.assert_called_once()
        self.assertEqual(poll_mock.call_args[0][0], "core.tasks.poll_guest_audit_task")
        event = AuditEvent.objects.get(action="guest.snapshot.delete")
        self.assertEqual(event.outcome, "running")
        self.assertEqual(event.details["proxmox_task_upid"], "UPID:pve1:snapshot-delete:500:root@pam:")

    def test_guest_clone_options_returns_nextid_and_storage_default(self):
        user = get_user_model().objects.create_user(username="operator", password="unused")
        self.client.force_login(user)

        class FakeClient:
            def guest_current(self, *, node, object_type, vmid):
                return {"status": "stopped"}

            def guest_config(self, *, node, object_type, vmid):
                return {"name": "Lab VM", "scsi0": "nfs-vm:vm-500-disk-0.qcow2,size=32G"}

            def get(self, path, *, timeout=None):
                if path == "cluster/nextid":
                    return 600
                if path == "nodes/pve1/storage":
                    return [
                        {"storage": "local", "content": "iso"},
                        {"storage": "nfs-vm", "content": "images,iso"},
                    ]
                raise ProxmoxAPIError(path)

        fake_client = self._patch_provider_client(FakeClient())
        self._seed_volume_catalog("nfs-vm", [])
        live_guest = self._live_guest(object_type="vm", vmid=500, name="Lab VM", node="pve1", status="stopped")
        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=[live_guest]),
            patch("core.views.common.cluster_scoped_clients", return_value=[fake_client]),
        ):
            response = self.client.get(reverse("core:guest_clone_options", args=[self.cluster.key, "vm", 500]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["nextid"], "600")
        self.assertEqual(payload["default_storage"], "nfs-vm")
        self.assertEqual(payload["storages"], [{"id": "nfs-vm", "label": "nfs-vm"}])
        self.assertEqual(payload["suggested_name"], "Lab VM-clone")

    def test_vms_bulk_clone_requires_name(self):
        user = get_user_model().objects.create_user(username="operator", password="unused")
        self.client.force_login(user)

        class FakeClient:
            def __init__(self):
                self.posts = []

            def guest_current(self, *, node, object_type, vmid):
                return {"status": "stopped"}

            def guest_config(self, *, node, object_type, vmid):
                return {"name": "Lab VM"}

            def post(self, path, data=None):
                self.posts.append((path, data or {}))
                return "UPID:pve1:clone:500:root@pam:"

        fake_client = self._patch_provider_client(FakeClient())
        live_guest = self._live_guest(object_type="vm", vmid=500, name="Lab VM", node="pve1", status="stopped")
        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=[live_guest]),
            patch("core.views.common.cluster_scoped_clients", return_value=[fake_client]),
        ):
            response = self.client.post(
                reverse("core:vms_bulk_action"),
                {
                    "bulk_action": "clone",
                    "guest": [GuestRef(self.cluster.key, "vm", 500).serialize()],
                    "clone_newid": "600",
                    "clone_name": "",
                    "clone_full": "1",
                    "clone_storage": "nfs-vm",
                },
            )

        self.assertRedirects(response, reverse("core:vms_overview"), fetch_redirect_response=False)
        self.assertEqual(fake_client.posts, [])
        event = AuditEvent.objects.get(action="guest.clone.create")
        self.assertEqual(event.outcome, "failed")
        self.assertIn("Name is required", event.details["error"])

    def test_vms_bulk_destroy_requires_matching_vmid_and_calls_delete(self):
        user = get_user_model().objects.create_user(username="operator", password="unused")
        self.client.force_login(user)

        class FakeClient:
            def __init__(self):
                self.deletes = []

            def guest_current(self, *, node, object_type, vmid):
                return {"status": "stopped"}

            def guest_config(self, *, node, object_type, vmid):
                return {"name": "Lab VM"}

            def delete(self, path):
                self.deletes.append(path)
                return "UPID:pve1:destroy:500:root@pam:"

        fake_client = self._patch_provider_client(FakeClient())
        live_guest = self._live_guest(object_type="vm", vmid=500, name="Lab VM", node="pve1", status="stopped")
        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=[live_guest]),
            patch("core.views.common.cluster_scoped_clients", return_value=[fake_client]),
        ):
            failed_response = self.client.post(
                reverse("core:vms_bulk_action"),
                {
                    "bulk_action": "destroy",
                    "guest": [GuestRef(self.cluster.key, "vm", 500).serialize()],
                    "destroy_confirm_vmid": "501",
                    "destroy_purge": "1",
                    "destroy_unreferenced_disks": "1",
                },
            )
            success_response = self.client.post(
                reverse("core:vms_bulk_action"),
                {
                    "bulk_action": "destroy",
                    "guest": [GuestRef(self.cluster.key, "vm", 500).serialize()],
                    "destroy_confirm_vmid": "500",
                    "destroy_purge": "1",
                    "destroy_unreferenced_disks": "1",
                },
            )

        self.assertRedirects(failed_response, reverse("core:vms_overview"), fetch_redirect_response=False)
        self.assertRedirects(success_response, reverse("core:vms_overview"), fetch_redirect_response=False)
        self.assertEqual(
            fake_client.deletes,
            ["nodes/pve1/qemu/500?purge=1&destroy-unreferenced-disks=1"],
        )
        self.assertEqual(AuditEvent.objects.filter(action="guest.destroy", outcome="failed").count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="guest.destroy", outcome="running").count(), 1)

    def test_vms_bulk_tags_updates_multiple_guests(self):
        user = get_user_model().objects.create_user(username="operator", password="unused")
        self.client.force_login(user)

        class FakeClient:
            def __init__(self):
                self.configs = {
                    500: {"name": "Lab VM", "tags": "old"},
                    501: {"name": "Other VM"},
                }
                self.updates = []

            def guest_current(self, *, node, object_type, vmid):
                return {"status": "stopped"}

            def guest_config(self, *, node, object_type, vmid):
                return dict(self.configs[vmid])

            def set_guest_config(self, *, node, object_type, vmid, updates, delete=None, digest=None):
                self.updates.append((vmid, updates, delete or []))
                return None

        fake_client = self._patch_provider_client(FakeClient())
        live_guests = [
            self._live_guest(object_type="vm", vmid=500, name="Lab VM", node="pve1", status="stopped"),
            self._live_guest(object_type="vm", vmid=501, name="Other VM", node="pve1", status="stopped"),
        ]
        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=live_guests),
            patch("core.views.common.cluster_scoped_clients", return_value=[fake_client]),
        ):
            response = self.client.post(
                reverse("core:vms_bulk_action"),
                {
                    "bulk_action": "tags",
                    "guest": [
                        GuestRef(self.cluster.key, "vm", 500).serialize(),
                        GuestRef(self.cluster.key, "vm", 501).serialize(),
                    ],
                    "tags_mode": "add",
                    "tags_value": "new old",
                },
            )

        self.assertRedirects(response, reverse("core:vms_overview"), fetch_redirect_response=False)
        self.assertEqual(
            fake_client.updates,
            [
                (500, {"tags": "old;new"}, []),
                (501, {"tags": "new;old"}, []),
            ],
        )
        self.assertEqual(AuditEvent.objects.filter(action="guest.tags.updated").count(), 2)

    def test_scheduled_tasks_page_lists_definitions_and_runs(self):
        user = get_user_model().objects.create_user(username="scheduler", password="unused")
        self.client.force_login(user)
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        ProxmoxInventory.objects.create(
            scan_run=scan,
            node="pve1",
            object_type=ProxmoxInventory.ObjectType.VM,
            vmid=500,
            name="Lab VM",
        )
        CurrentGuestInventory.objects.create(
            cluster=self.cluster,
            source_scan=scan,
            node="pve1",
            object_type=CurrentGuestInventory.ObjectType.VM,
            vmid=500,
            name="Lab VM",
            observed_at=timezone.now(),
        )
        action = ScheduledAction.objects.create(
            cluster=self.cluster,
            name="Night shutdown",
            action_type=ScheduledAction.ActionType.SHUTDOWN,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=500,
            target_name_snapshot="Lab VM",
            target_node="pve1",
            schedule_type=ScheduledAction.ScheduleType.RECURRING,
            recurrence_kind=ScheduledAction.RecurrenceKind.MONTHLY_ORDINAL,
            recurrence={"ordinal": "first", "weekday": "sunday", "time": "22:00"},
            next_run_at=timezone.now() + timedelta(days=1),
            created_by=user,
            last_status=ScheduledAction.LastStatus.COMPLETED,
        )
        ScheduledAction.objects.create(
            cluster=self.cluster,
            name="Container reboot",
            action_type=ScheduledAction.ActionType.REBOOT,
            target_type=ScheduledAction.TargetType.CT,
            target_vmid=101,
            target_name_snapshot="Lab CT",
            target_node="pve1",
            created_by=user,
        )
        ScheduledActionRun.objects.create(
            scheduled_action=action,
            planned_for=timezone.now(),
            occurrence_key="recent",
            status=ScheduledActionRun.Status.COMPLETED,
            outcome=ScheduledActionRun.Outcome.SUCCESS,
            started_at=timezone.now(),
            finished_at=timezone.now(),
            proxmox_task_node="pve1",
        )

        with patch(
            "core.views.common.fetch_live_guest_inventory",
            side_effect=AssertionError("overview should not fetch live targets"),
        ):
            response = self.client.get(reverse("core:scheduled_tasks"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Scheduled Tasks")
        self.assertContains(response, "Scheduled tasks only run while pve-helper")
        self.assertContains(response, "enabled")
        self.assertContains(response, "Night shutdown")
        self.assertContains(response, "guest-label")
        self.assertContains(response, 'title="500 (Lab VM)"')
        self.assertContains(response, '<span class="guest-vmid">500</span>', html=True)
        self.assertContains(response, '<span class="guest-name">Lab VM</span>', html=True)
        self.assertContains(response, "Monthly on the first sunday at 22:00")
        self.assertContains(response, "Latest 10 Runs")
        self.assertContains(response, "Success")

        with patch(
            "core.views.common.fetch_live_guest_inventory",
            side_effect=AssertionError("overview should not fetch live targets"),
        ):
            response = self.client.get(
                reverse("core:scheduled_tasks"), {"target": GuestRef(self.cluster.key, "vm", 500).serialize()}
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "filtered to VM 500 (Lab VM)")
        self.assertContains(response, "Night shutdown")
        self.assertContains(response, "target=gr1%3Adefault%3Avm%3A500")
        self.assertNotContains(response, "Container reboot")

    def test_scheduled_task_runs_endpoint_lists_and_filters_latest_runs(self):
        user = get_user_model().objects.create_user(username="scheduler", password="unused")
        self.client.force_login(user)
        vm_action = ScheduledAction.objects.create(
            cluster=self.cluster,
            name="Night shutdown",
            action_type=ScheduledAction.ActionType.SHUTDOWN,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=500,
            target_name_snapshot="Lab VM",
            target_node="pve1",
            created_by=user,
        )
        ct_action = ScheduledAction.objects.create(
            cluster=self.cluster,
            name="Container reboot",
            action_type=ScheduledAction.ActionType.REBOOT,
            target_type=ScheduledAction.TargetType.CT,
            target_vmid=101,
            target_name_snapshot="Lab CT",
            target_node="pve1",
            created_by=user,
        )
        run = ScheduledActionRun.objects.create(
            scheduled_action=vm_action,
            planned_for=timezone.now(),
            occurrence_key="recent",
            status=ScheduledActionRun.Status.COMPLETED,
            outcome=ScheduledActionRun.Outcome.SUCCESS_NOOP,
            started_at=timezone.now(),
            finished_at=timezone.now(),
            proxmox_task_node="pve1",
        )
        ScheduledActionRun.objects.create(
            scheduled_action=ct_action,
            planned_for=timezone.now(),
            occurrence_key="ct-recent",
            status=ScheduledActionRun.Status.QUEUED,
        )

        response = self.client.get(
            reverse("core:scheduled_task_runs"), {"target": GuestRef(self.cluster.key, "vm", 500).serialize()}
        )

        self.assertEqual(response.status_code, 200)
        runs = response.json()["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["task"], "Night shutdown")
        self.assertEqual(runs[0]["target"], "VM 500 (Lab VM)")
        self.assertEqual(runs[0]["status"], "Completed")
        self.assertEqual(runs[0]["status_class"], "completed")
        self.assertEqual(runs[0]["outcome"], "Success - no action needed")
        self.assertEqual(runs[0]["node"], "pve1")
        self.assertRegex(runs[0]["planned_for"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(ScheduledActionRun.objects.get(pk=run.pk).status, ScheduledActionRun.Status.COMPLETED)

    def test_scheduled_task_runs_endpoint_limits_latest_runs_to_ten(self):
        user = get_user_model().objects.create_user(username="scheduler", password="unused")
        self.client.force_login(user)
        action = ScheduledAction.objects.create(
            name="Night shutdown",
            action_type=ScheduledAction.ActionType.SHUTDOWN,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=500,
            target_name_snapshot="Lab VM",
            target_node="pve1",
            created_by=user,
        )
        for index in range(12):
            ScheduledActionRun.objects.create(
                scheduled_action=action,
                planned_for=timezone.now(),
                occurrence_key=f"recent-{index}",
                status=ScheduledActionRun.Status.COMPLETED,
                outcome=ScheduledActionRun.Outcome.SUCCESS,
                started_at=timezone.now(),
                finished_at=timezone.now(),
            )

        response = self.client.get(reverse("core:scheduled_task_runs"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["runs"]), 10)

    def test_scheduled_task_create_form_creates_recurring_task(self):
        user = get_user_model().objects.create_user(username="scheduler", password="unused")
        self.client.force_login(user)
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        ProxmoxInventory.objects.create(
            scan_run=scan,
            node="pve1",
            object_type=ProxmoxInventory.ObjectType.VM,
            vmid=500,
            name="Lab VM",
        )

        with patch("core.views.common.fetch_live_guest_inventory", return_value=[self._live_guest()]):
            response = self.client.get(reverse("core:scheduled_task_create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Lab VM")
        target_ref = GuestRef(self.cluster.key, "vm", 500).serialize()
        self.assertContains(response, f'value="{target_ref}"')
        self.assertContains(response, "Current Node")
        self.assertContains(response, 'data-node="pve1"')
        self.assertContains(response, "Timeout (seconds)")
        self.assertContains(response, "Monthly by weekday")
        self.assertContains(response, "Monthly by date")
        self.assertContains(response, "Run Time")
        self.assertContains(response, "Retry Window (hours)")
        self.assertContains(response, "Schedule Preview")
        self.assertContains(response, "Weeks")
        self.assertContains(response, "Months")

        with patch("core.views.common.fetch_live_guest_inventory", return_value=[self._live_guest()]):
            response = self.client.get(reverse("core:scheduled_task_create"), {"target": target_ref})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Lab VM")
        self.assertContains(response, f'value="{target_ref}" data-node="pve1" selected')

        with patch("core.views.common.fetch_live_guest_inventory", return_value=[self._live_guest()]):
            response = self.client.post(
                reverse("core:scheduled_task_create"),
                {
                    "name": "Night shutdown",
                    "enabled": "on",
                    "target": target_ref,
                    "action_type": ScheduledAction.ActionType.SHUTDOWN,
                    "action_timeout_seconds": "900",
                    "recurrence_kind": ScheduledAction.RecurrenceKind.MONTHLY_ORDINAL,
                    "run_hour": "22",
                    "run_minute": "0",
                    "weekdays_present": "1",
                    "weekdays": ["2"],
                    "ordinals_present": "1",
                    "ordinals": ["second", "fourth"],
                    "months_present": "1",
                    "months": ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"],
                    "catch_up_enabled": "on",
                    "max_lateness_hours": "2",
                },
            )

        self.assertRedirects(response, reverse("core:scheduled_tasks"), fetch_redirect_response=False)
        action = ScheduledAction.objects.get(name="Night shutdown")
        self.assertTrue(action.enabled)
        self.assertEqual(action.created_by, user)
        self.assertEqual(action.cluster, self.cluster)
        self.assertEqual(action.target_node, "pve1")
        self.assertEqual(action.target_name_snapshot, "Lab VM")
        self.assertEqual(action.recurrence["ordinals"], ["second", "fourth"])
        self.assertEqual(action.recurrence["weekdays"], ["2"])
        self.assertEqual(action.max_lateness_minutes, 120)
        self.assertIsNotNone(action.next_run_at)
        self.assertTrue(AuditEvent.objects.filter(action="scheduled_action.created").exists())

    def test_scheduled_task_create_rejects_duplicate_name(self):
        user = get_user_model().objects.create_user(username="scheduler", password="unused")
        self.client.force_login(user)
        ScheduledAction.objects.create(
            name="Night shutdown",
            action_type=ScheduledAction.ActionType.SHUTDOWN,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=500,
        )

        with patch("core.views.common.fetch_live_guest_inventory", return_value=[self._live_guest()]):
            response = self.client.post(
                reverse("core:scheduled_task_create"),
                {
                    "name": "Night shutdown",
                    "enabled": "on",
                    "target": GuestRef(self.cluster.key, "vm", 500).serialize(),
                    "action_type": ScheduledAction.ActionType.SHUTDOWN,
                    "action_timeout_seconds": "900",
                    "recurrence_kind": "once",
                    "run_date": "2026-07-03",
                    "run_hour": "22",
                    "run_minute": "0",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "A scheduled task with this name already exists.", status_code=400)
        self.assertEqual(ScheduledAction.objects.filter(name="Night shutdown").count(), 1)

    def test_scheduled_task_create_form_uses_live_targets_without_scan(self):
        user = get_user_model().objects.create_user(username="scheduler", password="unused")
        self.client.force_login(user)

        live_guests = [
            self._live_guest(object_type="vm", vmid=500, name="Lab VM", node="pve1"),
            self._live_guest(object_type="ct", vmid=101, name="Lab CT", node="pve2"),
        ]
        with patch("core.views.common.fetch_live_guest_inventory", return_value=live_guests):
            response = self.client.get(reverse("core:scheduled_task_create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="gr1:default:vm:500"')
        self.assertContains(response, "VM 500 (Lab VM)")
        self.assertContains(response, 'data-node="pve1"')
        self.assertContains(response, 'value="gr1:default:ct:101"')
        self.assertContains(response, "Container 101 (Lab CT)")
        self.assertContains(response, 'data-node="pve2"')

    def test_scheduled_task_create_form_creates_one_time_task_with_24h_fields(self):
        user = get_user_model().objects.create_user(username="scheduler", password="unused")
        self.client.force_login(user)
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        ProxmoxInventory.objects.create(
            scan_run=scan,
            node="pve1",
            object_type=ProxmoxInventory.ObjectType.VM,
            vmid=500,
            name="Lab VM",
        )

        with patch("core.views.common.fetch_live_guest_inventory", return_value=[self._live_guest()]):
            response = self.client.post(
                reverse("core:scheduled_task_create"),
                {
                    "name": "One-time reboot",
                    "enabled": "on",
                    "target": GuestRef(self.cluster.key, "vm", 500).serialize(),
                    "action_type": ScheduledAction.ActionType.REBOOT,
                    "action_timeout_seconds": "900",
                    "recurrence_kind": "once",
                    "run_date": "2026-07-03",
                    "run_hour": "22",
                    "run_minute": "5",
                    "max_lateness_hours": "1",
                },
            )

        self.assertRedirects(response, reverse("core:scheduled_tasks"), fetch_redirect_response=False)
        action = ScheduledAction.objects.get(name="One-time reboot")
        local_run_at = timezone.localtime(action.run_at)
        self.assertEqual(local_run_at.strftime("%Y-%m-%d %H:%M"), "2026-07-03 22:05")
        self.assertEqual(action.next_run_at, action.run_at)

    def test_scheduled_task_edit_rejects_duplicate_name(self):
        user = get_user_model().objects.create_user(username="scheduler", password="unused")
        self.client.force_login(user)
        ScheduledAction.objects.create(
            name="Night shutdown",
            action_type=ScheduledAction.ActionType.SHUTDOWN,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=500,
        )
        action = ScheduledAction.objects.create(
            name="Morning start",
            action_type=ScheduledAction.ActionType.START,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=501,
        )

        with patch("core.views.common.fetch_live_guest_inventory", return_value=[self._live_guest(vmid=501)]):
            response = self.client.post(
                reverse("core:scheduled_task_edit", args=[action.id]),
                {
                    "name": "Night shutdown",
                    "enabled": "on",
                    "target": GuestRef(self.cluster.key, "vm", 501).serialize(),
                    "action_type": ScheduledAction.ActionType.START,
                    "action_timeout_seconds": "900",
                    "recurrence_kind": "once",
                    "run_date": "2026-07-03",
                    "run_hour": "07",
                    "run_minute": "0",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "A scheduled task with this name already exists.", status_code=400)
        action.refresh_from_db()
        self.assertEqual(action.name, "Morning start")

    def test_scheduled_task_edit_form_updates_definition(self):
        user = get_user_model().objects.create_user(username="scheduler", password="unused")
        self.client.force_login(user)
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED)
        ProxmoxInventory.objects.create(
            scan_run=scan,
            node="pve1",
            object_type=ProxmoxInventory.ObjectType.VM,
            vmid=500,
            name="Lab VM",
        )
        action = ScheduledAction.objects.create(
            name="Old name",
            action_type=ScheduledAction.ActionType.START,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=500,
            schedule_type=ScheduledAction.ScheduleType.ONCE,
            run_at=timezone.now() + timedelta(hours=1),
            next_run_at=timezone.now() + timedelta(hours=1),
        )

        with patch("core.views.common.fetch_live_guest_inventory", return_value=[self._live_guest()]):
            response = self.client.post(
                reverse("core:scheduled_task_edit", args=[action.id]),
                {
                    "name": "Morning start",
                    "enabled": "on",
                    "target": GuestRef(self.cluster.key, "vm", 500).serialize(),
                    "action_type": ScheduledAction.ActionType.START,
                    "action_timeout_seconds": "1800",
                    "recurrence_kind": ScheduledAction.RecurrenceKind.DAILY,
                    "run_hour": "7",
                    "run_minute": "30",
                    "max_lateness_hours": "1",
                },
            )

        self.assertRedirects(response, reverse("core:scheduled_tasks"), fetch_redirect_response=False)
        action.refresh_from_db()
        self.assertEqual(action.name, "Morning start")
        self.assertEqual(action.schedule_type, ScheduledAction.ScheduleType.RECURRING)
        self.assertEqual(action.recurrence, {"time": "07:30"})
        self.assertTrue(AuditEvent.objects.filter(action="scheduled_action.updated").exists())

    def test_scheduled_task_toggle_and_delete_views(self):
        user = get_user_model().objects.create_user(username="scheduler", password="unused")
        self.client.force_login(user)
        action = ScheduledAction.objects.create(
            name="Daily reboot",
            enabled=False,
            action_type=ScheduledAction.ActionType.REBOOT,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=500,
            schedule_type=ScheduledAction.ScheduleType.RECURRING,
            recurrence_kind=ScheduledAction.RecurrenceKind.DAILY,
            recurrence={"time": "02:00"},
        )

        response = self.client.post(reverse("core:scheduled_task_toggle", args=[action.id]), {"enabled": "1"})

        self.assertRedirects(response, reverse("core:scheduled_tasks"), fetch_redirect_response=False)
        action.refresh_from_db()
        self.assertTrue(action.enabled)
        self.assertIsNotNone(action.next_run_at)
        self.assertTrue(AuditEvent.objects.filter(action="scheduled_action.enabled").exists())
        run = ScheduledActionRun.objects.create(
            scheduled_action=action,
            planned_for=timezone.now(),
            occurrence_key="completed-before-delete",
            status=ScheduledActionRun.Status.COMPLETED,
            outcome=ScheduledActionRun.Outcome.SUCCESS,
            finished_at=timezone.now(),
        )

        response = self.client.post(reverse("core:scheduled_task_delete", args=[action.id]))

        self.assertRedirects(response, reverse("core:scheduled_tasks"), fetch_redirect_response=False)
        action.refresh_from_db()
        self.assertIsNotNone(action.deleted_at)
        self.assertFalse(action.enabled)
        self.assertIsNone(action.next_run_at)
        self.assertTrue(ScheduledActionRun.objects.filter(pk=run.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(action="scheduled_action.deleted").exists())

        response = self.client.get(reverse("core:scheduled_tasks"))
        self.assertEqual(response.context["scheduled_actions"], [])

    def test_scheduled_task_delete_refuses_an_in_flight_run(self):
        user = get_user_model().objects.create_user(username="scheduler", password="unused")
        self.client.force_login(user)
        action = ScheduledAction.objects.create(
            name="In-flight shutdown",
            action_type=ScheduledAction.ActionType.SHUTDOWN,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=500,
        )
        ScheduledActionRun.objects.create(
            scheduled_action=action,
            planned_for=timezone.now(),
            occurrence_key="in-flight",
            status=ScheduledActionRun.Status.POLLING,
        )

        response = self.client.post(reverse("core:scheduled_task_delete", args=[action.id]), follow=True)

        self.assertEqual(response.status_code, 200)
        action.refresh_from_db()
        self.assertIsNone(action.deleted_at)
        self.assertContains(response, "run in progress and cannot be deleted")

    def test_scheduled_task_run_now_queues_manual_run(self):
        user = get_user_model().objects.create_user(username="scheduler", password="unused")
        self.client.force_login(user)
        action = ScheduledAction.objects.create(
            cluster=self.cluster,
            name="Manual start",
            action_type=ScheduledAction.ActionType.START,
            target_type=ScheduledAction.TargetType.VM,
            target_vmid=500,
        )

        with patch("core.services.scheduled_actions.async_task", return_value="manual-task-id"):
            response = self.client.post(reverse("core:scheduled_task_run_now", args=[action.id]))

        self.assertRedirects(response, reverse("core:scheduled_tasks"), fetch_redirect_response=False)
        run = ScheduledActionRun.objects.get(scheduled_action=action)
        self.assertEqual(run.status, ScheduledActionRun.Status.QUEUED)
        self.assertEqual(run.triggered_by, user)
        self.assertTrue(run.occurrence_key.startswith("manual:"))
        event = AuditEvent.objects.get(action="scheduled_action.run_queued")
        self.assertEqual(event.username, "scheduler")

    def test_dashboard_updates_scan_schedule(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        response = self.client.post(
            reverse("core:update_scan_schedule"),
            {"enabled": "on", "interval_minutes": "15"},
        )
        self.assertRedirects(response, reverse("core:dashboard"))
        self.assertEqual(list(get_messages(response.wsgi_request)), [])

        schedule = Schedule.objects.get(name=SCAN_SCHEDULE_NAME)
        self.assertEqual(schedule.func, "core.tasks.enqueue_scheduled_scan")
        self.assertEqual(schedule.schedule_type, Schedule.MINUTES)
        self.assertEqual(schedule.minutes, 15)

        response = self.client.post(
            reverse("core:update_scan_schedule"),
            {"interval_minutes": "15"},
        )
        self.assertRedirects(response, reverse("core:dashboard"))
        self.assertFalse(Schedule.objects.filter(name=SCAN_SCHEDULE_NAME).exists())

    def test_trash_purge_schedule_state_reads_text_kwargs(self):
        Schedule.objects.create(
            name=TRASH_PURGE_SCHEDULE_NAME,
            func="core.tasks.purge_expired_trash",
            schedule_type=Schedule.DAILY,
            repeats=-1,
            kwargs="{'max_age_days': 7}",
        )

        state = trash_purge_schedule_state()

        self.assertTrue(state.enabled)
        self.assertEqual(state.max_age_days, 7)

    def test_audit_log_shows_audit_retention_schedule(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        response = self.client.get(reverse("core:dashboard"))

        self.assertNotContains(response, "Keep audit logs for")

        response = self.client.get(reverse("core:audit_log"))

        self.assertContains(response, "Keep audit logs for")
        self.assertContains(response, 'name="retention_days"')

    def test_audit_log_updates_audit_retention_schedule(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)
        audit_url = reverse("core:audit_log")

        response = self.client.post(
            reverse("core:update_audit_retention_schedule"),
            {"enabled": "on", "retention_days": "120", "next": audit_url},
        )

        self.assertRedirects(response, audit_url)
        schedule = Schedule.objects.get(name=AUDIT_RETENTION_SCHEDULE_NAME)
        self.assertEqual(schedule.func, "core.tasks.purge_expired_audit_events")
        self.assertEqual(schedule.schedule_type, Schedule.DAILY)
        self.assertIn("120", schedule.kwargs)
        event = AuditEvent.objects.get(action="audit.retention.schedule.updated")
        self.assertEqual(event.details["retention_days"], 120)

        response = self.client.post(
            reverse("core:update_audit_retention_schedule"),
            {"retention_days": "120", "next": audit_url},
        )

        self.assertRedirects(response, audit_url)
        self.assertFalse(Schedule.objects.filter(name=AUDIT_RETENTION_SCHEDULE_NAME).exists())

    def test_audit_retention_schedule_state_reads_text_kwargs(self):
        Schedule.objects.create(
            name=AUDIT_RETENTION_SCHEDULE_NAME,
            func="core.tasks.purge_expired_audit_events",
            schedule_type=Schedule.DAILY,
            repeats=-1,
            kwargs="{'retention_days': 45}",
        )

        state = audit_retention_schedule_state()

        self.assertTrue(state.enabled)
        self.assertEqual(state.retention_days, 45)

    def test_audit_retention_purges_old_events(self):
        old_event = AuditEvent.objects.create(username="viewer", action="old.event")
        keep_event = AuditEvent.objects.create(username="viewer", action="new.event")
        AuditEvent.objects.filter(pk=old_event.pk).update(timestamp=timezone.now() - timedelta(days=10))
        AuditEvent.objects.filter(pk=keep_event.pk).update(timestamp=timezone.now() - timedelta(days=2))

        purge_expired_audit_events(retention_days=7)

        self.assertFalse(AuditEvent.objects.filter(pk=old_event.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(pk=keep_event.pk).exists())
        audit_event = AuditEvent.objects.get(action="audit.retention.purge")
        self.assertEqual(audit_event.details["retention_days"], 7)
        self.assertEqual(audit_event.details["purged"], 1)

    def test_record_storage_space_snapshots_samples_enabled_storages_and_purges_old_points(self):
        with TemporaryDirectory() as tmp:
            storage = StorageMount.objects.create(
                storage_id="nfs-fs",
                display_name="nfs-fs",
                path=tmp,
            )
            disabled_storage = StorageMount.objects.create(
                storage_id="disabled",
                display_name="disabled",
                path=tmp,
                enabled=False,
            )
            old_snapshot = StorageSpaceSnapshot.objects.create(
                storage=storage,
                recorded_at=timezone.now() - timedelta(days=10),
                total_bytes=1000,
                available_bytes=500,
                used_bytes=500,
            )

            # No Proxmox endpoints -> only the mounted storage is sampled here;
            # local (API) capacity recording is covered separately.
            with patch("core.tasks.cluster_clients", return_value=[]):
                created = record_storage_space_snapshots(retention_days=8)

        self.assertEqual(created, 1)
        self.assertFalse(StorageSpaceSnapshot.objects.filter(pk=old_snapshot.pk).exists())
        snapshot = StorageSpaceSnapshot.objects.get(storage=storage)
        self.assertIsNone(snapshot.scan_run)
        self.assertGreater(snapshot.total_bytes, 0)
        self.assertFalse(StorageSpaceSnapshot.objects.filter(storage=disabled_storage).exists())

    def test_record_storage_space_snapshots_records_local_api_storages(self):
        local = ClusterStorage.objects.create(
            cluster=self.cluster,
            storage_id="local-lvm",
            storage_type="lvmthin",
            shared=False,
            present=True,
        )
        ClusterStorageNodeState.objects.create(
            cluster_storage=local,
            node="pve1",
            present=True,
            active=True,
            total_bytes=1000,
            used_bytes=400,
            available_bytes=600,
        )
        shared = ClusterStorage.objects.create(
            cluster=self.cluster,
            storage_id="cephfs",
            storage_type="cephfs",
            shared=True,
            present=True,
        )
        ClusterStorageNodeState.objects.create(
            cluster_storage=shared,
            node="pve1",
            present=True,
            active=True,
            total_bytes=5000,
            used_bytes=100,
            available_bytes=4900,
        )

        created = record_storage_space_snapshots()

        locals_ = StorageSpaceSnapshot.objects.filter(storage__isnull=True)
        self.assertEqual(created, 1)  # only the non-shared storage; shared is skipped
        self.assertEqual(locals_.count(), 1)
        snap = locals_.get()
        self.assertEqual((snap.node, snap.api_storage_id), ("pve1", "local-lvm"))
        self.assertEqual(snap.used_bytes, 400)
        self.assertIsNone(snap.storage)

    def test_start_scan_is_silent_and_scan_status_updates_button_state(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)

        response = self.client.post(reverse("core:start_scan"))
        self.assertRedirects(response, reverse("core:dashboard"))
        self.assertEqual(list(get_messages(response.wsgi_request)), [])

        scan = ScanRun.objects.latest("created_at")
        self.assertEqual(scan.status, ScanRun.Status.QUEUED)

        response = self.client.get(reverse("core:scan_status"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["active"])
        self.assertEqual(payload["button_label"], "Scan queued")

        scan.status = ScanRun.Status.RUNNING
        scan.save(update_fields=["status", "updated_at"])
        response = self.client.get(reverse("core:scan_status"))
        self.assertEqual(response.json()["button_label"], "Scanning")

        scan.status = ScanRun.Status.COMPLETED
        scan.save(update_fields=["status", "updated_at"])
        response = self.client.get(reverse("core:scan_status"))
        self.assertFalse(response.json()["active"])
        self.assertEqual(response.json()["button_label"], "Start scan")

    def test_storage_browser_scan_targets_selected_storage_and_returns_to_page(self):
        user = get_user_model().objects.create_user(username="viewer", password="unused")
        self.client.force_login(user)
        storage = StorageMount.objects.create(
            storage_id="nfs-fs",
            display_name="nfs-fs",
            path="/storages/truenas-fs",
            expected_consumers=["pve-node-1"],
        )

        response = self.client.post(
            reverse("core:start_scan"),
            {
                "storage_id": storage.storage_id,
                "next": browser_url(storage.mount_ref),
            },
        )

        self.assertRedirects(response, browser_url(storage.mount_ref))
        scan = ScanRun.objects.latest("created_at")
        self.assertEqual(scan.target_storage, storage)
        self.assertEqual(scan.target_label, "nfs-fs")

        task_page = recent_task_page()
        self.assertEqual(task_page.tasks[0]["target"], "nfs-fs")


class QemuImgFailureMessageTests(SimpleTestCase):
    """qemu-img says why it failed only in free-form English on stderr.

    Recognising the handful of causes an operator can act on is worth doing; the
    contract is that a recognised one is named, an unrecognised one is admitted
    rather than guessed at, and neither answer ever carries the raw text. The last
    part is not only a response-boundary rule here: `probe_qemu_image_info` writes
    its result into `FileInventory.evidence`, so raw stderr would be persisted.
    """

    CAUSES = {
        "qemu-img: error while writing sector 8192: No space left on device": "out of free space",
        "qemu-img: Could not open '/mnt/x.qcow2': Disk quota exceeded": "quota is exhausted",
        "qemu-img: Could not open '/mnt/x.qcow2': Permission denied": "not permitted to access",
        "qemu-img: Could not create '/mnt/x': Read-only file system": "mounted read-only",
        "qemu-img: error while reading sector 0: Input/output error": "I/O error",
        "qcow2: Image is corrupt; cannot be opened read/write": "image is corrupt",
        "qemu-img: Could not open '/mnt/x.raw': Image is not in qcow2 format": "not in qcow2 format",
    }

    def test_each_recognised_cause_is_named(self):
        from core.services.image_info import qemu_img_failure_cause

        for stderr, expected in self.CAUSES.items():
            with self.subTest(stderr=stderr):
                self.assertIn(expected, qemu_img_failure_cause(stderr) or "")

    def test_an_unrecognised_cause_stays_unrecognised(self):
        from core.services.image_info import qemu_img_failure_cause

        self.assertIsNone(qemu_img_failure_cause("qemu-img: something nobody has seen before"))
        self.assertIsNone(qemu_img_failure_cause(""))

    def test_the_inflate_message_frames_the_cause_and_promises_the_original(self):
        from core.services.storage_actions import _inflate_failure_message

        named = _inflate_failure_message("qemu-img: error while writing: No space left on device")
        self.assertIn("out of free space", named)
        self.assertIn("original file was left unchanged", named)

        unnamed = _inflate_failure_message("qemu-img: something nobody has seen before")
        self.assertIn("no cause pve-helper recognises", unnamed)
        self.assertIn("original file was left unchanged", unnamed)
        self.assertIn("application log", unnamed)

    def test_probe_errors_are_stable_text_and_the_raw_output_goes_to_the_log(self):
        from core.services.image_info import _probe_failure

        raw = "qemu-img: Could not open '/storages/truenas-vm/images/501/vm-501-disk-0.qcow2': Permission denied"
        with self.assertLogs("core.services.image_info", level="WARNING") as logs:
            message = _probe_failure(
                subject="Image details are unavailable.",
                command="info",
                stderr=raw,
                path="/storages/truenas-vm/images/501/vm-501-disk-0.qcow2",
            )
        self.assertEqual(message, "Image details are unavailable. pve-helper is not permitted to access this file.")
        self.assertIn(raw, "\n".join(logs.output))

    def test_no_probe_message_ever_carries_the_raw_output(self):
        from core.services.image_info import _probe_failure

        # Both branches, because the fallback is the one that is tempting to widen
        # into "and here is what qemu-img said".
        for stderr in (
            "qemu-img: Could not open '/storages/truenas-vm/images/501/x.qcow2': Permission denied",
            "qemu-img: Could not open '/storages/truenas-vm/images/501/x.qcow2': mystery",
        ):
            with self.subTest(stderr=stderr), self.assertLogs("core.services.image_info", level="WARNING"):
                message = _probe_failure(
                    subject="The qcow2 map could not be read.", command="check", stderr=stderr, path="/x"
                )
                self.assertNotIn("/storages/truenas-vm", message)


class VmRegisterServiceTests(SimpleTestCase):
    def test_vmid_from_volid(self):
        from core.services.vm_register import vmid_from_volid

        self.assertEqual(vmid_from_volid("TrueNAS-VM:9999/vm-9999-disk-0.qcow2"), 9999)
        self.assertEqual(vmid_from_volid("stor:100/base-100-disk-1.raw"), 100)
        self.assertIsNone(vmid_from_volid("stor:iso/foo.iso"))
        self.assertIsNone(vmid_from_volid(""))

    def test_base_body_omits_i440fx_but_sets_q35(self):
        from core.services.vm_register import _base_body

        body = _base_body({"vmid": "101", "name": "x", "machine": "i440fx", "bios": "seabios", "nics": []})
        self.assertNotIn("machine", body)  # i440fx is the default; sending it 400s
        self.assertEqual(body["bios"], "seabios")
        self.assertEqual(body["ide2"], "none,media=cdrom")

        body_q35 = _base_body({"vmid": "1", "name": "x", "machine": "q35", "bios": "seabios", "nics": []})
        self.assertEqual(body_q35["machine"], "q35")

    def test_base_body_ovmf_efidisk_fresh_and_imported(self):
        from core.services.vm_register import _base_body

        fresh = _base_body({"vmid": "1", "name": "x", "bios": "ovmf", "efidisk_storage": "S", "nics": []})
        self.assertEqual(fresh["efidisk0"], "S:1,efitype=4m,pre-enrolled-keys=1")

        imported = _base_body(
            {
                "vmid": "1",
                "name": "x",
                "bios": "ovmf",
                "efidisk_storage": "S",
                "efidisk_source": "S:9/vm-9-disk-0.raw",
                "nics": [],
            }
        )
        self.assertIn("import-from=S:9/vm-9-disk-0.raw", imported["efidisk0"])

    def test_base_body_multiple_nics(self):
        from core.services.vm_register import _base_body

        body = _base_body(
            {
                "vmid": "1",
                "name": "x",
                "nics": [
                    {"model": "e1000", "bridge": "vmbr0"},
                    {"model": "virtio", "bridge": "vmbr1", "vlan": "5"},
                    {"model": "e1000", "bridge": ""},  # skipped (no bridge)
                ],
            }
        )
        self.assertEqual(body["net0"], "e1000,bridge=vmbr0")
        self.assertEqual(body["net1"], "virtio,bridge=vmbr1,tag=5")
        self.assertNotIn("net2", body)


class VmRegisterViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("vmreg-tester", is_staff=True, is_superuser=True)
        self.client.force_login(self.user)
        self.cluster = ProxmoxCluster.objects.create(key="default", display_name="Default cluster", enabled=True)

    _OPTS = {
        "available": True,
        "node": "pve3",
        "nextid": "101",
        "disk_storages": ["TrueNAS-VM"],
        "bridges": ["vmbr0"],
        "ostypes": [("l26", "Linux (modern)")],
    }

    @patch("core.views.vm_register.create_options")
    def test_get_import_shows_target_storage(self, mock_opts):
        mock_opts.return_value = dict(self._OPTS)
        resp = self.client.get(
            "/vms/default/register/?mode=import&storage=TrueNAS-FS&path=disk.qcow2",
            SERVER_NAME="localhost",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="target_storage"')
        self.assertContains(resp, "vmreg-source")

    @patch("core.views.vm_register.create_options")
    def test_get_adopt_hides_target_storage(self, mock_opts):
        mock_opts.return_value = dict(self._OPTS)
        resp = self.client.get(
            "/vms/default/register/?mode=adopt&volid=TrueNAS-VM:9/vm-9-disk-0.qcow2",
            SERVER_NAME="localhost",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'name="target_storage"')
        self.assertContains(resp, "vm-9-disk-0")

    @patch("core.views.vm_register.create_options")
    def test_post_rejects_bad_vmid(self, mock_opts):
        mock_opts.return_value = dict(self._OPTS)
        resp = self.client.post(
            "/vms/default/register/",
            {"mode": "adopt", "node": "pve3", "volid": "TrueNAS-VM:9/vm-9-disk-0.qcow2", "vmid": "abc", "name": "n"},
            SERVER_NAME="localhost",
        )
        self.assertEqual(resp.status_code, 200)  # re-rendered with an error message
        self.assertContains(resp, "VMID must be a whole number")


@override_settings(BACKUP_TASK_TIMEOUT_SECONDS=3600)
class GuestBackupRestoreTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("backup-operator", password="unused")
        self.client.force_login(self.user)
        self.cluster = ProxmoxCluster.objects.create(key="default", display_name="Default cluster", enabled=True)
        CurrentGuestInventory.objects.create(
            cluster=self.cluster,
            object_type="vm",
            vmid=500,
            node="pve1",
            name="Lab VM",
            status="stopped",
            config={},
            config_complete=False,
            observed_at=timezone.now(),
            runtime_observed_at=timezone.now(),
        )

    def _seed_storage_catalog(self, *archives: str, nodes=("pve1", "pve2")):
        metadata_generation = uuid.uuid4()
        volume_generation = uuid.uuid4()
        backup = ClusterStorage.objects.create(
            cluster=self.cluster,
            storage_id="backup",
            storage_type="pbs",
            content=["backup"],
            shared=True,
            observed_metadata_generation=metadata_generation,
        )
        images = ClusterStorage.objects.create(
            cluster=self.cluster,
            storage_id="images",
            storage_type="lvmthin",
            content=["images"],
            observed_metadata_generation=metadata_generation,
        )
        for node in nodes:
            ClusterStorageNodeState.objects.create(
                cluster_storage=backup,
                node=node,
                active=True,
                enabled=True,
                observed_metadata_generation=metadata_generation,
            )
            ClusterStorageNodeState.objects.create(
                cluster_storage=images,
                node=node,
                active=True,
                enabled=True,
                observed_metadata_generation=metadata_generation,
            )
            ClusterStorageVolumeCoverage.objects.create(
                cluster_storage=images,
                scope=ClusterStorageVolumeCoverage.Scope.NODE,
                node=node,
                volume_generation=volume_generation,
                based_on_metadata_generation=metadata_generation,
                refreshed_at=timezone.now(),
                last_attempt_at=timezone.now(),
                complete=True,
            )
        # A shared definition publishes one logical set under the empty node; the
        # per-node agreement lives on the coverage row.
        for archive in archives:
            ClusterStorageVolumeObservation.objects.create(
                cluster_storage=backup,
                node=SHARED_OBSERVATION_NODE,
                volid=archive,
                content="backup",
                observed_volume_generation=volume_generation,
                based_on_metadata_generation=metadata_generation,
                last_seen_at=timezone.now(),
            )
        ClusterStorageVolumeCoverage.objects.create(
            cluster_storage=backup,
            scope=ClusterStorageVolumeCoverage.Scope.SHARED,
            volume_generation=volume_generation,
            based_on_metadata_generation=metadata_generation,
            refreshed_at=timezone.now(),
            last_attempt_at=timezone.now(),
            complete=True,
            agreeing_nodes=sorted(nodes),
        )
        StorageCatalogState.objects.create(
            cluster=self.cluster,
            metadata_generation=metadata_generation,
            metadata_complete=True,
            volume_complete=True,
        )

    @staticmethod
    def _guest(*, vmid=500, object_type="vm", node="pve1", name="Lab VM", status="stopped"):
        guest = Mock()
        guest.vmid = vmid
        guest.object_type = object_type
        guest.node = node
        guest.name = name
        guest.status = status
        return guest

    def test_backup_now_tracks_the_vzdump_upid(self):
        self._seed_storage_catalog(nodes=("pve1",))

        class FakeClient:
            endpoint = "https://pve1.invalid:8006"

            def guest_current(self, **_kwargs):
                return {"status": "running"}

            def guest_config(self, **_kwargs):
                return {"name": "Lab VM"}

            def get(self, path, **_kwargs):
                if path == "nodes/pve1/storage":
                    return [{"storage": "backup", "content": "backup", "active": 1}]
                raise ProxmoxAPIError(path)

            def post(self, path, data):
                self.path, self.data = path, data
                return "UPID:pve1:vzdump:500:root@pam:"

        fake = FakeClient()
        with (
            patch("core.views.common.fetch_live_guest_inventory", return_value=[self._guest()]),
            patch("core.views.common.cluster_scoped_clients", return_value=[fake]),
            patch("core.views.common.async_task", return_value="poll-backup-1") as enqueue,
        ):
            response = self.client.post(
                reverse("core:guest_backup_now", args=[self.cluster.key, "vm", 500]),
                {"storage": "backup", "mode": "snapshot", "compress": "zstd", "protected": "on"},
                HTTP_X_REQUESTED_WITH="fetch",
            )

        self.assertEqual(response.json(), {"ok": True, "errors": []})
        self.assertEqual(fake.path, "nodes/pve1/vzdump")
        self.assertEqual(fake.data["protected"], 1)
        self.assertEqual(fake.data["notification-mode"], "auto")
        event = AuditEvent.objects.get(action="guest.backup.run")
        self.assertEqual(event.outcome, "running")
        self.assertEqual(event.details["proxmox_task_upid"], "UPID:pve1:vzdump:500:root@pam:")
        self.assertEqual(enqueue.call_args.args[0], "core.tasks.poll_guest_audit_task")

    def test_restore_queues_a_worker_for_a_new_vmid(self):
        archive = "backup:vzdump-qemu-500-2026_07_11-12_00_00.vma.zst"
        self._seed_storage_catalog(archive, nodes=("pve1",))

        class FakeClient:
            endpoint = "https://pve1.invalid:8006"

            def node_names(self, fallback=""):
                return ["pve1"]

            def get(self, path, **_kwargs):
                if path == "cluster/nextid":
                    return 501
                if path == "nodes/pve1/storage":
                    return [
                        {"storage": "backup", "content": "backup", "active": 1},
                        {"storage": "images", "content": "images", "active": 1},
                    ]
                if path == "nodes/pve1/storage/backup/content?content=backup":
                    return [{"volid": archive, "ctime": 1, "size": 42}]
                raise ProxmoxAPIError(path)

        fake = FakeClient()
        key = f"{fake.endpoint}|pve1|backup|{archive}"
        with (
            patch("core.views.common.cluster_scoped_clients", return_value=[fake]),
            patch("core.views.common.fetch_live_guest_inventory", return_value=[]),
            patch("core.views.common.async_task", return_value="restore-worker-1") as enqueue,
        ):
            response = self.client.post(
                reverse("core:guest_backup_restore", args=[self.cluster.key]),
                {
                    "archive_key": key,
                    "node": f"{fake.endpoint}|pve1",
                    "storage": "images",
                    "vmid": "501",
                    "start_after": "on",
                },
            )

        self.assertRedirects(response, reverse("core:vms"), fetch_redirect_response=False)
        event = AuditEvent.objects.get(action="guest.backup.restore")
        self.assertEqual(event.outcome, "running")
        self.assertEqual(event.details["archive"], archive)
        self.assertEqual(enqueue.call_args.args, ("core.tasks.restore_guest_backup_task", event.id))
        self.assertEqual(event.details["target_type"], "vm")
        self.assertEqual(event.details["vmid"], 501)

    def test_restore_page_embeds_parseable_storage_options(self):
        archive = "backup:vzdump-qemu-500-2026_07_11-12_00_00.vma.zst"
        other_archive = "backup:vzdump-qemu-507-2026_07_11-12_00_00.vma.zst"
        self._seed_storage_catalog(archive, other_archive)

        class FakeClient:
            endpoint = "https://pve1.invalid:8006"

            def node_names(self, fallback=""):
                return ["pve1", "pve2"]

            def get(self, path, **_kwargs):
                if path == "cluster/nextid":
                    return 501
                if path in {"nodes/pve1/storage", "nodes/pve2/storage"}:
                    return [
                        {"storage": "backup", "content": "backup", "active": 1},
                        {"storage": "images", "content": "images", "active": 1},
                    ]
                if path in {
                    "nodes/pve1/storage/backup/content?content=backup",
                    "nodes/pve2/storage/backup/content?content=backup",
                }:
                    return [{"volid": archive}, {"volid": other_archive}]
                raise ProxmoxAPIError(path)

        with patch("core.views.common.cluster_scoped_clients", return_value=[FakeClient()]):
            response = self.client.get(
                reverse("core:guest_backup_restore", args=[self.cluster.key]),
                {"source_type": "vm", "source_vmid": "500"},
            )

        self.assertContains(response, 'id="restore-storage-options"')
        self.assertContains(response, '"vm": ["images"]')
        self.assertNotContains(response, r"{\u0022")
        self.assertEqual(len(response.context["archives"]), 1)
        self.assertEqual(response.context["archives"][0]["source_vmid"], 500)

    def test_overwrite_uses_fresh_power_state(self):
        archive = "backup:vzdump-qemu-500-2026_07_11-12_00_00.vma.zst"
        self._seed_storage_catalog(archive, nodes=("pve1",))

        class FakeClient:
            endpoint = "https://pve1.invalid:8006"

            def node_names(self, fallback=""):
                return ["pve1"]

            def guest_config(self, **_kwargs):
                return {"name": "Lab VM"}

            def guest_current(self, **_kwargs):
                return {"status": "running"}

            def get(self, path, **_kwargs):
                if path == "cluster/nextid":
                    return 501
                if path == "nodes/pve1/storage":
                    return [
                        {"storage": "backup", "content": "backup", "active": 1},
                        {"storage": "images", "content": "images", "active": 1},
                    ]
                if path == "nodes/pve1/storage/backup/content?content=backup":
                    return [{"volid": archive}]
                raise ProxmoxAPIError(path)

        fake = FakeClient()
        key = f"{fake.endpoint}|pve1|backup|{archive}"
        stale_guest = self._guest(status="stopped")
        with (
            patch("core.views.common.cluster_scoped_clients", return_value=[fake]),
            patch("core.views.common.fetch_live_guest_inventory", return_value=[stale_guest]),
            patch("core.views.common.async_task", return_value="restore-worker-2") as enqueue,
        ):
            response = self.client.post(
                reverse("core:guest_backup_restore", args=[self.cluster.key]),
                {
                    "archive_key": key,
                    "node": f"{fake.endpoint}|pve1",
                    "storage": "images",
                    "vmid": "500",
                    "overwrite": "on",
                    "overwrite_confirm": "500",
                },
            )

        self.assertRedirects(response, reverse("core:vms"), fetch_redirect_response=False)
        event = AuditEvent.objects.get(action="guest.backup.restore")
        self.assertEqual(enqueue.call_args.args, ("core.tasks.restore_guest_backup_task", event.id))
        self.assertTrue(event.details["overwrite"])
        self.assertTrue(event.details["shutdown_first"])

    def test_restore_worker_records_each_proxmox_stage(self):
        event = AuditEvent.objects.create(
            cluster=self.cluster,
            action="guest.backup.restore",
            object_type="guest",
            object_id="vm:501",
            outcome="running",
            details={"node": "pve1", "vmid": 501, "target_type": "vm", "name": "Restored VM"},
        )

        class FakeClient:
            def __init__(self, _endpoint):
                self.posts = []

            def post(self, path, data):
                self.posts.append((path, data))
                return f"UPID:pve1:{len(self.posts)}"

            def wait_for_task(self, *, node, upid, timeout_seconds):
                return ProxmoxTaskResult(node=node, upid=upid, status="stopped", exitstatus="OK", raw={})

        fake = FakeClient("unused")
        with patch(
            "core.tasks.client_for_audit_event",
            return_value=(fake, GuestRef("default", "vm", 501, "pve1"), self.cluster),
        ):
            restore_guest_backup_task(
                event.id,
                "https://pve1.invalid:8006",
                "pve1",
                "vm",
                501,
                "backup:vzdump-qemu-500.vma.zst",
                "images",
                False,
                False,
                True,
                3600,
            )

        event.refresh_from_db()
        self.assertEqual(event.outcome, "success")
        self.assertEqual(event.details["completed_stages"], ["restore archive", "start restored guest"])

    def test_restore_worker_rechecks_and_shuts_down_running_guest(self):
        event = AuditEvent.objects.create(
            cluster=self.cluster,
            action="guest.backup.restore",
            object_type="guest",
            object_id="vm:500",
            outcome="running",
            details={"node": "pve1", "vmid": 500, "target_type": "vm", "name": "Lab VM"},
        )

        class FakeClient:
            def __init__(self, _endpoint):
                self.posts = []
                self.statuses = iter(("running", "stopped"))

            def guest_current(self, **_kwargs):
                return {"status": next(self.statuses)}

            def post(self, path, data):
                self.posts.append((path, data))
                return f"UPID:pve1:{len(self.posts)}"

            def wait_for_task(self, *, node, upid, timeout_seconds):
                return ProxmoxTaskResult(node=node, upid=upid, status="stopped", exitstatus="OK", raw={})

        fake = FakeClient("unused")
        with patch(
            "core.tasks.client_for_audit_event",
            return_value=(fake, GuestRef("default", "vm", 500, "pve1"), self.cluster),
        ):
            restore_guest_backup_task(
                event.id,
                "https://pve1.invalid:8006",
                "pve1",
                "vm",
                500,
                "backup:vzdump-qemu-500.vma.zst",
                "images",
                True,
                False,
                False,
                3600,
            )

        event.refresh_from_db()
        self.assertEqual(event.outcome, "success")
        self.assertEqual(
            [path for path, _data in fake.posts],
            ["nodes/pve1/qemu/500/status/shutdown", "nodes/pve1/qemu"],
        )


class StylesheetTokenTests(SimpleTestCase):
    """Guard the CSS custom properties the stylesheets actually depend on.

    A `var(--x)` naming a property that is never defined is not a no-op: the
    declaration is invalid at computed-value time, so the whole property falls
    back to its initial value. `border: 1px solid var(--undefined)` therefore
    renders as *no border at all*, silently, in every browser and with no
    warning from any linter. That is how `--line-strong` removed the border from
    every secondary button in the application without anyone noticing.
    """

    css_root = Path(settings.BASE_DIR) / "static" / "css"
    template_root = Path(settings.BASE_DIR) / "templates"
    definition_re = re.compile(r"(--[\w-]+)\s*:")
    usage_re = re.compile(r"var\(\s*(--[\w-]+)\s*(,)?")

    def _defined_properties(self) -> set[str]:
        defined: set[str] = set()
        for path in self.css_root.rglob("*.css"):
            defined.update(self.definition_re.findall(path.read_text()))
        # Some properties are set per element in a template (the storage
        # browser's --depth, a tag's --tag-bg/--tag-fg), never in a stylesheet.
        for path in self.template_root.rglob("*.html"):
            defined.update(self.definition_re.findall(path.read_text()))
        return defined

    def test_every_used_custom_property_is_defined(self):
        defined = self._defined_properties()
        undefined = []
        for path in sorted(self.css_root.rglob("*.css")):
            text = path.read_text()
            for match in self.usage_re.finditer(text):
                name = match.group(1)
                if name in defined:
                    continue
                line = text.count("\n", 0, match.start()) + 1
                has_fallback = bool(match.group(2))
                relative = path.relative_to(settings.BASE_DIR)
                undefined.append(f"{relative}:{line} uses {name}{' (fallback only)' if has_fallback else ''}")
        self.assertEqual(
            undefined,
            [],
            "Undefined CSS custom properties — a var() without a definition drops the "
            "whole declaration:\n  " + "\n  ".join(undefined),
        )

    def test_custom_properties_use_kebab_case(self):
        """`--surface_alt` and `--surface-alt` are different properties.

        The underscore spellings silently resolved to their hardcoded fallbacks,
        so those rules never followed the light/dark theme at all.
        """
        offenders = []
        for path in sorted(self.css_root.rglob("*.css")):
            text = path.read_text()
            for match in re.finditer(r"--[\w-]*_[\w-]*", text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(settings.BASE_DIR)}:{line} {match.group(0)}")
        self.assertEqual(offenders, [], "CSS custom properties are kebab-case:\n  " + "\n  ".join(offenders))


class FrontendNavigationSourceTests(SimpleTestCase):
    """Forms must go through soft navigation, not a document reload."""

    js_root = Path(settings.BASE_DIR) / "static" / "js"

    def test_no_native_form_submit_calls(self):
        """`form.submit()` bypasses every submit listener, including ours.

        It is invisible to the soft-navigation interceptor, so it always reloads
        the whole document. Use `requestSubmit()`, which fires a real submit
        event that the interceptor can act on.
        """
        offenders = []
        for path in sorted(self.js_root.rglob("*.js")):
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                code = line.split("//", 1)[0]
                if re.search(r"\.submit\(\s*\)", code):
                    offenders.append(f"{path.relative_to(settings.BASE_DIR)}:{number} {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "Use requestSubmit() so soft navigation sees the submit event:\n  " + "\n  ".join(offenders),
        )

"""Seed the isolated Playwright database with the representative topology.

This is test infrastructure, not an application bootstrap path.  The production
runtime stage copies only ``scripts/worker_healthcheck.py`` and deliberately does
not copy this file.  ``docker-compose.tools.yml`` runs it only after selecting the
throwaway SQLite database and disabling all Proxmox network access.

Module 5's topology projection schema does not exist yet. States it will
eventually own are therefore declared in :data:`CLUSTER_FIXTURES` and mirrored
under the ``details.e2e_fixture`` namespace on today's ``ProxmoxCluster`` rows.
Where typed projections already exist (guest and storage coverage), the seed also
materializes them and checks that they agree with the manifest. As new projections
land, their phases can follow the same rule without changing scenario vocabulary.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Literal

ScenarioName = Literal["representative", "retired-only", "zero"]
TopologyRole = Literal["standalone", "corosync"]
DomainState = Literal["fresh", "partial", "stale", "permission_denied"]

FIXTURE_NAMESPACE = "e2e_fixture"
FIXTURE_TIMESTAMP = "2026-08-10T12:00:00+00:00"
LONG_NODE_NAME = "node-with-a-deliberately-long-name-for-responsive-layout"
THROWAWAY_DATABASE = Path("/tmp/e2e/db.sqlite3")


@dataclass(frozen=True)
class NodeFixture:
    name: str
    online: bool = True
    runtime: DomainState = "fresh"


@dataclass(frozen=True)
class ClusterFixture:
    key: str
    display_name: str
    role: TopologyRole
    nodes: tuple[NodeFixture, ...]
    enabled: bool = True
    quorate: bool | None = None
    qdevice: bool | None = None
    membership: DomainState = "fresh"
    domains: dict[str, DomainState] = field(default_factory=dict)
    quarantined: bool = False
    transition_pending: TopologyRole | None = None
    external_generation_changed: bool = False


CLUSTER_FIXTURES: tuple[ClusterFixture, ...] = (
    ClusterFixture(
        key="e2e",
        display_name="E2E cluster",
        role="corosync",
        nodes=(
            NodeFixture("pve1"),
            NodeFixture("pve2", online=False, runtime="stale"),
            NodeFixture(LONG_NODE_NAME),
        ),
        quorate=True,
        qdevice=False,
        membership="fresh",
        domains={"runtime": "partial", "guests": "partial", "storage": "fresh", "updates": "stale"},
        external_generation_changed=True,
    ),
    # Deliberately shares both node name pve1 and (vm, 100) with e2e.
    ClusterFixture(
        key="standalone-e2e",
        display_name="Standalone E2E host with an intentionally long display name",
        role="standalone",
        nodes=(NodeFixture("pve1"),),
        quorate=None,
        membership="fresh",
        domains={"runtime": "fresh", "guests": "stale", "storage": "stale"},
    ),
    ClusterFixture(
        key="healthy-multi-e2e",
        display_name="Healthy multi-node E2E cluster",
        role="corosync",
        nodes=(NodeFixture("healthy-pve-a"), NodeFixture("healthy-pve-b")),
        quorate=True,
        qdevice=False,
        membership="fresh",
        domains={"runtime": "fresh", "guests": "fresh", "storage": "fresh", "updates": "fresh"},
    ),
    ClusterFixture(
        key="one-node-e2e",
        display_name="One-node corosync E2E cluster",
        role="corosync",
        nodes=(NodeFixture("pve-one"),),
        quorate=True,
        qdevice=False,
    ),
    ClusterFixture(
        key="two-node-e2e",
        display_name="Two-node E2E cluster without QDevice",
        role="corosync",
        nodes=(NodeFixture("pve-a"), NodeFixture("pve-b")),
        quorate=False,
        qdevice=False,
        domains={"runtime": "partial", "ha": "stale"},
    ),
    ClusterFixture(
        key="membership-partial-e2e",
        display_name="Duplicate partial-coverage fixture",
        role="corosync",
        nodes=(NodeFixture("membership-pve-a"), NodeFixture("membership-pve-b")),
        quorate=True,
        membership="partial",
        domains={"runtime": "fresh", "storage": "fresh", "guests": "fresh"},
    ),
    ClusterFixture(
        key="guest-partial-e2e",
        display_name="Guest partial-coverage fixture",
        role="corosync",
        nodes=(NodeFixture("guest-partial-pve"), NodeFixture("guest-unavailable-pve")),
        quorate=True,
        qdevice=False,
        membership="fresh",
        domains={"runtime": "fresh", "storage": "fresh", "guests": "partial"},
    ),
    ClusterFixture(
        key="storage-partial-e2e",
        display_name="Duplicate partial-coverage fixture",
        role="standalone",
        nodes=(NodeFixture("storage-partial-pve"),),
        membership="fresh",
        domains={"runtime": "fresh", "storage": "partial", "guests": "fresh"},
    ),
    ClusterFixture(
        key="quarantined-e2e",
        display_name="Quarantined E2E cluster",
        role="corosync",
        nodes=(NodeFixture("pve-quarantined"),),
        quorate=True,
        quarantined=True,
    ),
    ClusterFixture(
        key="transition-e2e",
        display_name="Standalone becoming clustered E2E",
        role="standalone",
        nodes=(NodeFixture("pve-transition"),),
        quarantined=True,
        transition_pending="corosync",
        membership="stale",
        domains={"runtime": "fresh"},
    ),
    ClusterFixture(
        key="denied-e2e",
        display_name="Permission-denied E2E cluster",
        role="corosync",
        nodes=(NodeFixture("pve-denied", runtime="permission_denied"),),
        membership="permission_denied",
        domains={"runtime": "permission_denied"},
    ),
    ClusterFixture(
        key="unused-e2e",
        display_name="Unused E2E connection",
        role="standalone",
        nodes=(NodeFixture("pve1", online=False, runtime="stale"),),
        enabled=False,
        membership="stale",
        domains={"runtime": "stale"},
    ),
)

SCENARIO_CLUSTER_KEYS: dict[ScenarioName, tuple[str, ...]] = {
    "representative": tuple(fixture.key for fixture in CLUSTER_FIXTURES),
    "retired-only": (),
    "zero": (),
}


def fixture_for(key: str) -> ClusterFixture:
    """Return one named topology fixture without relying on tuple position."""
    try:
        return next(fixture for fixture in CLUSTER_FIXTURES if fixture.key == key)
    except StopIteration as exc:  # pragma: no cover - caller error
        raise KeyError(f"Unknown E2E cluster fixture: {key}") from exc


def fixture_details(fixture: ClusterFixture) -> dict:
    """Stable JSON form stored only in the test fixture namespace."""
    details = asdict(fixture)
    details["fixture_timestamp"] = FIXTURE_TIMESTAMP
    details["membership_generation"] = f"{fixture.key}-membership-g1"
    if fixture.external_generation_changed:
        details["external_generation"] = {
            "rendered": f"{fixture.key}-external-g1",
            "current": f"{fixture.key}-external-g2",
        }
    return {FIXTURE_NAMESPACE: details}


def _configure_django() -> None:
    # Executing ``python scripts/e2e_seed.py`` puts /app/scripts, not /app, at
    # sys.path[0].  Make the repository root explicit just as manage.py does.
    repository_root = str(Path(__file__).resolve().parents[1])
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pve_helper.settings")
    import django

    django.setup()


def _assert_isolated_database() -> None:
    from django.conf import settings

    database = settings.DATABASES["default"]
    if database["ENGINE"] != "django.db.backends.sqlite3":
        raise RuntimeError("The E2E seed refuses to reset a non-SQLite database.")
    if Path(str(database["NAME"])).resolve() != THROWAWAY_DATABASE:
        raise RuntimeError(f"The E2E seed only resets its throwaway database at {THROWAWAY_DATABASE}.")
    if not settings.PVE_TEST_NETWORK_DISABLED:
        raise RuntimeError("The E2E seed requires PVE_TEST_NETWORK_DISABLED=true.")


def _create_cluster(fixture: ClusterFixture):
    from django.utils import timezone

    from core.models import ProxmoxCluster

    return ProxmoxCluster.objects.create(
        key=fixture.key,
        display_name=fixture.display_name,
        enabled=fixture.enabled,
        discovered_name=f"fixture-{fixture.key}",
        discovered_ca_uuid=str(uuid.uuid5(uuid.NAMESPACE_DNS, f"pve-helper-e2e:{fixture.key}")),
        discovered_ca_fingerprint=(fixture.key.upper() + "-FIXTURE-FINGERPRINT")[:200],
        ingestion_quarantined=fixture.quarantined,
        quarantine_reason=(
            "Fixture identity changed during standalone-to-corosync transition."
            if fixture.transition_pending
            else "Fixture endpoint reported a different cluster identity."
            if fixture.quarantined
            else ""
        ),
        quarantined_at=timezone.now() if fixture.quarantined else None,
        details=fixture_details(fixture),
    )


def _create_retired_cluster(*, key: str = "retired-e2e", display_name: str = "Retired E2E cluster"):
    from django.utils import timezone

    from core.models import ProxmoxCluster

    return ProxmoxCluster.objects.create(
        key=key,
        display_name=display_name,
        enabled=False,
        discovered_name=f"Former {display_name}",
        retired_at=timezone.now(),
        retirement_mode=ProxmoxCluster.RetirementMode.FORCED,
        retirement_reason="The isolated E2E site was permanently decommissioned.",
        retired_ca_uuid=(
            "22222222-2222-2222-2222-222222222222"
            if key == "retired-e2e"
            else str(uuid.uuid5(uuid.NAMESPACE_DNS, f"pve-helper-e2e-retired:{key}"))
        ),
        retired_ca_fingerprint=(key.upper() + "-RETIRED-FINGERPRINT")[:200],
        details={FIXTURE_NAMESPACE: {"scenario": "retired-only", "fixture_timestamp": FIXTURE_TIMESTAMP}},
    )


def _create_topology_projection(clusters: dict[str, object], now) -> None:
    """Materialize the membership projection the workspace tree reads.

    The fixtures always described a topology; until 5a2A nothing rendered it, so it
    lived only in the `details` JSON. The Hosts & Clusters tree reads
    `ClusterMembershipState` and `ClusterNodeState`, so a browser test that clicks
    from the sidebar to a node needs the real rows.

    `standalone-e2e` therefore lands in the Hosts group and `e2e` in Clusters, which
    is the split the tree exists to make visible. `e2e` also activates the
    enrollment contract with `pve2` as `safety_only`, so the representative fixture
    carries a hidden node — a leaf that must never appear and a URL that must 404.
    """
    from core.models import (
        ClusterMembershipState,
        ClusterNodeEnrollment,
        ClusterNodeState,
        ClusterProjectionCoverage,
    )

    for fixture in CLUSTER_FIXTURES:
        cluster = clusters[fixture.key]
        generation = 1
        ClusterMembershipState.objects.update_or_create(
            cluster=cluster,
            defaults={
                "membership_generation": generation,
                "member_count": len(fixture.nodes),
                "quorate": bool(fixture.quorate),
                "observed_from": fixture.nodes[0].name if fixture.nodes else "",
                "topology_role": fixture.role,
            },
        )
        ClusterProjectionCoverage.objects.update_or_create(
            cluster=cluster,
            domain=ClusterProjectionCoverage.DOMAIN_MEMBERSHIP,
            node_name=None,
            defaults={
                "generation": generation,
                "based_on_generation": None,
                "complete": fixture.membership == "fresh",
                "attempted_at": now,
                "observed_at": now,
                "error_code": "",
            },
        )
        for index, node in enumerate(fixture.nodes, start=1):
            ClusterNodeState.objects.update_or_create(
                cluster=cluster,
                node_name=node.name,
                defaults={
                    "nodeid": index,
                    "present": True,
                    "online": node.online,
                    "membership_generation": generation,
                },
            )

    # One activated connection with a hidden node, so the representative fixture
    # exercises the publication boundary rather than only the legacy path.
    enrolled = clusters["e2e"]
    for node in fixture_for("e2e").nodes:
        ClusterNodeEnrollment.objects.update_or_create(
            cluster=enrolled,
            node_name=node.name,
            defaults={
                "mode": (
                    ClusterNodeEnrollment.Mode.SAFETY_ONLY
                    if node.name == "pve2"
                    else ClusterNodeEnrollment.Mode.MANAGED
                ),
                "enrolled_at": now,
            },
        )
    enrolled.enrollment_contract_version = 1
    enrolled.enrollment_generation = 1
    enrolled.enrollment_activated_at = now
    enrolled.save(
        update_fields=[
            "enrollment_contract_version",
            "enrollment_generation",
            "enrollment_activated_at",
        ]
    )


def _create_guests(clusters: dict[str, object], scan, now) -> None:
    from core.models import CurrentGuestInventory, CurrentGuestInventoryState

    primary = clusters["e2e"]
    standalone = clusters["standalone-e2e"]

    CurrentGuestInventory.objects.create(
        cluster=primary,
        source_scan=scan,
        node="pve1",
        object_type="vm",
        vmid=100,
        name="e2e-vm-running",
        status="running",
        ha_state="started",
        pool="production",
        config={"tags": "prod", "agent": "1", "cores": 4, "memory": 8192},
        observed_at=now,
        runtime_observed_at=now,
    )
    CurrentGuestInventory.objects.create(
        cluster=primary,
        source_scan=scan,
        node="pve1",
        object_type="vm",
        vmid=101,
        name="e2e-vm-stopped",
        status="stopped",
        config={"cores": 2, "memory": 4096},
        observed_at=now,
        runtime_observed_at=now,
    )
    CurrentGuestInventory.objects.create(
        cluster=primary,
        source_scan=scan,
        node="pve2",
        object_type="vm",
        vmid=102,
        name="e2e-vm-unobserved",
        status="unknown",
        observed_at=now,
        runtime_observed_at=now,
    )
    CurrentGuestInventory.objects.create(
        cluster=primary,
        source_scan=scan,
        node=LONG_NODE_NAME,
        object_type="ct",
        vmid=500,
        name="e2e-container-duplicate-identity-a",
        status="running",
        observed_at=now,
        runtime_observed_at=now,
    )
    # Dense rows are layout evidence, not decorative data.  Twenty rows force the
    # inventory table to own its scroll at both required viewport sizes.
    for offset in range(20):
        CurrentGuestInventory.objects.create(
            cluster=primary,
            source_scan=scan,
            node="pve1" if offset % 2 == 0 else LONG_NODE_NAME,
            object_type="vm" if offset % 3 else "ct",
            vmid=600 + offset,
            name=f"representative-layout-guest-{offset:02d}-with-a-long-name",
            status="running" if offset % 2 == 0 else "stopped",
            observed_at=now,
            runtime_observed_at=now,
        )

    # Both durable components collide across cluster keys on purpose.
    for object_type, vmid, name in (
        ("vm", 100, "standalone-duplicate-vm-100"),
        ("ct", 500, "standalone-duplicate-ct-500"),
    ):
        CurrentGuestInventory.objects.create(
            cluster=standalone,
            source_scan=scan,
            node="pve1",
            object_type=object_type,
            vmid=vmid,
            name=name,
            status="running",
            observed_at=now,
            runtime_observed_at=now,
        )

    for fixture in CLUSTER_FIXTURES:
        state = fixture.domains.get("guests")
        if state is None:
            continue
        attempted = [node.name for node in fixture.nodes]
        succeeded = list(attempted)
        errors: dict[str, list[str]] = {}
        complete = state != "partial"
        refreshed_at = now - timedelta(minutes=30) if state == "stale" else now
        if state == "partial":
            succeeded = [node.name for node in fixture.nodes if node.name != "pve2" and "unavailable" not in node.name]
            errors = {"live_inventory": [f"{node} unavailable" for node in attempted if node not in succeeded]}
        CurrentGuestInventoryState.objects.create(
            cluster=clusters[fixture.key],
            refreshed_at=refreshed_at,
            last_complete_at=refreshed_at if complete else now - timedelta(minutes=30),
            complete=complete,
            endpoints_attempted=attempted,
            endpoints_succeeded=succeeded,
            errors=errors,
            source_scan=scan,
        )


def _create_storage(clusters: dict[str, object], now) -> None:
    from core.models import (
        ClusterStorage,
        ClusterStorageMount,
        ClusterStorageNodeState,
        ProxmoxStorageConsumer,
        StorageCatalogState,
        StorageMount,
    )

    primary = clusters["e2e"]
    catalog_states = {}
    for fixture in CLUSTER_FIXTURES:
        state = fixture.domains.get("storage")
        if state is None:
            continue
        refreshed_at = now - timedelta(minutes=30) if state in {"stale", "partial"} else now
        last_attempt_at = refreshed_at if state == "stale" else now
        complete = state != "partial"
        errors = {fixture.nodes[0].name: "storage coverage response was incomplete"} if state == "partial" else {}
        catalog_states[fixture.key] = StorageCatalogState.objects.create(
            cluster=clusters[fixture.key],
            metadata_generation=uuid.uuid5(uuid.NAMESPACE_DNS, f"pve-helper-e2e-storage:{fixture.key}"),
            metadata_refreshed_at=refreshed_at,
            metadata_last_attempt_at=last_attempt_at,
            metadata_complete=complete,
            metadata_errors=errors,
            volume_refreshed_at=refreshed_at,
            volume_last_attempt_at=last_attempt_at,
            volume_complete=complete,
            volume_errors=errors,
        )

    StorageMount.objects.get_or_create(
        storage_id="e2e-store",
        defaults={"display_name": "E2E Store", "path": "/tmp/e2e-store"},
    )
    shared = ClusterStorage.objects.create(
        cluster=primary,
        storage_id="e2e-nfs",
        storage_type="nfs",
        shared=True,
        present=True,
        content=["images", "iso"],
        config={"server": "nas.e2e.local", "export": "/mnt/tank/vm"},
        observed_metadata_generation=catalog_states["e2e"].metadata_generation,
    )
    local = ClusterStorage.objects.create(
        cluster=primary,
        storage_id="e2e-dir",
        storage_type="dir",
        shared=False,
        present=True,
        content=["images", "rootdir", "vztmpl"],
        config={"path": "/var/lib/vz"},
        observed_metadata_generation=catalog_states["e2e"].metadata_generation,
    )
    api_only = ClusterStorage.objects.create(
        cluster=primary,
        storage_id="e2e-pbs",
        storage_type="pbs",
        shared=True,
        present=True,
        content=["backup"],
        config={"server": "pbs.e2e.invalid", "datastore": "backups"},
        observed_metadata_generation=catalog_states["e2e"].metadata_generation,
    )
    mount = StorageMount.objects.create(
        storage_id="e2e-nfs",
        display_name="E2E shared storage",
        path="/tmp/e2e-nfs",
    )
    ClusterStorageMount.objects.create(cluster_storage=shared, mount=mount, scope="shared")
    ProxmoxStorageConsumer.objects.create(
        storage=mount,
        cluster=primary,
        expected_node_name="pve1",
        last_successful_inventory_scan=now,
        last_gate_status="ok",
    )
    # Stamped with the catalog's own generation, because that is what currency is:
    # the workspace Datastores tab compares these two values and an unstamped row
    # renders as "not current" -- a fixture that makes every row look degraded is a
    # fixture that hides a real degradation.
    generation = catalog_states["e2e"].metadata_generation
    ClusterStorageNodeState.objects.create(
        cluster_storage=shared,
        node="pve1",
        present=True,
        active=True,
        enabled=True,
        total_bytes=2 * 1024**4,
        used_bytes=760 * 1024**3,
        available_bytes=2 * 1024**4 - 760 * 1024**3,
        observed_metadata_generation=generation,
        last_seen_at=now,
    )
    ClusterStorageNodeState.objects.create(
        cluster_storage=local,
        node="pve1",
        present=True,
        active=True,
        enabled=True,
        total_bytes=100 * 1024**3,
        used_bytes=41 * 1024**3,
        available_bytes=59 * 1024**3,
        observed_metadata_generation=generation,
        last_seen_at=now,
    )
    # Left unstamped on purpose: a node that never answered has no generation to
    # carry, so this is the degraded row the tab must render as unknown rather than
    # as absent or as a blank.
    ClusterStorageNodeState.objects.create(
        cluster_storage=api_only,
        node="pve1",
        present=True,
        active=False,
        enabled=True,
        unreachable=True,
    )


def _create_connection_fixture() -> object:
    from core.models import ProxmoxEndpoint
    from core.services.cluster_credentials import set_cluster_credential
    from core.services.cluster_trust import approve_cluster_transport

    cluster = _create_cluster(fixture_for("unused-e2e"))
    ProxmoxEndpoint.objects.create(cluster=cluster, name="pve1", url="https://pve1.unused-e2e.test:8006/")
    approve_cluster_transport(cluster, mode="public")
    set_cluster_credential(cluster, token_id="e2e@pve!token", token_secret="e2e-not-a-secret")
    return cluster


def _create_audit_history(primary, retired, transition, now) -> None:
    from core.models import AuditEvent

    AuditEvent.objects.create(
        username="e2e",
        action="tag.bulk_operation",
        object_type="tag",
        object_id="old-tag",
        outcome="failed",
        details={
            "cluster_key": "e2e",
            "operation": "delete",
            "source_tag": "old-tag",
            "targets": [{"cluster_key": "e2e", "node": "pve1", "object_type": "vm", "vmid": 100}],
            "failed": [{"vmid": 100, "reason": "locked"}],
            "retryable": True,
        },
    )
    AuditEvent.objects.create(
        username="e2e",
        action="cluster.force_retired",
        object_type="cluster",
        object_id=retired.key,
        outcome="success",
        cluster=retired,
        cluster_key_snapshot=retired.key,
        details={
            "display_name": retired.display_name,
            "cluster_key": retired.key,
            "retirement_mode": "forced",
            "retirement_reason": retired.retirement_reason,
            "identity_verification": "skipped",
            "endpoint_count": 2,
            "cleanup": {"schedules_deleted": 1, "current_guests_deleted": 2},
        },
    )
    AuditEvent.objects.create(
        username="e2e",
        action="cluster.topology_transition_detected",
        object_type="cluster",
        object_id=transition.key,
        outcome="warning",
        cluster=transition,
        cluster_key_snapshot=transition.key,
        details={
            "cluster_key": transition.key,
            "registered_role": "standalone",
            "pending_role": "corosync",
            "history_preserved": True,
            "operator_confirmation_required": True,
        },
    )
    AuditEvent.objects.create(
        username="e2e",
        action="file.bulk_operation",
        object_type="storage",
        object_id="e2e-store",
        outcome="warning",
        module="storage",
        cluster=primary,
        cluster_key_snapshot="e2e",
        storage_id="e2e-store",
        details={
            "verb": "moved to trash",
            "operation": "trash",
            "storage_id": "e2e-store",
            "storage_name": "E2E Store",
            "summary": "1 of 2 files moved to trash",
            "total": 2,
            "question": True,
            "succeeded": ["dump/ok.vma.zst"],
            "failed": [{"path": "images/100/vm-100-disk-0.qcow2", "error": "Permission denied."}],
            "skipped": [],
            "retry": {
                "url": "/storage/e2e-store/trash-file/",
                "paths": ["images/100/vm-100-disk-0.qcow2"],
            },
            "observed_at": now.isoformat(),
        },
    )


def seed_database(*, scenario: ScenarioName = "representative", reset: bool = True) -> None:
    """Materialize one deterministic scenario into the configured database."""
    from django.core.management import call_command
    from django.db import transaction
    from django.db.models import Count
    from django.utils import timezone

    from core.models import AuditEvent, CurrentGuestInventory, ProxmoxCluster, ScanRun

    if reset:
        _assert_isolated_database()
        call_command("flush", interactive=False, verbosity=0)

    if scenario == "zero":
        return

    with transaction.atomic():
        retired = _create_retired_cluster()
        if scenario == "retired-only":
            AuditEvent.objects.create(
                username="e2e",
                action="cluster.force_retired",
                object_type="cluster",
                object_id=retired.key,
                outcome="success",
                cluster=retired,
                cluster_key_snapshot=retired.key,
                details={"cluster_key": retired.key, "scenario": "retired-only"},
            )
            return

        now = timezone.now()
        clusters = {
            fixture.key: _create_cluster(fixture) for fixture in CLUSTER_FIXTURES if fixture.key != "unused-e2e"
        }
        clusters["unused-e2e"] = _create_connection_fixture()
        scan = ScanRun.objects.create(status=ScanRun.Status.COMPLETED, progress_message="representative e2e seed")
        _create_topology_projection(clusters, now)
        _create_guests(clusters, scan, now)
        _create_storage(clusters, now)
        _create_audit_history(clusters["e2e"], retired, clusters["transition-e2e"], now)

        # Guard the two identity collisions at the materialization boundary.  A
        # later schema edit that accidentally makes either impossible fails while
        # starting e2e-web rather than silently weakening every browser test.
        node_counts = Counter(node.name for fixture in CLUSTER_FIXTURES for node in fixture.nodes)
        duplicate_nodes = {node for node, count in node_counts.items() if count > 1}
        if "pve1" not in duplicate_nodes:
            raise AssertionError("Representative fixture lost its duplicate node name.")
        duplicates = (
            CurrentGuestInventory.objects.values("object_type", "vmid")
            .order_by()
            .annotate(cluster_count=Count("cluster", distinct=True))
            .filter(cluster_count__gt=1)
        )
        if not duplicates.filter(object_type="vm", vmid=100).exists():
            raise AssertionError("Representative fixture lost its duplicate (type, vmid).")
        if ProxmoxCluster.objects.filter(retired_at__isnull=True).count() < 2:
            raise AssertionError("Representative fixture requires at least two managed cluster domains.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=tuple(SCENARIO_CLUSTER_KEYS),
        default=os.environ.get("E2E_SEED_SCENARIO", "representative"),
    )
    args = parser.parse_args()
    _configure_django()
    seed_database(scenario=args.scenario)


if __name__ == "__main__":
    main()

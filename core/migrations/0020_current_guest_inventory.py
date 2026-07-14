from django.db import migrations, models
import django.db.models.deletion


def backfill_current_guests(apps, schema_editor):
    ScanRun = apps.get_model("core", "ScanRun")
    ProxmoxInventory = apps.get_model("core", "ProxmoxInventory")
    ProxmoxEndpoint = apps.get_model("core", "ProxmoxEndpoint")
    CurrentGuestInventory = apps.get_model("core", "CurrentGuestInventory")
    CurrentGuestInventoryState = apps.get_model("core", "CurrentGuestInventoryState")

    scan_ids = list(
        ProxmoxInventory.objects.filter(object_type__in=["vm", "ct"], vmid__isnull=False)
        .values_list("scan_run_id", flat=True)
        .distinct()
    )
    scans = list(ScanRun.objects.filter(pk__in=scan_ids).order_by("created_at", "pk"))
    if not scans:
        return
    endpoints = list(ProxmoxEndpoint.objects.all())
    by_node = {}
    for endpoint in endpoints:
        by_node.setdefault(endpoint.name, []).append(endpoint)
        detail_node = str((endpoint.details or {}).get("node") or "")
        if detail_node and detail_node != endpoint.name:
            by_node.setdefault(detail_node, []).append(endpoint)
    # Replay scan coverage so a newer partial scan cannot erase guests from an
    # endpoint that failed. A complete scan is an authoritative reset; partial
    # scans only retire rows for endpoint/node names that succeeded.
    current = {}
    for scan in scans:
        rows = list(
            ProxmoxInventory.objects.filter(scan_run=scan, object_type__in=["vm", "ct"])
            .exclude(vmid__isnull=True)
            .order_by("id")
        )
        attempted = set(scan.endpoints_attempted or [])
        succeeded = set(scan.endpoints_succeeded or [])
        complete = bool(attempted) and attempted == succeeded
        observed_keys = {(guest.object_type, guest.vmid) for guest in rows}
        if complete:
            current.clear()
        else:
            for key, guest in list(current.items()):
                if guest.node in succeeded and key not in observed_keys:
                    del current[key]
        for guest in rows:
            current[(guest.object_type, guest.vmid)] = guest

    for guest in current.values():
        scan = guest.scan_run
        observed_at = scan.proxmox_inventory_at or scan.finished_at or scan.created_at
        matches = by_node.get(guest.node, [])
        CurrentGuestInventory.objects.create(
            source_endpoint=matches[0] if len(matches) == 1 else None,
            source_scan=scan,
            node=guest.node,
            object_type=guest.object_type,
            vmid=guest.vmid,
            name=guest.name,
            status=guest.status,
            config=guest.config,
            config_complete=True,
            disk_references=guest.disk_references,
            observed_at=observed_at,
        )
    scan = scans[-1]
    observed_at = scan.proxmox_inventory_at or scan.finished_at or scan.created_at
    attempted = list(scan.endpoints_attempted or [])
    succeeded = list(scan.endpoints_succeeded or [])
    complete = bool(attempted) and set(attempted) == set(succeeded)
    CurrentGuestInventoryState.objects.create(
        id=1,
        refreshed_at=observed_at,
        last_complete_at=observed_at if complete else None,
        source_scan=scan,
        complete=complete,
        endpoints_attempted=attempted,
        endpoints_succeeded=succeeded,
        errors=scan.error_details or {},
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0019_remove_derived_tags")]

    operations = [
        migrations.CreateModel(
            name="CurrentGuestInventoryState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("refreshed_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("last_complete_at", models.DateTimeField(blank=True, null=True)),
                ("complete", models.BooleanField(default=False)),
                ("endpoints_attempted", models.JSONField(blank=True, default=list)),
                ("endpoints_succeeded", models.JSONField(blank=True, default=list)),
                ("errors", models.JSONField(blank=True, default=dict)),
                ("source_scan", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="current_inventory_states", to="core.scanrun")),
            ],
        ),
        migrations.CreateModel(
            name="CurrentGuestInventory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("node", models.CharField(db_index=True, max_length=120)),
                ("object_type", models.CharField(choices=[("vm", "VM"), ("ct", "Container")], max_length=30)),
                ("vmid", models.PositiveIntegerField()),
                ("name", models.CharField(blank=True, max_length=255)),
                ("status", models.CharField(blank=True, max_length=80)),
                ("config", models.JSONField(blank=True, default=dict)),
                ("config_complete", models.BooleanField(default=True)),
                ("disk_references", models.JSONField(blank=True, default=list)),
                ("observed_at", models.DateTimeField(db_index=True)),
                ("source_endpoint", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="current_guests", to="core.proxmoxendpoint")),
                ("source_scan", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="current_guests", to="core.scanrun")),
            ],
            options={
                "ordering": ["node", "object_type", "vmid"],
                "indexes": [models.Index(fields=["source_endpoint", "object_type"], name="core_curg_endpoint_type_idx"), models.Index(fields=["object_type", "vmid"], name="core_curguest_type_vmid_idx")],
                "constraints": [models.UniqueConstraint(fields=("object_type", "vmid"), name="unique_current_guest_identity")],
            },
        ),
        migrations.RunPython(backfill_current_guests, migrations.RunPython.noop),
    ]

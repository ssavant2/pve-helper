# Generated for pve-helper phase 0 skeleton.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AuditEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("timestamp", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("username", models.CharField(blank=True, max_length=255)),
                ("source_ip", models.GenericIPAddressField(blank=True, null=True)),
                ("action", models.CharField(max_length=120)),
                ("object_type", models.CharField(blank=True, max_length=120)),
                ("object_id", models.CharField(blank=True, max_length=512)),
                ("outcome", models.CharField(default="success", max_length=60)),
                ("details", models.JSONField(blank=True, default=dict)),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="pve_helper_audit_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-timestamp"],
            },
        ),
        migrations.CreateModel(
            name="ProxmoxEndpoint",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=120, unique=True)),
                ("url", models.URLField()),
                ("enabled", models.BooleanField(default=True)),
                ("last_health_status", models.CharField(blank=True, max_length=60)),
                ("last_successful_scan", models.DateTimeField(blank=True, null=True)),
                ("details", models.JSONField(blank=True, default=dict)),
            ],
            options={
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="ScanRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("queued_task_id", models.CharField(blank=True, max_length=120)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("running", "Running"),
                            ("completed", "Completed"),
                            ("failed", "Failed"),
                            ("cancelled", "Cancelled"),
                        ],
                        default="queued",
                        max_length=30,
                    ),
                ),
                ("progress_message", models.CharField(blank=True, max_length=255)),
                ("endpoints_attempted", models.JSONField(blank=True, default=list)),
                ("endpoints_succeeded", models.JSONField(blank=True, default=list)),
                ("summary_counts", models.JSONField(blank=True, default=dict)),
                ("error_details", models.JSONField(blank=True, default=dict)),
                ("storage_gate_status", models.JSONField(blank=True, default=dict)),
                ("filesystem_scan_at", models.DateTimeField(blank=True, null=True)),
                ("proxmox_inventory_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="StorageMount",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("storage_id", models.CharField(max_length=120, unique=True)),
                ("display_name", models.CharField(max_length=160)),
                ("export", models.CharField(blank=True, max_length=512)),
                ("path", models.CharField(max_length=512)),
                ("trash_path", models.CharField(blank=True, max_length=512)),
                ("expected_consumers", models.JSONField(blank=True, default=list)),
                ("enabled", models.BooleanField(default=True)),
            ],
            options={
                "ordering": ["display_name"],
            },
        ),
        migrations.CreateModel(
            name="ProxmoxStorageConsumer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("expected_node_name", models.CharField(max_length=120)),
                ("last_successful_inventory_scan", models.DateTimeField(blank=True, null=True)),
                ("last_gate_status", models.CharField(blank=True, max_length=80)),
                (
                    "storage",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="consumer_statuses",
                        to="core.storagemount",
                    ),
                ),
            ],
            options={
                "ordering": ["storage__display_name", "expected_node_name"],
            },
        ),
        migrations.CreateModel(
            name="ProxmoxInventory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("node", models.CharField(db_index=True, max_length=120)),
                (
                    "object_type",
                    models.CharField(
                        choices=[("vm", "VM"), ("ct", "Container"), ("storage", "Storage"), ("node", "Node")],
                        max_length=30,
                    ),
                ),
                ("vmid", models.IntegerField(blank=True, db_index=True, null=True)),
                ("name", models.CharField(blank=True, max_length=255)),
                ("status", models.CharField(blank=True, max_length=80)),
                ("config", models.JSONField(blank=True, default=dict)),
                ("disk_references", models.JSONField(blank=True, default=list)),
                (
                    "scan_run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="proxmox_objects",
                        to="core.scanrun",
                    ),
                ),
            ],
            options={
                "ordering": ["node", "object_type", "vmid"],
            },
        ),
        migrations.CreateModel(
            name="FileInventory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("path", models.CharField(max_length=1024)),
                ("derived_volid", models.CharField(blank=True, max_length=512)),
                ("content_category", models.CharField(blank=True, max_length=80)),
                (
                    "entry_type",
                    models.CharField(
                        choices=[("file", "File"), ("directory", "Directory"), ("symlink", "Symlink"), ("other", "Other")],
                        default="file",
                        max_length=30,
                    ),
                ),
                ("size_bytes", models.BigIntegerField(blank=True, null=True)),
                ("modified_at", models.DateTimeField(blank=True, null=True)),
                (
                    "classification",
                    models.CharField(
                        choices=[
                            ("referenced", "Referenced"),
                            ("likely_orphan", "Likely orphan"),
                            ("unknown", "Unknown"),
                            ("classification_blocked", "Classification blocked"),
                            ("trash", "Trash"),
                        ],
                        default="unknown",
                        max_length=40,
                    ),
                ),
                ("classification_reason", models.TextField(blank=True)),
                ("matched_object", models.JSONField(blank=True, default=dict)),
                ("evidence", models.JSONField(blank=True, default=dict)),
                (
                    "scan_run",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="files", to="core.scanrun"),
                ),
                (
                    "storage",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="files", to="core.storagemount"),
                ),
            ],
            options={
                "ordering": ["storage__display_name", "path"],
            },
        ),
        migrations.CreateModel(
            name="TrashItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("original_path", models.CharField(max_length=1024)),
                ("trash_path", models.CharField(max_length=1024)),
                ("moved_at", models.DateTimeField(blank=True, null=True)),
                (
                    "restore_status",
                    models.CharField(
                        choices=[("trashed", "Trashed"), ("restored", "Restored"), ("purged", "Purged"), ("failed", "Failed")],
                        default="trashed",
                        max_length=40,
                    ),
                ),
                ("metadata", models.JSONField(blank=True, default=dict)),
                (
                    "moved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="pve_helper_trash_items",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="auditevent",
            index=models.Index(fields=["action", "outcome"], name="core_audite_action_112982_idx"),
        ),
        migrations.AddIndex(
            model_name="auditevent",
            index=models.Index(fields=["object_type", "object_id"], name="core_audite_object__bbabc4_idx"),
        ),
        migrations.AddIndex(
            model_name="fileinventory",
            index=models.Index(fields=["storage", "path"], name="core_filein_storage_fbf742_idx"),
        ),
        migrations.AddIndex(
            model_name="fileinventory",
            index=models.Index(fields=["storage", "derived_volid"], name="core_filein_storage_e0df46_idx"),
        ),
        migrations.AddIndex(
            model_name="fileinventory",
            index=models.Index(fields=["classification"], name="core_filein_classif_484f03_idx"),
        ),
        migrations.AddIndex(
            model_name="fileinventory",
            index=models.Index(fields=["content_category"], name="core_filein_content_9ef441_idx"),
        ),
        migrations.AddConstraint(
            model_name="fileinventory",
            constraint=models.UniqueConstraint(
                fields=("scan_run", "storage", "path"),
                name="unique_file_inventory_per_scan_storage_path",
            ),
        ),
        migrations.AddIndex(
            model_name="proxmoxinventory",
            index=models.Index(fields=["scan_run", "node"], name="core_proxmo_scan_ru_7d6c24_idx"),
        ),
        migrations.AddIndex(
            model_name="proxmoxinventory",
            index=models.Index(fields=["object_type", "vmid"], name="core_proxmo_object__1fad14_idx"),
        ),
        migrations.AddConstraint(
            model_name="proxmoxstorageconsumer",
            constraint=models.UniqueConstraint(
                fields=("storage", "expected_node_name"),
                name="unique_storage_expected_consumer",
            ),
        ),
    ]

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("core", "0012_storagespacesnapshot_api_storage_id_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="ConsoleSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("target_type", models.CharField(choices=[("vm", "VM"), ("ct", "Container")], max_length=20)),
                ("target_vmid", models.PositiveIntegerField()),
                ("target_node", models.CharField(blank=True, max_length=120)),
                ("target_name_snapshot", models.CharField(blank=True, max_length=255)),
                ("username", models.CharField(blank=True, max_length=255)),
                ("source_ip", models.GenericIPAddressField(blank=True, null=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                ("connected_at", models.DateTimeField(blank=True, null=True)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("connecting", "Connecting"),
                            ("connected", "Connected"),
                            ("closed", "Closed"),
                            ("failed", "Failed"),
                            ("expired", "Expired"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=30,
                    ),
                ),
                ("proxmox_endpoint", models.URLField(blank=True)),
                ("proxmox_node", models.CharField(blank=True, max_length=120)),
                ("proxmox_upid", models.CharField(blank=True, max_length=255)),
                ("proxmox_port", models.CharField(blank=True, max_length=20)),
                ("proxmox_ticket", models.TextField(blank=True)),
                ("proxmox_password", models.CharField(blank=True, max_length=255)),
                ("close_reason", models.CharField(blank=True, max_length=255)),
                ("error", models.TextField(blank=True)),
                ("details", models.JSONField(blank=True, default=dict)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="pve_helper_console_sessions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["target_type", "target_vmid"], name="core_console_target_idx"),
                    models.Index(fields=["status", "expires_at"], name="core_console_status_exp_idx"),
                ],
            },
        ),
    ]

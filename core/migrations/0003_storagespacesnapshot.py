from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0002_scanrun_target_storage"),
    ]

    operations = [
        migrations.CreateModel(
            name="StorageSpaceSnapshot",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("recorded_at", models.DateTimeField()),
                ("total_bytes", models.BigIntegerField()),
                ("available_bytes", models.BigIntegerField()),
                ("used_bytes", models.BigIntegerField()),
                (
                    "scan_run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="space_snapshots",
                        to="core.scanrun",
                    ),
                ),
                (
                    "storage",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="space_snapshots",
                        to="core.storagemount",
                    ),
                ),
            ],
            options={
                "ordering": ["-recorded_at"],
                "indexes": [
                    models.Index(
                        fields=["storage", "recorded_at"],
                        name="core_storag_storage_idx",
                    ),
                ],
            },
        ),
    ]

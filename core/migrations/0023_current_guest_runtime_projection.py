from django.db import migrations, models


def backfill_observation_times(apps, schema_editor):
    CurrentGuestInventory = apps.get_model("core", "CurrentGuestInventory")
    for guest in CurrentGuestInventory.objects.all().iterator():
        guest.runtime_observed_at = guest.observed_at
        guest.config_observed_at = guest.observed_at if guest.config_complete else None
        guest.save(update_fields=["runtime_observed_at", "config_observed_at"])


class Migration(migrations.Migration):
    dependencies = [("core", "0022_remove_redundant_oidcidentity_indexes")]

    operations = [
        migrations.AddField(
            model_name="currentguestinventory",
            name="config_observed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="currentguestinventory",
            name="cpu_usage",
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name="currentguestinventory",
            name="disk_max_bytes",
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="currentguestinventory",
            name="disk_used_bytes",
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="currentguestinventory",
            name="memory_max_bytes",
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="currentguestinventory",
            name="memory_used_bytes",
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="currentguestinventory",
            name="runtime_lock",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name="currentguestinventory",
            name="runtime_observed_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="currentguestinventory",
            name="uptime_seconds",
            field=models.BigIntegerField(default=0),
        ),
        migrations.RunPython(backfill_observation_times, migrations.RunPython.noop),
    ]

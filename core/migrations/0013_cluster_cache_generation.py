from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0012_backfill_read_model_cluster"),
    ]

    operations = [
        migrations.AddField(
            model_name="proxmoxcluster",
            name="cache_generation",
            field=models.PositiveBigIntegerField(default=1),
        ),
    ]

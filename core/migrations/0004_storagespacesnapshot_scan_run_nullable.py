from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0003_storagespacesnapshot"),
    ]

    operations = [
        migrations.AlterField(
            model_name="storagespacesnapshot",
            name="scan_run",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="space_snapshots",
                to="core.scanrun",
            ),
        ),
    ]

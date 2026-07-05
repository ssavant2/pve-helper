import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0011_auditevent_module"),
    ]

    operations = [
        migrations.AddField(
            model_name="storagespacesnapshot",
            name="api_storage_id",
            field=models.CharField(blank=True, default="", max_length=120),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="storagespacesnapshot",
            name="node",
            field=models.CharField(blank=True, default="", max_length=120),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name="storagespacesnapshot",
            name="storage",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="space_snapshots",
                to="core.storagemount",
            ),
        ),
        migrations.AddIndex(
            model_name="storagespacesnapshot",
            index=models.Index(
                fields=["node", "api_storage_id", "recorded_at"],
                name="core_storag_node_3cc367_idx",
            ),
        ),
    ]

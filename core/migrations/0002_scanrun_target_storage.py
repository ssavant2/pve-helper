from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="scanrun",
            name="target_label",
            field=models.CharField(blank=True, max_length=160),
        ),
        migrations.AddField(
            model_name="scanrun",
            name="target_storage",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="scan_runs",
                to="core.storagemount",
            ),
        ),
    ]

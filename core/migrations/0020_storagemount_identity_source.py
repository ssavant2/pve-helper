from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0019_clusterstoragevolumecoverage"),
    ]

    operations = [
        migrations.AddField(
            model_name="storagemount",
            name="identity_source",
            field=models.CharField(
                choices=[
                    ("derived", "Derived from the Proxmox definition"),
                    ("manual", "Entered by an operator"),
                ],
                default="manual",
                max_length=16,
            ),
        ),
    ]

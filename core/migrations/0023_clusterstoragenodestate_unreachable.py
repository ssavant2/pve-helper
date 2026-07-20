from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0022_canonical_trash_directory")]

    operations = [
        migrations.AddField(
            model_name="clusterstoragenodestate",
            name="unreachable",
            field=models.BooleanField(default=False),
        ),
    ]

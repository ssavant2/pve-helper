from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("core", "0018_derivedtagstyle")]

    operations = [
        migrations.RemoveField(model_name="proxmoxinventory", name="derived_type"),
        migrations.DeleteModel(name="DerivedTagStyle"),
    ]

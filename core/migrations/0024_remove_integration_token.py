from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("core", "0023_current_guest_runtime_projection")]

    operations = [
        migrations.DeleteModel(name="IntegrationToken"),
    ]

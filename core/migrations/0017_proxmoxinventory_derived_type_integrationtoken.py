from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0016_alter_fileinventory_classification")]

    operations = [
        migrations.AddField(
            model_name="proxmoxinventory",
            name="derived_type",
            field=models.CharField(blank=True, db_index=True, max_length=40),
        ),
        migrations.CreateModel(
            name="IntegrationToken",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("token_id", models.CharField(max_length=32, unique=True)),
                ("name", models.CharField(max_length=120)),
                ("secret_hash", models.CharField(max_length=255)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ["name", "token_id"]},
        ),
    ]

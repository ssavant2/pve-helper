from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0017_proxmoxinventory_derived_type_integrationtoken")]

    operations = [
        migrations.CreateModel(
            name="DerivedTagStyle",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("tag", models.CharField(max_length=40, unique=True)),
                ("background", models.CharField(max_length=6)),
                ("foreground", models.CharField(max_length=6)),
            ],
            options={"ordering": ["tag"]},
        ),
    ]

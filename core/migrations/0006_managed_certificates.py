import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0005_logforwardertransporttrust"),
    ]

    operations = [
        migrations.CreateModel(
            name="ManagedCertificate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "usage",
                    models.CharField(
                        choices=[("server", "HTTPS server certificate"), ("authority", "Trusted certificate authority")],
                        max_length=12,
                    ),
                ),
                ("label", models.CharField(max_length=150)),
                ("certificate_pem", models.TextField()),
                ("chain_pem", models.TextField(blank=True)),
                ("private_key_sealed", models.TextField(blank=True)),
                ("subject", models.CharField(blank=True, max_length=500)),
                ("issuer", models.CharField(blank=True, max_length=500)),
                ("serial_number", models.CharField(blank=True, max_length=80)),
                ("sha256_fingerprint", models.CharField(max_length=64)),
                ("subject_alt_names", models.JSONField(blank=True, default=list)),
                ("not_before", models.DateTimeField(blank=True, null=True)),
                ("not_after", models.DateTimeField(blank=True, null=True)),
                ("is_certificate_authority", models.BooleanField(default=False)),
                ("source_filename", models.CharField(blank=True, max_length=255)),
                ("uploaded_by", models.CharField(blank=True, max_length=150)),
            ],
            options={
                "ordering": ["usage", "label"],
            },
        ),
        migrations.AddConstraint(
            model_name="managedcertificate",
            constraint=models.UniqueConstraint(
                fields=("usage", "sha256_fingerprint"), name="managed_certificate_unique"
            ),
        ),
        migrations.CreateModel(
            name="CertificateSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("https_enabled", models.BooleanField(default=False)),
                ("expiry_warning_enabled", models.BooleanField(default=True)),
                ("expiry_warning_days", models.PositiveSmallIntegerField(default=7)),
                (
                    "active_certificate",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="active_for_settings",
                        to="core.managedcertificate",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
    ]

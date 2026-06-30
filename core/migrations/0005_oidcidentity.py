from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0004_storagespacesnapshot_scan_run_nullable"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="OidcIdentity",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("issuer", models.CharField(max_length=512)),
                ("subject", models.CharField(max_length=255)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pve_helper_oidc_identities",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["issuer", "subject"],
                "indexes": [
                    models.Index(fields=["user"], name="core_oidcid_user_id_idx"),
                    models.Index(fields=["issuer", "subject"], name="core_oidcid_issuer_subject_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("issuer", "subject"),
                        name="unique_oidc_identity_subject",
                    ),
                ],
            },
        ),
    ]

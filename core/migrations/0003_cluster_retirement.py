import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("core", "0002_log_forwarding"),
    ]

    operations = [
        # The CA UUID uniqueness is narrowed to live rows before the new columns
        # land, so a retired tombstone that copied its identity into the columns
        # below never keeps the physical cluster claimed.
        migrations.RemoveConstraint(
            model_name="proxmoxcluster",
            name="unique_nonblank_cluster_ca_uuid",
        ),
        migrations.AddField(
            model_name="proxmoxcluster",
            name="retired_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="proxmoxcluster",
            name="retired_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="proxmoxcluster",
            name="retirement_mode",
            field=models.CharField(
                blank=True,
                choices=[("verified", "Verified"), ("forced", "Forced")],
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="proxmoxcluster",
            name="retirement_reason",
            field=models.CharField(blank=True, max_length=1000),
        ),
        migrations.AddField(
            model_name="proxmoxcluster",
            name="retired_ca_uuid",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="proxmoxcluster",
            name="retired_ca_fingerprint",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="proxmoxcluster",
            name="lifecycle_generation",
            field=models.PositiveBigIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="proxmoxcluster",
            name="operational_footprint_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="proxmoxcluster",
            name="operational_footprint_reason",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddConstraint(
            model_name="proxmoxcluster",
            constraint=models.UniqueConstraint(
                condition=models.Q(("discovered_ca_uuid", ""), _negated=True) & models.Q(("retired_at__isnull", True)),
                fields=("discovered_ca_uuid",),
                name="unique_nonblank_cluster_ca_uuid",
            ),
        ),
        migrations.AddConstraint(
            model_name="proxmoxcluster",
            constraint=models.CheckConstraint(
                condition=models.Q(("retired_at__isnull", True))
                | (models.Q(("enabled", False)) & models.Q(("retirement_mode", ""), _negated=True)),
                name="retired_cluster_is_disabled_and_moded",
            ),
        ),
        migrations.AddConstraint(
            model_name="proxmoxcluster",
            constraint=models.CheckConstraint(
                condition=models.Q(("retired_at__isnull", False))
                | (
                    models.Q(("retirement_mode", ""))
                    & models.Q(("retirement_reason", ""))
                    & models.Q(("retired_by__isnull", True))
                ),
                name="active_cluster_has_no_retirement_metadata",
            ),
        ),
    ]

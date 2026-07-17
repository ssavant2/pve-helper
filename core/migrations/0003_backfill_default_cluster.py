"""Seed the `default` cluster for installations that were configured before cluster identity.

An installation that already has endpoints or storage is by definition already
bootstrapped: the environment has had its say, and re-importing it here would be the
env-ownership behaviour this phase removes. Such an installation gets the `default`
cluster, its endpoints attached, and a completion marker recorded.

A fresh, empty installation is left unbootstrapped on purpose, so the first
`ensure_bootstrap()` call performs the real environment import under its lock.
"""

from django.db import migrations


DEFAULT_CLUSTER_KEY = "default"
DEFAULT_CLUSTER_DISPLAY_NAME = "Default cluster"
SINGLETON_PK = 1


def backfill_default_cluster(apps, schema_editor):
    ProxmoxCluster = apps.get_model("core", "ProxmoxCluster")
    ProxmoxEndpoint = apps.get_model("core", "ProxmoxEndpoint")
    StorageMount = apps.get_model("core", "StorageMount")
    RuntimeConfigurationState = apps.get_model("core", "RuntimeConfigurationState")

    already_configured = ProxmoxEndpoint.objects.exists() or StorageMount.objects.exists()
    if not already_configured:
        return

    cluster, _ = ProxmoxCluster.objects.get_or_create(
        key=DEFAULT_CLUSTER_KEY,
        defaults={"display_name": DEFAULT_CLUSTER_DISPLAY_NAME, "enabled": True},
    )
    ProxmoxEndpoint.objects.filter(cluster__isnull=True).update(cluster=cluster)

    # No env fingerprint is recorded: this installation's configuration was never
    # imported under the fingerprinted contract, and claiming otherwise would assert
    # provenance we cannot prove.
    RuntimeConfigurationState.objects.update_or_create(
        pk=SINGLETON_PK,
        defaults={
            "bootstrap_completed": True,
            "bootstrap_completed_at": None,
            "bootstrap_fingerprint": "",
            "identity_contract_version": 0,
            "details": {"bootstrap_source": "migration_backfill"},
        },
    )


def unbackfill_default_cluster(apps, schema_editor):
    ProxmoxCluster = apps.get_model("core", "ProxmoxCluster")
    ProxmoxEndpoint = apps.get_model("core", "ProxmoxEndpoint")
    RuntimeConfigurationState = apps.get_model("core", "RuntimeConfigurationState")

    ProxmoxEndpoint.objects.filter(cluster__key=DEFAULT_CLUSTER_KEY).update(cluster=None)
    ProxmoxCluster.objects.filter(key=DEFAULT_CLUSTER_KEY).delete()
    RuntimeConfigurationState.objects.filter(pk=SINGLETON_PK).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0002_cluster_identity_foundation"),
    ]

    operations = [
        migrations.RunPython(backfill_default_cluster, unbackfill_default_cluster),
    ]

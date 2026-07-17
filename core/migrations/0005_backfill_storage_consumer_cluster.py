"""Attach existing storage consumers to the sole cluster.

Consumer identity becomes (storage, cluster, node). Existing rows predate cluster
identity and belong to the one cluster the installation has; leaving them
unattributed would block their storage gates, since the gate treats an
unattributed consumer as uncovered rather than matching it by bare node name.
"""

from django.db import migrations


def backfill_consumer_cluster(apps, schema_editor):
    ProxmoxCluster = apps.get_model("core", "ProxmoxCluster")
    ProxmoxStorageConsumer = apps.get_model("core", "ProxmoxStorageConsumer")

    if not ProxmoxStorageConsumer.objects.filter(cluster__isnull=True).exists():
        return

    clusters = list(ProxmoxCluster.objects.order_by("key")[:2])
    if len(clusters) != 1:
        # Backfilling would have to guess which cluster owns each consumer, and a
        # wrong guess is exactly the cross-cluster gate defect this phase fixes.
        raise RuntimeError(
            "Cannot backfill storage consumers: expected exactly one cluster, found "
            f"{ProxmoxCluster.objects.count()}. Attribute them explicitly before migrating."
        )

    ProxmoxStorageConsumer.objects.filter(cluster__isnull=True).update(cluster=clusters[0])


def unbackfill_consumer_cluster(apps, schema_editor):
    ProxmoxStorageConsumer = apps.get_model("core", "ProxmoxStorageConsumer")
    ProxmoxStorageConsumer.objects.update(cluster=None)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0004_storage_gate_cluster_identity"),
    ]

    operations = [
        migrations.RunPython(backfill_consumer_cluster, unbackfill_consumer_cluster),
    ]

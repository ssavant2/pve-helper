"""Attribute pre-existing read-model rows to their cluster.

Current guests are attributed from their source endpoint where known, since that is
authoritative. Everything else — endpointless current rows, historical
ProxmoxInventory scan evidence, and the old singleton inventory state — predates
multi-cluster and belongs to the one cluster that has ever been scanned: the sole
enabled cluster. A duplicate VMID cannot exist yet, so this attribution is
unambiguous for existing data.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    ProxmoxCluster = apps.get_model("core", "ProxmoxCluster")
    CurrentGuestInventory = apps.get_model("core", "CurrentGuestInventory")
    CurrentGuestInventoryState = apps.get_model("core", "CurrentGuestInventoryState")
    ProxmoxInventory = apps.get_model("core", "ProxmoxInventory")

    # Current guests: authoritative attribution from the source endpoint first.
    for guest in CurrentGuestInventory.objects.filter(
        cluster__isnull=True, source_endpoint__isnull=False
    ).select_related("source_endpoint"):
        if guest.source_endpoint.cluster_id is not None:
            guest.cluster_id = guest.source_endpoint.cluster_id
            guest.save(update_fields=["cluster"])

    needs_sole = (
        CurrentGuestInventory.objects.filter(cluster__isnull=True).exists()
        or ProxmoxInventory.objects.filter(cluster__isnull=True).exists()
        or CurrentGuestInventoryState.objects.filter(cluster__isnull=True).exists()
    )
    if not needs_sole:
        return

    enabled = list(ProxmoxCluster.objects.filter(enabled=True)[:2])
    if len(enabled) != 1:
        # With no unambiguous sole cluster, refuse rather than guess which cluster
        # owns historical evidence — a wrong guess is the cross-cluster confusion
        # this whole foundation prevents.
        raise RuntimeError(
            "Cannot backfill read-model cluster: expected exactly one enabled cluster, "
            f"found {ProxmoxCluster.objects.filter(enabled=True).count()}."
        )
    sole = enabled[0]

    CurrentGuestInventory.objects.filter(cluster__isnull=True).update(cluster=sole)
    ProxmoxInventory.objects.filter(cluster__isnull=True).update(cluster=sole)
    CurrentGuestInventoryState.objects.filter(cluster__isnull=True).update(cluster=sole)


def unbackfill(apps, schema_editor):
    CurrentGuestInventory = apps.get_model("core", "CurrentGuestInventory")
    CurrentGuestInventoryState = apps.get_model("core", "CurrentGuestInventoryState")
    ProxmoxInventory = apps.get_model("core", "ProxmoxInventory")
    CurrentGuestInventory.objects.update(cluster=None)
    ProxmoxInventory.objects.update(cluster=None)
    CurrentGuestInventoryState.objects.update(cluster=None)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0011_read_models_cluster"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]

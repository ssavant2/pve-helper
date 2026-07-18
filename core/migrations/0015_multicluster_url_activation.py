import django.db.models.deletion
from django.db import migrations, models


def prepare_activation_data(apps, schema_editor):
    ProxmoxCluster = apps.get_model("core", "ProxmoxCluster")
    ProxmoxEndpoint = apps.get_model("core", "ProxmoxEndpoint")
    ProxmoxInventory = apps.get_model("core", "ProxmoxInventory")
    ProxmoxStorageConsumer = apps.get_model("core", "ProxmoxStorageConsumer")
    StorageSpaceSnapshot = apps.get_model("core", "StorageSpaceSnapshot")

    if ProxmoxEndpoint.objects.filter(cluster__isnull=True).exists():
        raise RuntimeError("Cannot activate multi-cluster identity: an endpoint has no cluster.")
    if ProxmoxStorageConsumer.objects.filter(cluster__isnull=True).exists():
        raise RuntimeError("Cannot activate multi-cluster identity: a storage consumer has no cluster.")

    duplicate_uuids = (
        ProxmoxCluster.objects.exclude(discovered_ca_uuid="")
        .values("discovered_ca_uuid")
        .annotate(count=models.Count("id"))
        .filter(count__gt=1)
    )
    if duplicate_uuids.exists():
        raise RuntimeError(
            "Cannot activate multi-cluster identity: two cluster records claim the same Proxmox CA UUID."
        )

    for snapshot in StorageSpaceSnapshot.objects.filter(
        cluster__isnull=True,
        storage__isnull=True,
    ).iterator():
        candidates = ProxmoxInventory.objects.filter(
            node=snapshot.node,
            name=snapshot.api_storage_id,
            cluster__isnull=False,
        )
        if snapshot.scan_run_id is not None:
            candidates = candidates.filter(scan_run_id=snapshot.scan_run_id)
        cluster_ids = list(
            candidates.values_list("cluster_id", flat=True).distinct()[:2]
        )
        if len(cluster_ids) == 1:
            StorageSpaceSnapshot.objects.filter(pk=snapshot.pk).update(
                cluster_id=cluster_ids[0]
            )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("core", "0014_durable_object_identity")]

    operations = [
        migrations.RemoveConstraint(
            model_name="proxmoxcluster",
            name="single_enabled_cluster_until_activation",
        ),
        migrations.AddConstraint(
            model_name="proxmoxcluster",
            constraint=models.UniqueConstraint(
                fields=("discovered_ca_uuid",),
                condition=~models.Q(discovered_ca_uuid=""),
                name="unique_nonblank_cluster_ca_uuid",
            ),
        ),
        migrations.RemoveIndex(
            model_name="storagespacesnapshot",
            name="core_storag_node_3cc367_idx",
        ),
        migrations.AddField(
            model_name="storagespacesnapshot",
            name="cluster",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="storage_space_snapshots",
                to="core.proxmoxcluster",
            ),
        ),
        migrations.RunPython(prepare_activation_data, noop_reverse),
        migrations.AlterField(
            model_name="proxmoxendpoint",
            name="cluster",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="endpoints",
                to="core.proxmoxcluster",
            ),
        ),
        migrations.AlterField(
            model_name="proxmoxstorageconsumer",
            name="cluster",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="storage_consumers",
                to="core.proxmoxcluster",
            ),
        ),
        migrations.AddIndex(
            model_name="storagespacesnapshot",
            index=models.Index(
                fields=["cluster", "node", "api_storage_id", "recorded_at"],
                name="core_space_cl_api_time_idx",
            ),
        ),
    ]

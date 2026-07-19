from django.db import migrations, models


def republish_shared_volume_scopes(apps, schema_editor):
    """Fail-safe conversion to one logical volume set per shared definition.

    Shared definitions previously stored one duplicate copy of the whole volume
    list per answering node; they now store the set once under the empty node,
    with the node agreement recorded on the coverage row. Rather than guess which
    stored copy is authoritative, the old copies are dropped and the affected
    scopes are marked incomplete: an incomplete scope cannot prove absence, so
    nothing can be classified as an orphan from converted data. The next volume
    refresh (default every five minutes) republishes them.
    """
    ClusterStorage = apps.get_model("core", "ClusterStorage")
    ClusterStorageVolumeObservation = apps.get_model("core", "ClusterStorageVolumeObservation")
    ClusterStorageVolumeCoverage = apps.get_model("core", "ClusterStorageVolumeCoverage")

    shared_ids = list(ClusterStorage.objects.filter(shared=True).values_list("id", flat=True))
    if not shared_ids:
        return
    ClusterStorageVolumeObservation.objects.filter(cluster_storage_id__in=shared_ids).delete()
    ClusterStorageVolumeCoverage.objects.filter(cluster_storage_id__in=shared_ids, node__isnull=True).update(
        complete=False,
        volume_generation=None,
        error_code="republish_required",
        error_reason="Volume inventory is being republished after an upgrade.",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0020_storagemount_identity_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="clusterstoragevolumecoverage",
            name="agreeing_nodes",
            field=models.JSONField(blank=True, default=list),
        ),
        # The unique tuple is unchanged; only the column order is. The backing
        # index now serves the classification hot path and the refresh diff,
        # which both match on (cluster_storage, volid). Rebuilding it takes a
        # brief exclusive lock on a table the refresh rewrites anyway.
        migrations.RemoveConstraint(
            model_name="clusterstoragevolumeobservation",
            name="unique_cluster_storage_volume_observation",
        ),
        migrations.AddConstraint(
            model_name="clusterstoragevolumeobservation",
            constraint=models.UniqueConstraint(
                fields=("cluster_storage", "volid", "node"),
                name="unique_cluster_storage_volume_observation",
            ),
        ),
        migrations.RunPython(republish_shared_volume_scopes, migrations.RunPython.noop),
    ]

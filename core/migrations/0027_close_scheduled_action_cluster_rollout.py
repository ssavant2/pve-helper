"""Make a schedule's cluster mandatory, and scope its name uniqueness to that cluster.

`ScheduledAction.cluster` was additive during the version-0 rollout, resolved for
legacy rows through the sole-cluster adapter. Navigation now groups schedules under
their cluster the way tags and datastores already are, and a null has no honest place
in that tree — it would simply not render anywhere. So the rollout closes here.

An installation with exactly one cluster can be backfilled without guessing: that is
what the adapter was resolving to anyway. With several clusters there is no defensible
answer, so this refuses rather than picking one, in the same shape as
`0016_close_read_model_cluster_rollout`.

The name constraint moves with it. Fleet-wide uniqueness made "Nightly backup"
available in only one cluster, which is arbitrary once the tree says schedules belong
to a cluster. Existing rows cannot violate the narrower constraint: it is strictly
weaker than the one being dropped.
"""

import django.db.models.deletion
from django.db import migrations, models


def backfill_scheduled_action_cluster(apps, schema_editor):
    ScheduledAction = apps.get_model("core", "ScheduledAction")
    ProxmoxCluster = apps.get_model("core", "ProxmoxCluster")

    unresolved = ScheduledAction.objects.filter(cluster__isnull=True)
    count = unresolved.count()
    if not count:
        return

    clusters = list(ProxmoxCluster.objects.all()[:2])
    if len(clusters) != 1:
        raise RuntimeError(
            f"Cannot activate non-null scheduled-action cluster identity: {count} row(s) have no "
            f"cluster and {len(clusters)} clusters are registered, so there is no unambiguous "
            "cluster to attribute them to. Assign each schedule a cluster before migrating."
        )
    unresolved.update(cluster=clusters[0])


class Migration(migrations.Migration):
    dependencies = [("core", "0026_storage_space_snapshot_scope")]

    operations = [
        migrations.RunPython(backfill_scheduled_action_cluster, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="scheduledaction",
            name="cluster",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="scheduled_actions",
                to="core.proxmoxcluster",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="scheduledaction",
            name="uniq_active_scheduled_action_name",
        ),
        migrations.AddConstraint(
            model_name="scheduledaction",
            constraint=models.UniqueConstraint(
                condition=models.Q(("deleted_at__isnull", True)),
                fields=("cluster", "name"),
                name="uniq_active_scheduled_action_name_per_cluster",
            ),
        ),
    ]

"""Enforce the scope `StorageSpaceSnapshot` has documented since it was written.

The model comment has always claimed "exactly one of these is set" — a mounted
`StorageMount`, or a local API-only storage identified by `(node,
api_storage_id)`. Nothing enforced it, while the two neighbouring models with the
same shape both do (`storage_mount_scope_matches_node`,
`storage_volume_coverage_scope_node`) and `__str__` here already assumes it.

Four months of rows show the writers keep the identity XOR without help: no row
sets both identities and none sets neither. The half that did drift is the one a
bare XOR would not have covered. `0015` added `cluster` as nullable without
backfilling the snapshots already in the table, and eight API-side rows were left
without one. That is not a cosmetic gap: `_api_storage_space_chart_data` selects
on `storage__isnull=True, cluster=..., api_storage_id=...`, so a cluster-less API
row is not reachable by any chart in the application. They had been invisible for
five days and would have been pruned by the eight-day retention window three days
later, which is precisely why nobody would ever have found them by looking.

So the constraint covers both halves rather than the identity alone: the reader's
filter is the real contract, and it reads more than one column.

The unreachable rows are deleted rather than repaired. Choosing a cluster for
them would mean inferring one from a node name, and an invented attribution in a
capacity time series is worse than a gap — the rows carry no information any
surface can display, and the retention sweep was going to remove them anyway.

Reverse drops the constraint; it does not resurrect the deleted rows.
"""

from django.db import migrations, models


def delete_unreachable_snapshots(apps, schema_editor):
    snapshot = apps.get_model("core", "StorageSpaceSnapshot")
    # The complement of the constraint, written once: anything the check would
    # reject has to leave before the check exists.
    snapshot.objects.exclude(
        models.Q(storage__isnull=False, cluster__isnull=True, node="", api_storage_id="")
        | (models.Q(storage__isnull=True, cluster__isnull=False) & ~models.Q(node="") & ~models.Q(api_storage_id=""))
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0025_drop_prefix_redundant_fk_indexes"),
    ]

    operations = [
        migrations.RunPython(delete_unreachable_snapshots, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="storagespacesnapshot",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("api_storage_id", ""),
                        ("cluster__isnull", True),
                        ("node", ""),
                        ("storage__isnull", False),
                    ),
                    models.Q(
                        ("cluster__isnull", False),
                        ("storage__isnull", True),
                        models.Q(("node", ""), _negated=True),
                        models.Q(("api_storage_id", ""), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="storage_space_snapshot_scope",
            ),
        ),
    ]

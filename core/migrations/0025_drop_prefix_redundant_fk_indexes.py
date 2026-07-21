"""Drop nineteen indexes that duplicate the leading column of a wider one.

`0024` removed one of these by hand (`fileinventory.storage`) while fixing a
different finding. A sweep across every model in `core` then found eighteen more
of exactly the same shape: Django indexes every `ForeignKey` by default, and on
these models that column also leads a composite `models.Index` or a
`UniqueConstraint`. The implicit index is therefore a strict prefix — a second
btree, maintained on every insert and on every update of that column, to answer
lookups the wider index answers at the same complexity. `consolesession.status`
is the one non-FK in the set; it carried an explicit `db_index=True` that
`core_console_status_exp_idx` already leads with.

`pg_stat_user_indexes.idx_scan` is *not* the arbiter here, and this is the
interesting part. It was the arbiter in `0024`, where the claim was "no query
reaches this column" — a claim the planner can settle. The claim here is
"something else answers this", and a prefix index scores well on `idx_scan`
precisely *because* it is redundant: it is the narrower of two applicable
indexes, so Postgres prefers it. Measured before this migration,
`core_clusterstoragenodestate_cluster_storage_id_5c7207bd` had 115160 scans and
`core_proxmoxinventory_scan_run_id_1065cfc1` had 375. Those lookups do not
disappear; they move to `core_csnode_state_idx` and
`core_proxmo_scan_ru_7d6c24_idx`, which lead with the same column. Reading the
counter as "in use, keep it" would have preserved every index in the set.

What was checked instead, because these are the ways a prefix can fail to be
covered:

* No wider index in the set is partial. A `UniqueConstraint(condition=...)`
  builds a partial index, which answers only queries matching its predicate; the
  seven partial constraints in this module are all on other columns.
* No wider index leads with a descending field, and none uses a non-default
  opclass. The one opclass that mattered was the `varchar_pattern_ops` twin
  Postgres builds alongside an indexed `CharField` — `consolesession.status` —
  and nothing does a prefix match on an enum.
* Every FK's cascade or `PROTECT` check reads the same leading column, so it is
  served by the composite like any other lookup.

`InventoryIndexInvariantTests` re-runs the sweep over every model, so a
`ForeignKey` added tomorrow to a model whose composite already leads with it
fails the suite instead of quietly costing a btree.

Reversible: migrating backwards rebuilds all nineteen.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0024_drop_unreachable_inventory_indexes"),
    ]

    operations = [
        migrations.AlterField(
            model_name="clusterstorage",
            name="cluster",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="storage_definitions",
                to="core.proxmoxcluster",
            ),
        ),
        migrations.AlterField(
            model_name="clusterstoragemount",
            name="cluster_storage",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="mount_bindings",
                to="core.clusterstorage",
            ),
        ),
        migrations.AlterField(
            model_name="clusterstoragenodestate",
            name="cluster_storage",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="node_states",
                to="core.clusterstorage",
            ),
        ),
        migrations.AlterField(
            model_name="clusterstoragevolumecoverage",
            name="cluster_storage",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="volume_coverages",
                to="core.clusterstorage",
            ),
        ),
        migrations.AlterField(
            model_name="clusterstoragevolumeobservation",
            name="cluster_storage",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="volume_observations",
                to="core.clusterstorage",
            ),
        ),
        migrations.AlterField(
            model_name="consolesession",
            name="cluster",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="console_sessions",
                to="core.proxmoxcluster",
            ),
        ),
        migrations.AlterField(
            model_name="consolesession",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("connecting", "Connecting"),
                    ("connected", "Connected"),
                    ("closed", "Closed"),
                    ("failed", "Failed"),
                    ("expired", "Expired"),
                ],
                default="pending",
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="currentguestinventory",
            name="cluster",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="current_guests",
                to="core.proxmoxcluster",
            ),
        ),
        migrations.AlterField(
            model_name="currentguestinventory",
            name="source_endpoint",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="current_guests",
                to="core.proxmoxendpoint",
            ),
        ),
        migrations.AlterField(
            model_name="fileinventory",
            name="scan_run",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="files",
                to="core.scanrun",
            ),
        ),
        migrations.AlterField(
            model_name="proxmoxendpoint",
            name="cluster",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="endpoints",
                to="core.proxmoxcluster",
            ),
        ),
        migrations.AlterField(
            model_name="proxmoxinventory",
            name="cluster",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="proxmox_objects",
                to="core.proxmoxcluster",
            ),
        ),
        migrations.AlterField(
            model_name="proxmoxinventory",
            name="scan_run",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="proxmox_objects",
                to="core.scanrun",
            ),
        ),
        migrations.AlterField(
            model_name="proxmoxstorageconsumer",
            name="storage",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="consumer_statuses",
                to="core.storagemount",
            ),
        ),
        migrations.AlterField(
            model_name="scanclusterobservation",
            name="scan_run",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="cluster_observations",
                to="core.scanrun",
            ),
        ),
        migrations.AlterField(
            model_name="scheduledaction",
            name="cluster",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="scheduled_actions",
                to="core.proxmoxcluster",
            ),
        ),
        migrations.AlterField(
            model_name="scheduledactionrun",
            name="scheduled_action",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="runs",
                to="core.scheduledaction",
            ),
        ),
        migrations.AlterField(
            model_name="storagespacesnapshot",
            name="cluster",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="storage_space_snapshots",
                to="core.proxmoxcluster",
            ),
        ),
        migrations.AlterField(
            model_name="storagespacesnapshot",
            name="storage",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="space_snapshots",
                to="core.storagemount",
            ),
        ),
    ]

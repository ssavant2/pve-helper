"""Drop four indexes no query could reach.

Measured against the running database before removal, since "no query uses
this" is a claim about the planner and not about the source:
`pg_stat_user_indexes.idx_scan` was 0 for all four over the database's whole
lifetime (`pg_stat_database.stats_reset` was null), and a source sweep found no
filter on `vmid` or `derived_volid` at all. The one query that filters
`content` binds `cluster_storage`, `observed_volume_generation` and `node` in
the same call, so `core_csvol_generation_idx` serves it outright.

The FK index on `fileinventory.storage` goes for a different reason: it is a
strict prefix of the `(storage, path)` composite that stays, so the planner —
including the cascade check — has never needed it.

Two single-column indexes named in the same review finding are deliberately
kept: `classification` and `content_category` are the two most-scanned indexes
on the table. The finding asserted `content_category` "is never filtered at
all"; `_storage_content_usage` filters it six times per call.

Reversible: re-running this migration backwards rebuilds all four.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0023_clusterstoragenodestate_unreachable"),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name="clusterstoragevolumeobservation",
            name="core_csvol_vmid_idx",
        ),
        migrations.RemoveIndex(
            model_name="clusterstoragevolumeobservation",
            name="core_csvol_content_idx",
        ),
        migrations.RemoveIndex(
            model_name="fileinventory",
            name="core_filein_storage_e0df46_idx",
        ),
        migrations.AlterField(
            model_name="fileinventory",
            name="storage",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="files",
                to="core.storagemount",
            ),
        ),
    ]

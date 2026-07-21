"""Persist per-guest pool, HA state and guest-agent enrichment on the projection.

The VM overview rendered pool, HA status, real OS name, hostname and IPs from a
process-local cache the periodic worker could never warm — `LocMemCache` is not
shared between the worker and the web processes, so those fields only ever
appeared for a guest whose detail page a given web process had already opened.

Pool and HA state already ride on the `cluster/resources` response the live
reconcile reads, so they cost no extra provider call and are stored on every
refresh. The guest-agent facts (OS pretty name, hostname, IPs) are fetched by the
worker on a slower TTL and kept in `agent_info`, so overview and summary read one
shared, cross-process copy instead of each web process fanning out its own calls.

All fields default to empty; the reverse simply drops them.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0027_close_scheduled_action_cluster_rollout"),
    ]

    operations = [
        migrations.AddField(
            model_name="currentguestinventory",
            name="pool",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="currentguestinventory",
            name="ha_state",
            field=models.CharField(blank=True, default="", max_length=40),
        ),
        migrations.AddField(
            model_name="currentguestinventory",
            name="agent_info",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="currentguestinventory",
            name="agent_observed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]

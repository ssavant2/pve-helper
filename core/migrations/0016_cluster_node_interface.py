"""Node network projection: the interface rows and their coverage domain. 5a4B-i.

The forward direction is not the risk. `core_projection_coverage_scope` gains a
third node-grained domain inside an existing OR arm, which makes the constraint
monotonically *weaker*: every row that satisfied the two-domain version satisfies
this one, so validating it against populated coverage cannot fail and proves
nothing.

The **reverse** direction is the one that aborts. Once `node_network` coverage rows
exist, re-adding the narrower constraint validates them and fails, leaving the
database mid-rollback with no way forward that does not involve hand-deleting rows
on a live cluster. `_drop_node_network_coverage` runs in that window -- after the
wider constraint is dropped, before the narrower one is added -- so a rollback is an
ordinary migration rather than an outage with a manual step.
"""

from django.db import migrations, models

import core.models


def _forward_noop(apps, schema_editor):
    """Nothing to do going forward; the new domain has no rows yet."""


def _drop_node_network_coverage(apps, schema_editor):
    """Remove the rows the narrower constraint is about to reject.

    Coverage is a statement about what a refresh proved, not durable history, so
    dropping this domain's rows loses nothing a later sweep will not republish.
    The interface rows themselves are removed by the reversed `CreateModel`.
    """
    coverage = apps.get_model("core", "ClusterProjectionCoverage")
    coverage.objects.filter(domain="node_network").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0015_guest_publication_scope"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="clusterprojectioncoverage",
            name="core_projection_coverage_scope",
        ),
        migrations.RunPython(_forward_noop, _drop_node_network_coverage),
        migrations.AddConstraint(
            model_name="clusterprojectioncoverage",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        domain="membership",
                        node_name__isnull=True,
                        based_on_generation__isnull=True,
                    )
                    | (
                        models.Q(
                            domain__in=("node_runtime", "node_network"),
                            node_name__isnull=False,
                            based_on_generation__isnull=False,
                        )
                        & ~models.Q(node_name="")
                        & ~models.Q(node_name__contains=":")
                    )
                ),
                name="core_projection_coverage_scope",
            ),
        ),
        migrations.AlterField(
            model_name="clusterprojectioncoverage",
            name="domain",
            field=models.CharField(
                choices=[
                    ("membership", "Membership"),
                    ("node_runtime", "Node runtime"),
                    ("node_network", "Node network"),
                ],
                max_length=64,
            ),
        ),
        migrations.CreateModel(
            name="ClusterNodeInterface",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("node_name", models.CharField(max_length=120)),
                ("iface", models.CharField(max_length=120)),
                ("interface_type", models.CharField(blank=True, default="", max_length=32)),
                ("attachable", models.BooleanField(default=False)),
                ("active", models.BooleanField(blank=True, null=True)),
                ("autostart", models.BooleanField(blank=True, null=True)),
                ("method", models.CharField(blank=True, default="", max_length=32)),
                ("address", models.CharField(blank=True, default="", max_length=64)),
                ("cidr", models.CharField(blank=True, default="", max_length=64)),
                ("gateway", models.CharField(blank=True, default="", max_length=64)),
                ("bridge_ports", models.CharField(blank=True, default="", max_length=255)),
                ("bridge_vids", models.CharField(blank=True, default="", max_length=255)),
                ("bridge_vlan_aware", models.BooleanField(blank=True, null=True)),
                ("bond_mode", models.CharField(blank=True, default="", max_length=64)),
                ("bond_slaves", models.CharField(blank=True, default="", max_length=255)),
                ("comments", models.TextField(blank=True, default="")),
                ("observed_generation", models.PositiveBigIntegerField(default=0)),
                ("based_on_enrollment_generation", models.PositiveBigIntegerField(blank=True, null=True)),
                ("last_seen_at", models.DateTimeField(blank=True, null=True)),
                ("present", models.BooleanField(default=True)),
                ("unreachable", models.BooleanField(default=False)),
                (
                    "cluster",
                    models.ForeignKey(
                        db_index=False,
                        on_delete=models.deletion.CASCADE,
                        related_name="node_interfaces",
                        to="core.proxmoxcluster",
                    ),
                ),
            ],
            options={
                "verbose_name": "cluster node interface",
                "verbose_name_plural": "cluster node interfaces",
                "ordering": ["cluster__key", "node_name", "iface"],
            },
            bases=(core.models.TimestampedModel,),
        ),
        migrations.AddConstraint(
            model_name="clusternodeinterface",
            constraint=models.UniqueConstraint(
                fields=("cluster", "node_name", "iface"), name="core_node_interface_uniq"
            ),
        ),
        migrations.AddConstraint(
            model_name="clusternodeinterface",
            constraint=models.CheckConstraint(
                condition=(~models.Q(node_name="") & ~models.Q(node_name__contains=":") & ~models.Q(iface="")),
                name="core_node_interface_valid_ref",
            ),
        ),
        migrations.AddIndex(
            model_name="clusternodeinterface",
            index=models.Index(fields=["cluster", "node_name", "attachable"], name="core_node_iface_attach_idx"),
        ),
    ]

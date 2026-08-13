from django.db import migrations, models


class Migration(migrations.Migration):
    """Record which nodes a guest-inventory pass completely read.

    Endpoint coverage was standing in for node coverage, and the scan's gap-fill
    pass reads nodes that have no endpoint row of their own. Existing rows default
    to an empty set: no node is claimed as covered until a pass measures one, which
    is the conservative direction — an empty set retires nothing.
    """

    dependencies = [
        ("core", "0013_topology_handoff"),
    ]

    operations = [
        migrations.AddField(
            model_name="currentguestinventorystate",
            name="covered_nodes",
            field=models.JSONField(blank=True, default=list),
        ),
    ]

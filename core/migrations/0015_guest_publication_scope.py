from django.db import migrations, models


class Migration(migrations.Migration):
    """Mark guest rows with whether they may be published, and under which policy.

    Both defaults describe the pre-enrollment world exactly: every existing row is
    published, under generation 0. Clusters below ``enrollment_contract_version`` 1
    keep those values forever, so an installation that never activates enrollment is
    byte-for-byte unaffected.

    Rolling back drops the marks, which restores cluster-wide publication — the same
    state ``enrollment_contract_version`` already makes visible in one query.
    """

    dependencies = [
        ("core", "0014_guest_inventory_node_coverage"),
    ]

    operations = [
        migrations.AddField(
            model_name="currentguestinventory",
            name="published",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="currentguestinventory",
            name="based_on_enrollment_generation",
            field=models.PositiveBigIntegerField(default=0),
        ),
    ]

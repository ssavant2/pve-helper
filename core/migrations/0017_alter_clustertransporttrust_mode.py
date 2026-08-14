from django.db import migrations, models


class Migration(migrations.Migration):
    """Add the additive `public_ca_pem` transport-trust mode (5a1K).

    Choices-only: PostgreSQL does not enforce `choices`, and `max_length=20` already
    fits the new 13-character value, so this is a state change with no SQL. Existing
    rows are untouched, and a rollback leaves any stored `public_ca_pem` readable —
    `resolve_trust_profile` maps an unknown mode to `public`, which loses the node
    rather than the verification.
    """

    dependencies = [
        ("core", "0016_cluster_node_interface"),
    ]

    operations = [
        migrations.AlterField(
            model_name="clustertransporttrust",
            name="mode",
            field=models.CharField(
                choices=[
                    ("public", "Publicly trusted"),
                    ("ca_pem", "Internal CA bundle"),
                    ("public_ca_pem", "Public CA store plus cluster CA"),
                ],
                default="public",
                max_length=20,
            ),
        ),
    ]

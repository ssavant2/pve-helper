from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0007_recurrence_kind_none_sentinel"),
    ]

    operations = [
        migrations.AddField(
            model_name="scheduledaction",
            name="end_condition",
            field=models.CharField(
                choices=[
                    ("none", "No end"),
                    ("run_until", "Run until"),
                    ("run_count", "Run a fixed number of times"),
                ],
                default="none",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="scheduledaction",
            name="max_scheduled_runs",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="scheduledaction",
            name="run_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="scheduledaction",
            name="scheduled_run_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddConstraint(
            model_name="scheduledaction",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(("end_condition", "none"), ("max_scheduled_runs__isnull", True), ("run_until__isnull", True))
                    | models.Q(
                        ("end_condition", "run_until"),
                        ("max_scheduled_runs__isnull", True),
                        ("run_until__isnull", False),
                    )
                    | models.Q(
                        ("end_condition", "run_count"),
                        ("max_scheduled_runs__gte", 1),
                        ("max_scheduled_runs__lte", 999),
                        ("run_until__isnull", True),
                    )
                ),
                name="scheduled_action_end_condition_fields",
            ),
        ),
    ]

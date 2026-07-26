from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0008_scheduled_action_end_conditions"),
    ]

    operations = [
        migrations.CreateModel(
            name="ScheduledActionSettings",
            fields=[
                (
                    "id",
                    models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("run_history_retention_days", models.PositiveSmallIntegerField(default=90)),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            ("run_history_retention_days__gte", 1),
                            ("run_history_retention_days__lte", 999),
                        ),
                        name="scheduled_action_retention_days_range",
                    ),
                ],
            },
        ),
    ]

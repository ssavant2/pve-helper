from django.db import migrations, models


def advanced_to_none(apps, schema_editor):
    """Retire the `advanced` recurrence kind.

    It was never offered in the form, so a stored `advanced` row is almost certainly a
    one-time schedule using it as the filler kind — `none` is what those always meant.
    A *recurring* row could only carry it via a crafted post, and its RRULE was
    evaluated against a `DTSTART` of "now", so it never ran the series the operator
    wrote. Those are disabled rather than reinterpreted: there is no honest mapping
    from an unsound rule to one of the four structured kinds, and leaving them enabled
    would make every scheduling pass raise on an unschedulable row.
    """
    ScheduledAction = apps.get_model("core", "ScheduledAction")
    advanced = ScheduledAction.objects.filter(recurrence_kind="advanced")
    advanced.filter(schedule_type="recurring").update(enabled=False, next_run_at=None)
    advanced.update(recurrence_kind="none")


def none_to_advanced(apps, schema_editor):
    ScheduledAction = apps.get_model("core", "ScheduledAction")
    ScheduledAction.objects.filter(recurrence_kind="none").update(recurrence_kind="advanced")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0006_managed_certificates"),
    ]

    operations = [
        migrations.AlterField(
            model_name="scheduledaction",
            name="recurrence_kind",
            field=models.CharField(
                choices=[
                    ("none", "Not recurring"),
                    ("daily", "Daily"),
                    ("weekly", "Weekly"),
                    ("monthly_ordinal", "Monthly ordinal"),
                    ("monthly_day", "Monthly day"),
                ],
                default="none",
                max_length=40,
            ),
        ),
        migrations.RunPython(advanced_to_none, none_to_advanced),
    ]

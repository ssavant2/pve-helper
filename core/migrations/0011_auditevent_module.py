from django.db import migrations, models


def _module_for(action, object_type, details):
    """Mirror of core.views.common._audit_module_key_for for the backfill."""
    details = details if isinstance(details, dict) else {}
    action = action or ""
    object_type = object_type or ""
    if action.startswith("auth."):
        return "auth"
    if action.startswith("network.") or object_type.startswith("network"):
        return "network"
    if action.startswith("vm.") or action.startswith("scheduled_action.") or object_type in {
        "vm",
        "ct",
        "guest",
        "scheduled_action",
        "scheduled_action_run",
    }:
        return "vms"
    if action.startswith("cluster.") or object_type.startswith("cluster"):
        return "clusters"
    if (
        action.startswith("scan.")
        or action.startswith("file.")
        or action.startswith("trash.")
        or object_type in {"scan_run", "scan_schedule", "storage", "file"}
        or details.get("target_storage")
    ):
        return "storage"
    return "system"


def backfill_module(apps, schema_editor):
    AuditEvent = apps.get_model("core", "AuditEvent")
    updates = []
    for event in AuditEvent.objects.all().iterator():
        module = _module_for(event.action, event.object_type, event.details)
        if event.module != module:
            event.module = module
            updates.append(event)
        if len(updates) >= 500:
            AuditEvent.objects.bulk_update(updates, ["module"])
            updates = []
    if updates:
        AuditEvent.objects.bulk_update(updates, ["module"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0010_scheduledaction_name_unique"),
    ]

    operations = [
        migrations.AddField(
            model_name="auditevent",
            name="module",
            field=models.CharField(blank=True, db_index=True, default="", max_length=20),
            preserve_default=False,
        ),
        migrations.RunPython(backfill_module, noop),
    ]

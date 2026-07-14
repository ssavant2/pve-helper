from django.db import migrations


def _module_key(action, object_type, details):
    details = details if isinstance(details, dict) else {}
    action = action or ""
    object_type = object_type or ""

    if action.startswith("auth."):
        return "auth"
    if action.startswith("network.") or object_type.startswith("network"):
        return "network"
    if (
        action.startswith("vm.")
        or action.startswith("scheduled_action.")
        or object_type in {"vm", "ct", "guest", "scheduled_action", "scheduled_action_run"}
    ):
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


def backfill_audit_modules(apps, schema_editor):
    AuditEvent = apps.get_model("core", "AuditEvent")
    pending = []
    queryset = AuditEvent.objects.filter(module="").only("id", "action", "object_type", "details")
    for event in queryset.iterator(chunk_size=1_000):
        event.module = _module_key(event.action, event.object_type, event.details)
        pending.append(event)
        if len(pending) >= 1_000:
            AuditEvent.objects.bulk_update(pending, ["module"], batch_size=1_000)
            pending = []
    if pending:
        AuditEvent.objects.bulk_update(pending, ["module"], batch_size=1_000)


class Migration(migrations.Migration):
    dependencies = [("core", "0020_current_guest_inventory")]

    operations = [migrations.RunPython(backfill_audit_modules, migrations.RunPython.noop)]

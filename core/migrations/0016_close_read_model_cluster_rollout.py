import django.db.models.deletion
from django.db import migrations, models


def require_qualified_read_models(apps, schema_editor):
    model_names = (
        "CurrentGuestInventory",
        "CurrentGuestInventoryState",
    )
    ambiguous = {}
    for model_name in model_names:
        model = apps.get_model("core", model_name)
        count = model.objects.filter(cluster__isnull=True).count()
        if count:
            ambiguous[model_name] = count
    if ambiguous:
        detail = ", ".join(f"{name}={count}" for name, count in ambiguous.items())
        raise RuntimeError(
            "Cannot activate non-null read-model cluster identity; unresolved rows remain: "
            f"{detail}. Run the multi-cluster readiness repair before migrating."
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0015_multicluster_url_activation")]

    operations = [
        migrations.RunPython(require_qualified_read_models, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="currentguestinventory",
            name="cluster",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="current_guests",
                to="core.proxmoxcluster",
            ),
        ),
        migrations.AlterField(
            model_name="currentguestinventorystate",
            name="cluster",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="inventory_state",
                to="core.proxmoxcluster",
            ),
        ),
    ]

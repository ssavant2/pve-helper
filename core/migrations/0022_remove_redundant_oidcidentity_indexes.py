from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("core", "0021_backfill_audit_modules")]

    operations = [
        migrations.RemoveIndex(
            model_name="oidcidentity",
            name="core_oidcid_user_id_idx",
        ),
        migrations.RemoveIndex(
            model_name="oidcidentity",
            name="core_oidcid_issuer_subject_idx",
        ),
    ]

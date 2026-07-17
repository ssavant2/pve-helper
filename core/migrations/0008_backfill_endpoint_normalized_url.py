"""Canonicalize the URLs of endpoints registered before the uniqueness rule.

The constraint ignores empty values, so without this backfill every pre-existing
endpoint would sit outside the rule that one transport belongs to one cluster —
exactly the rows most likely to be re-pointed by hand.
"""

from django.db import migrations

from core.services.config import normalize_endpoint_url


def backfill_normalized_url(apps, schema_editor):
    ProxmoxEndpoint = apps.get_model("core", "ProxmoxEndpoint")

    seen: dict[str, str] = {}
    for endpoint in ProxmoxEndpoint.objects.all().order_by("pk"):
        normalized = normalize_endpoint_url(endpoint.url)
        if not normalized:
            continue
        if normalized in seen:
            # Two existing rows already name the same transport. Refuse rather than
            # let the constraint pick a winner: which cluster owns it is an operator
            # decision, and guessing is how inventory lands under a wrong identity.
            raise RuntimeError(
                f"Endpoints '{seen[normalized]}' and '{endpoint.name}' both resolve to "
                f"{normalized}. Remove or re-point one before migrating."
            )
        seen[normalized] = endpoint.name
        endpoint.normalized_url = normalized
        endpoint.save(update_fields=["normalized_url"])


def clear_normalized_url(apps, schema_editor):
    ProxmoxEndpoint = apps.get_model("core", "ProxmoxEndpoint")
    ProxmoxEndpoint.objects.update(normalized_url="")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0007_endpoint_normalized_url"),
    ]

    operations = [
        migrations.RunPython(backfill_normalized_url, clear_normalized_url),
    ]

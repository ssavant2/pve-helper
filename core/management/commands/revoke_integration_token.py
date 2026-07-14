from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import IntegrationToken
from core.services.audit_events import record_audit_event


class Command(BaseCommand):
    help = "Revoke a backup integration bearer token."

    def add_arguments(self, parser):
        parser.add_argument("token_id")

    def handle(self, *args, **options):
        token = IntegrationToken.objects.filter(token_id=options["token_id"]).first()
        if token is None:
            raise CommandError("Token not found")
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at", "updated_at"])
        record_audit_event(
            username="management-command",
            action="tag.integration.token",
            object_type="integration_token",
            object_id=token.token_id,
            details={"operation": "revoke", "name": token.name},
        )
        self.stdout.write(f"Revoked {token.token_id}")

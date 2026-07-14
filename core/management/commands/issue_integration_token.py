from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from core.services.audit_events import record_audit_event
from core.services.integration_tokens import issue_token


class Command(BaseCommand):
    help = "Issue a read-only backup integration bearer token."

    def add_arguments(self, parser):
        parser.add_argument("name")
        parser.add_argument("--expires-at")

    def handle(self, *args, **options):
        expires_at = parse_datetime(options["expires_at"]) if options.get("expires_at") else None
        if options.get("expires_at") and expires_at is None:
            raise CommandError("--expires-at must be an ISO-8601 datetime")
        token, raw = issue_token(options["name"], expires_at=expires_at)
        record_audit_event(
            username="management-command",
            action="tag.integration.token",
            object_type="integration_token",
            object_id=token.token_id,
            details={"operation": "issue", "name": token.name, "expires_at": options.get("expires_at") or ""},
        )
        self.stdout.write(raw)
        self.stderr.write("Store this token now; the secret cannot be recovered.")

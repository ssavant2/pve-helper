from django.core.management.base import BaseCommand, CommandError

from core.services.audit_events import record_audit_event
from core.services.cluster_credentials import complete_credential_cutover
from core.services.secret_encryption import EncryptionConfigurationError


class Command(BaseCommand):
    help = (
        "Import the legacy global Proxmox token into the bootstrap cluster's encrypted "
        "credential storage and stop runtime reads of the legacy settings. Deliberately "
        "explicit: it changes where every provider call gets its identity. Reversible by "
        "code rollback — the legacy settings are ignored from here, not deleted."
    )

    def handle(self, *args, **options):
        try:
            changed, message = complete_credential_cutover()
        except EncryptionConfigurationError as exc:
            raise CommandError(f"{exc} The cutover was not recorded, so the legacy token is still in use.") from exc

        if not changed:
            raise CommandError(message)

        record_audit_event(
            action="cluster.credential.cutover",
            object_type="cluster",
            object_id="",
            outcome="success",
            system_username="complete_credential_cutover",
            details={"stage": "completed"},
        )
        self.stdout.write(message)
        self.stdout.write(
            "Keep the legacy token in the environment until the identity contract "
            "version 1 boundary has succeeded; rollback resumes reading it."
        )

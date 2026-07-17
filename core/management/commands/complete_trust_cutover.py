from django.core.management.base import BaseCommand, CommandError

from core.services.audit_events import record_audit_event
from core.services.cluster_trust import complete_trust_cutover


class Command(BaseCommand):
    help = (
        "Seal the legacy global TLS decision into the bootstrap cluster's transport "
        "trust and stop runtime reads of PVE_CA_BUNDLE/PVE_VERIFY_TLS. Reversible by "
        "code rollback: the settings are ignored, not deleted."
    )

    def handle(self, *args, **options):
        changed, message = complete_trust_cutover()
        if not changed:
            raise CommandError(message)
        record_audit_event(
            action="cluster.trust.cutover",
            object_type="cluster",
            object_id="",
            outcome="success",
            system_username="complete_trust_cutover",
            details={"stage": "completed"},
        )
        self.stdout.write(message)
        self.stdout.write(
            "Keep the legacy TLS settings in the environment until the identity "
            "contract version 1 boundary has succeeded; rollback resumes reading them."
        )

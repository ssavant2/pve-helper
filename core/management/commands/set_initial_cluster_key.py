from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from core.services.audit_events import record_audit_event
from core.services.cluster_activation import ClusterActivationError, set_initial_cluster_key


class Command(BaseCommand):
    help = (
        "Choose the durable cluster key for the bootstrap cluster. Allowed only before "
        "cluster-qualified contracts activate; the key is immutable afterwards because it "
        "appears in URLs, queued payloads and audit history. Display names stay editable."
    )

    def add_arguments(self, parser):
        parser.add_argument("new_key", help="Lowercase, URL-safe key (a-z, 0-9, hyphen).")

    def handle(self, *args, **options):
        new_key = options["new_key"]
        try:
            cluster = set_initial_cluster_key(new_key)
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        except ClusterActivationError as exc:
            raise CommandError(str(exc)) from exc

        record_audit_event(
            action="cluster.initial_key.set",
            object_type="cluster",
            object_id=cluster.key,
            outcome="success",
            system_username="set_initial_cluster_key",
            details={"cluster_key": cluster.key, "display_name": cluster.display_name},
        )
        self.stdout.write(f"Bootstrap cluster key is now '{cluster.key}'.")

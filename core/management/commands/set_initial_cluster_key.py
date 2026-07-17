from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from core.models import ProxmoxCluster
from core.services.audit_events import record_audit_event
from core.services.cluster_activation import ClusterActivationError, set_initial_cluster_key


class Command(BaseCommand):
    help = (
        "Choose a cluster's durable key. Allowed only before cluster-qualified contracts "
        "activate; the key is immutable afterwards because it appears in URLs, queued "
        "payloads and audit history. Display names stay editable."
    )

    def add_arguments(self, parser):
        parser.add_argument("new_key", help="Lowercase, URL-safe key (a-z, 0-9, hyphen).")
        parser.add_argument(
            "--cluster",
            default="",
            help="Current key of the cluster to rekey. Optional when only one is configured.",
        )

    def handle(self, *args, **options):
        new_key = options["new_key"]
        target = (options["cluster"] or "").strip().lower()
        # Captured before the change so audit can name where this cluster came from;
        # rows written under the old key are left exactly as they were written.
        if target:
            existing = ProxmoxCluster.objects.filter(key=target).first()
        else:
            existing = ProxmoxCluster.objects.first() if ProxmoxCluster.objects.count() == 1 else None
        previous_key = existing.key if existing else ""
        try:
            cluster = set_initial_cluster_key(new_key, current_key=target or None)
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
            details={
                "cluster_key": cluster.key,
                "previous_cluster_key": previous_key,
                "display_name": cluster.display_name,
            },
        )
        self.stdout.write(f"Bootstrap cluster key is now '{cluster.key}'.")

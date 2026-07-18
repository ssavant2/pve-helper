from django.core.management.base import BaseCommand, CommandError

from core.models import ProxmoxCluster
from core.services.audit_events import record_audit_event
from core.services.cluster_activation import ClusterActivationError, enable_cluster


class Command(BaseCommand):
    help = "Enable a registered Proxmox cluster through the identity-contract gate."

    def add_arguments(self, parser):
        parser.add_argument("cluster_key")

    def handle(self, *args, **options):
        key = options["cluster_key"].strip().lower()
        cluster = ProxmoxCluster.objects.filter(key=key).first()
        if cluster is None:
            raise CommandError(f"No cluster with key '{key}'.")
        try:
            cluster = enable_cluster(cluster)
        except ClusterActivationError as exc:
            raise CommandError(str(exc)) from exc
        record_audit_event(
            action="cluster.enabled",
            object_type="cluster",
            object_id=cluster.key,
            cluster=cluster,
            system_username="enable_cluster",
            details={"cluster_key": cluster.key},
        )
        self.stdout.write(self.style.SUCCESS(f"Enabled cluster '{cluster.key}'."))

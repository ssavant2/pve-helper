from django.core.management.base import BaseCommand, CommandError

from core.models import ProxmoxCluster
from core.services.audit_events import record_audit_event
from core.services.cluster_identity import (
    ClusterIdentityError,
    observe_cluster_identity,
    reapprove_identity,
)


class Command(BaseCommand):
    help = (
        "Lift a cluster's identity quarantine by re-pinning the CA its endpoint now "
        "reports. This is the explicit human confirmation that a CA rotation or an "
        "intended re-point happened — not a re-point to the wrong cluster."
    )

    def add_arguments(self, parser):
        parser.add_argument("cluster_key", help="Cluster durable key.")
        parser.add_argument("--yes", action="store_true", help="Confirm re-pinning the currently observed CA.")

    def handle(self, *args, **options):
        cluster = ProxmoxCluster.objects.filter(key=options["cluster_key"].strip().lower()).first()
        if cluster is None:
            raise CommandError(f"No cluster with key '{options['cluster_key']}'.")

        try:
            observed = observe_cluster_identity(cluster)
        except ClusterIdentityError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            f"Cluster '{cluster.key}':\n"
            f"  pinned CA UUID : {cluster.discovered_ca_uuid or '(none)'}\n"
            f"  observed CA UUID: {observed.ca_uuid}\n"
            f"  observed fingerprint: {observed.ca_fingerprint}"
        )
        if not options["yes"]:
            self.stdout.write("Re-run with --yes to re-pin the observed CA and lift quarantine.")
            return

        previous = cluster.discovered_ca_uuid
        reapprove_identity(cluster, observed)
        record_audit_event(
            action="cluster.identity.reapprove",
            object_type="cluster",
            object_id=cluster.key,
            outcome="success",
            system_username="reapprove_cluster_identity",
            details={
                "cluster_key": cluster.key,
                "previous_ca_uuid": previous,
                "ca_uuid": observed.ca_uuid,
            },
        )
        self.stdout.write(f"Re-pinned '{cluster.key}' to CA {observed.ca_uuid}; quarantine lifted.")

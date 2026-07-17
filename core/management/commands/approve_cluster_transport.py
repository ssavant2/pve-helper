from django.core.management.base import BaseCommand, CommandError

from core.models import ClusterTransportTrust, ProxmoxCluster
from core.services.audit_events import record_audit_event
from core.services.cluster_trust import (
    TransportTrustError,
    approve_cluster_transport,
    inspect_endpoint_certificate,
)


class Command(BaseCommand):
    help = (
        "Approve a cluster's transport trust. 'public' accepts the system CA store "
        "(a publicly trusted pveproxy certificate); 'ca_pem' trusts only the CA in "
        "--ca-file. Inspects the presented certificate first, credential-free."
    )

    def add_arguments(self, parser):
        parser.add_argument("cluster_key", help="Cluster durable key.")
        parser.add_argument("mode", choices=["public", "ca_pem"])
        parser.add_argument("--ca-file", default="", help="PEM file for ca_pem mode.")

    def handle(self, *args, **options):
        cluster = ProxmoxCluster.objects.filter(key=options["cluster_key"].strip().lower()).first()
        if cluster is None:
            raise CommandError(f"No cluster with key '{options['cluster_key']}'.")

        endpoint = cluster.endpoints.filter(enabled=True).order_by("name").first()
        if endpoint is not None:
            try:
                cert = inspect_endpoint_certificate(endpoint.url)
                self.stdout.write(
                    f"Presented certificate on {endpoint.name}:\n"
                    f"  subject: {cert.subject}\n  issuer:  {cert.issuer}\n"
                    f"  sha256:  {cert.sha256_fingerprint}"
                )
            except TransportTrustError as exc:
                self.stderr.write(f"Certificate inspection failed: {exc}")

        ca_pem = ""
        if options["mode"] == "ca_pem":
            if not options["ca_file"]:
                raise CommandError("ca_pem mode needs --ca-file.")
            try:
                with open(options["ca_file"], "r", encoding="utf-8") as handle:
                    ca_pem = handle.read()
            except OSError as exc:
                raise CommandError(f"Could not read {options['ca_file']}: {exc}") from exc

        try:
            approve_cluster_transport(cluster, mode=options["mode"], ca_pem=ca_pem)
        except TransportTrustError as exc:
            raise CommandError(str(exc)) from exc

        record_audit_event(
            action="cluster.transport.approve",
            object_type="cluster",
            object_id=cluster.key,
            outcome="success",
            system_username="approve_cluster_transport",
            details={"cluster_key": cluster.key, "mode": options["mode"]},
        )
        self.stdout.write(f"Approved {options['mode']} transport trust for '{cluster.key}'.")

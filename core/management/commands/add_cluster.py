from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import ProxmoxCluster, ProxmoxEndpoint, cluster_key_validator
from core.services.audit_events import record_audit_event
from core.services.config import endpoint_name_from_url, normalize_endpoint_url


class Command(BaseCommand):
    help = (
        "Register a Proxmox cluster and its first endpoint. The cluster is created "
        "disabled: enabling a second cluster is gated on the identity contract, and "
        "this command must not bypass that. Phase 5's wizard is the supported path; "
        "this exists so the foundation can be verified against real clusters."
    )

    def add_arguments(self, parser):
        parser.add_argument("cluster_key", help="Durable, immutable key used in URLs, e.g. 'clusterb'.")
        parser.add_argument("display_name", help="Freely editable label, e.g. 'Cluster B'.")
        parser.add_argument("endpoint_url", help="First endpoint, e.g. https://pve201.example.net:8006")
        parser.add_argument("--endpoint-name", default="", help="Defaults to the URL's first hostname label.")

    def handle(self, *args, **options):
        key = options["cluster_key"].strip().lower()
        try:
            cluster_key_validator(key)
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc

        url = options["endpoint_url"].strip().rstrip("/")
        normalized = normalize_endpoint_url(url)
        if not normalized:
            raise CommandError(f"'{url}' is not a usable endpoint URL.")
        name = (options["endpoint_name"] or endpoint_name_from_url(url)).strip()

        with transaction.atomic():
            if ProxmoxCluster.objects.filter(key__iexact=key).exists():
                raise CommandError(f"A cluster with key '{key}' already exists.")

            # An endpoint is a transport, and one transport must not answer for two
            # clusters: its inventory would arrive under the wrong identity.
            owner = ProxmoxEndpoint.objects.filter(normalized_url=normalized).select_related("cluster").first()
            if owner is not None:
                owning = owner.cluster.key if owner.cluster_id else "no cluster"
                raise CommandError(
                    f"Endpoint {normalized} is already registered as '{owner.name}' ({owning}). "
                    "An endpoint may belong to only one cluster."
                )
            cluster = ProxmoxCluster.objects.create(
                key=key, display_name=options["display_name"].strip(), enabled=False
            )
            endpoint = ProxmoxEndpoint.objects.create(name=name, url=url, cluster=cluster, enabled=True)

        record_audit_event(
            action="cluster.add",
            object_type="cluster",
            object_id=cluster.key,
            outcome="success",
            system_username="add_cluster",
            details={
                "cluster_key": cluster.key,
                "display_name": cluster.display_name,
                "endpoint_name": endpoint.name,
                "endpoint_url": endpoint.normalized_url,
            },
        )
        self.stdout.write(
            f"Registered cluster '{cluster.key}' with endpoint '{endpoint.name}' ({endpoint.normalized_url}).\n"
            "It is disabled: a second cluster may only be enabled once every read, write, URL and "
            "payload boundary is cluster-qualified.\n"
            f"Next: store its API token with\n"
            f"  PVE_HELPER_TOKEN_SECRET=... manage.py set_cluster_credential {cluster.key} '<token-id>'"
        )

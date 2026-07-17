import os
import sys

from django.core.management.base import BaseCommand, CommandError

from core.models import ProxmoxCluster
from core.services.audit_events import record_audit_event
from core.services.cluster_credentials import ClusterCredentialError, set_cluster_credential
from core.services.secret_encryption import EncryptionConfigurationError


class Command(BaseCommand):
    help = (
        "Store a cluster's Proxmox API token, encrypted at rest. The secret is read "
        "from the PVE_HELPER_TOKEN_SECRET environment variable or from stdin — never "
        "from an argument, which would be visible in the process list and shell history."
    )

    # This command is part of the recovery path for a missing encryption key, so it
    # must run in exactly the state the keyring check reports. Letting that check
    # block it would make the documented recovery procedure impossible.
    requires_system_checks: list[str] = []

    def add_arguments(self, parser):
        parser.add_argument("cluster_key", help="The cluster's durable key, e.g. 'default'.")
        parser.add_argument("token_id", help="Token id, e.g. 'pve-helper@pve!pve-helper'. Not a secret.")

    def handle(self, *args, **options):
        cluster_key = options["cluster_key"].strip().lower()
        cluster = ProxmoxCluster.objects.filter(key=cluster_key).first()
        if cluster is None:
            raise CommandError(f"No cluster with key '{cluster_key}'.")

        secret = os.environ.get("PVE_HELPER_TOKEN_SECRET", "")
        if not secret:
            if sys.stdin.isatty():
                raise CommandError(
                    "Provide the token secret via the PVE_HELPER_TOKEN_SECRET environment "
                    "variable, or pipe it on stdin. It is deliberately not an argument."
                )
            secret = sys.stdin.read()
        secret = secret.strip()
        if not secret:
            raise CommandError("The token secret was empty.")

        try:
            credential = set_cluster_credential(
                cluster, token_id=options["token_id"], token_secret=secret
            )
        except (ClusterCredentialError, EncryptionConfigurationError) as exc:
            raise CommandError(str(exc)) from exc

        # Audit records that the credential changed and which key sealed it. It must
        # never carry the secret, and the token id is an identifier, not a secret.
        record_audit_event(
            action="cluster.credential.set",
            object_type="cluster",
            object_id=cluster.key,
            outcome="success",
            system_username="set_cluster_credential",
            details={
                "cluster_key": cluster.key,
                "token_id": credential.token_id,
                "encryption_key_id": credential.encryption_key_id,
            },
        )
        self.stdout.write(
            f"Stored credential for cluster '{cluster.key}' as {credential.token_id}, "
            f"sealed under encryption key '{credential.encryption_key_id}'."
        )

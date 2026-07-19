from django.core.management.base import BaseCommand, CommandError

from core.services.audit_events import record_audit_event
from core.services.cluster_credentials import (
    credentials_needing_rotation,
    rotate_credential,
)
from core.services.secret_encryption import (
    EncryptionConfigurationError,
    MissingEncryptionKeyError,
    active_key_id,
)


class Command(BaseCommand):
    help = (
        "Re-encrypt stored cluster credentials under the active encryption key. "
        "Unsealing with the old key and re-sealing with the current one is what makes "
        "a compromised key recoverable. Defaults to a dry run."
    )

    # This command is part of the recovery path for a missing encryption key, so it
    # must run in exactly the state the keyring check reports. Letting that check
    # block it would make the documented recovery procedure impossible.
    requires_system_checks: list[str] = []

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Perform the rotation. Without this the command only reports what would change.",
        )

    def handle(self, *args, **options):
        try:
            target_key = active_key_id()
        except EncryptionConfigurationError as exc:
            raise CommandError(str(exc)) from exc

        pending = credentials_needing_rotation()
        if not pending:
            self.stdout.write(f"All stored credentials are already sealed under '{target_key}'.")
            return

        self.stdout.write(f"Active encryption key: {target_key}")
        for credential in pending:
            self.stdout.write(
                f"  {credential.cluster.key}: sealed under '{credential.encryption_key_id}' "
                f"-> would re-seal under '{target_key}'"
            )

        if not options["apply"]:
            self.stdout.write(f"\nDry run: {len(pending)} credential(s) would be re-sealed. Re-run with --apply.")
            return

        rotated = []
        for credential in pending:
            previous_key_id = credential.encryption_key_id
            try:
                rotate_credential(credential)
            except MissingEncryptionKeyError as exc:
                # The old key is gone, so this secret cannot be read at all. Say so
                # plainly rather than leaving a half-rotated keyring looking finished.
                raise CommandError(
                    f"Cannot rotate '{credential.cluster.key}': {exc} Restore that key from backup/escrow and re-run."
                ) from exc
            rotated.append((credential.cluster.key, previous_key_id))

        for cluster_key, previous_key_id in rotated:
            # Metadata only: which cluster and which keys. Never secret material.
            record_audit_event(
                action="cluster.credential.rotate",
                object_type="cluster",
                object_id=cluster_key,
                outcome="success",
                system_username="rotate_encryption_keys",
                details={
                    "cluster_key": cluster_key,
                    "previous_encryption_key_id": previous_key_id,
                    "encryption_key_id": target_key,
                },
            )

        self.stdout.write(f"Re-sealed {len(rotated)} credential(s) under '{target_key}'.")

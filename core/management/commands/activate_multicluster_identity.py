from django.core.management.base import BaseCommand, CommandError

from core.services.cluster_activation import (
    ClusterActivationError,
    activate_multicluster_identity,
)


class Command(BaseCommand):
    help = (
        "Permanently activate cluster-qualified identity after verifying that no "
        "active or durable unqualified references remain."
    )

    def handle(self, *args, **options):
        try:
            state = activate_multicluster_identity()
        except ClusterActivationError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(f"Multi-cluster identity contract v{state.identity_contract_version} is active.")
        )

from django.core.management.base import BaseCommand, CommandError

from core.services.tag_actions import TagOperationQueueError, TagOperationRetryError, retry_tag_operation


class Command(BaseCommand):
    help = "Retry an interrupted/failed idempotent tag fan-out operation."

    def add_arguments(self, parser):
        parser.add_argument("event_id", type=int)

    def handle(self, *args, **options):
        try:
            task_id = retry_tag_operation(options["event_id"])
        except (TagOperationRetryError, TagOperationQueueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Queued retry {task_id}")

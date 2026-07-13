from django.core.management.base import BaseCommand, CommandError
from django_q.tasks import async_task

from core.models import AuditEvent
from core.services.task_queues import BULK_QUEUE_NAME


class Command(BaseCommand):
    help = "Retry an interrupted/failed idempotent tag fan-out operation."

    def add_arguments(self, parser):
        parser.add_argument("event_id", type=int)

    def handle(self, *args, **options):
        event = AuditEvent.objects.filter(pk=options["event_id"], action="tag.bulk_operation").first()
        if event is None:
            raise CommandError("Tag operation not found")
        details = dict(event.details or {})
        if not details.get("targets"):
            raise CommandError("Tag operation has no durable target payload")
        details["stage"] = "retry queued"
        details["failed"] = []
        details.pop("finished_at", None)
        details.pop("interrupted_at", None)
        details.pop("heartbeat_at", None)
        details.pop("error", None)
        details.pop("retryable", None)
        event.details = details
        event.outcome = "queued"
        event.save(update_fields=["details", "outcome"])
        task_id = async_task(
            "core.services.tag_actions.execute_tag_operation",
            event.id,
            q_options={"cluster": BULK_QUEUE_NAME},
        )
        event.details = {**event.details, "worker_task_id": task_id}
        event.save(update_fields=["details"])
        self.stdout.write(f"Queued retry {task_id}")

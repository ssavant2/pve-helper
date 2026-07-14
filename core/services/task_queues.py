"""Named Django-Q queues and durable queue-state inspection helpers."""

from django.core.signing import BadSignature
from django_q.models import OrmQ
from django_q.signing import SignedPackage

BULK_QUEUE_NAME = "bulk"


def queued_task_ids(task_ids: set[str], *, queue_name: str = BULK_QUEUE_NAME) -> set[str]:
    """Return requested Django-Q ids that are still present in a DB-backed queue."""
    if not task_ids:
        return set()
    queued: set[str] = set()
    for payload in OrmQ.objects.filter(key=queue_name).values_list("payload", flat=True):
        try:
            package = SignedPackage.loads(payload)
        except (BadSignature, TypeError, ValueError):
            continue
        task_id = str(package.get("id") or "") if isinstance(package, dict) else ""
        if task_id in task_ids:
            queued.add(task_id)
    return queued

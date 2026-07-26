from __future__ import annotations

from django.db import transaction

from core.models import ScheduledActionSettings

MIN_RUN_HISTORY_RETENTION_DAYS = 1
MAX_RUN_HISTORY_RETENTION_DAYS = 999
DEFAULT_RUN_HISTORY_RETENTION_DAYS = 90


def settings_record() -> ScheduledActionSettings:
    record, _created = ScheduledActionSettings.objects.get_or_create(
        pk=ScheduledActionSettings.SINGLETON_PK,
        defaults={"run_history_retention_days": DEFAULT_RUN_HISTORY_RETENTION_DAYS},
    )
    return record


def run_history_retention_days() -> int:
    return settings_record().run_history_retention_days


def update_run_history_retention(*, days: int) -> ScheduledActionSettings:
    if not MIN_RUN_HISTORY_RETENTION_DAYS <= days <= MAX_RUN_HISTORY_RETENTION_DAYS:
        raise ValueError(
            f"Run history retention must be between {MIN_RUN_HISTORY_RETENTION_DAYS} "
            f"and {MAX_RUN_HISTORY_RETENTION_DAYS} days."
        )

    with transaction.atomic():
        record, _created = ScheduledActionSettings.objects.select_for_update().get_or_create(
            pk=ScheduledActionSettings.SINGLETON_PK,
            defaults={"run_history_retention_days": DEFAULT_RUN_HISTORY_RETENTION_DAYS},
        )
        if record.run_history_retention_days != days:
            record.run_history_retention_days = days
            record.save(update_fields=["run_history_retention_days", "updated_at"])
    return record

from __future__ import annotations

import json
import logging
import re
import socket
import ssl
from datetime import UTC, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django_q.models import Schedule

from core.models import AuditEvent, LogForwarderConfiguration, LogForwardingDelivery
from core.services.log_forwarder_certificate_watch import ensure_certificate_watch_schedule
from core.services.log_forwarder_trust import (
    LogForwarderTrustError,
    assert_pinned_match,
    delivery_context,
    reset_trust,
    trust,
    trust_applies_to,
)
from core.services.public_errors import PublicMessageError

logger = logging.getLogger(__name__)

LOG_FORWARDER_CONFIGURATION_PK = 1
LOG_FORWARDER_SCHEDULE_NAME = "pve-helper log forwarding"
LOG_FORWARDER_FUNC = "core.services.log_forwarding.deliver_pending_log_events"
CLAIM_TIMEOUT = timedelta(minutes=5)
SENT_RETENTION = timedelta(days=7)
MAX_BATCH_SIZE = 200


class LogForwarderConfigurationError(PublicMessageError, ValueError):
    pass


def configuration() -> LogForwarderConfiguration:
    config, _created = LogForwarderConfiguration.objects.get_or_create(pk=LOG_FORWARDER_CONFIGURATION_PK)
    return config


def update_configuration(*, enabled: bool, host: str, port: int, transport: str) -> LogForwarderConfiguration:
    host = host.strip()
    if enabled and not host:
        raise LogForwarderConfigurationError("Host is required when log forwarding is enabled.")
    if not host or len(host) > 255 or any(character.isspace() or ord(character) < 32 for character in host):
        if host:
            raise LogForwarderConfigurationError("Host must be a DNS name or IP address without whitespace.")
    if port < 1 or port > 65535:
        raise LogForwarderConfigurationError("Port must be between 1 and 65535.")
    if transport not in LogForwarderConfiguration.Transport.values:
        raise LogForwarderConfigurationError("Choose a supported transport.")

    config = configuration()
    config.enabled = enabled
    config.host = host
    config.port = port
    config.transport = transport
    config.save(update_fields=["enabled", "host", "port", "transport", "updated_at"])
    _configure_schedule(enabled)
    # A trust approval is a statement about one destination. Re-pointing the
    # forwarder must not inherit it, or "yes, I recognise that certificate" silently
    # becomes an answer about a collector the operator never saw.
    record = trust()
    if record.mode != record.Mode.UNSET and not trust_applies_to(record, host, port):
        reset_trust(host, port)
    ensure_certificate_watch_schedule(enabled and transport == LogForwarderConfiguration.Transport.TLS)
    return config


def destination_requires_approval(config: LogForwarderConfiguration) -> bool:
    """Whether saving this configuration leaves TLS unusable until a human answers."""
    return (
        config.enabled
        and config.transport == LogForwarderConfiguration.Transport.TLS
        and not trust_applies_to(trust(), config.host, config.port)
    )


def enqueue_audit_event(event: AuditEvent) -> LogForwardingDelivery | None:
    if not LogForwarderConfiguration.objects.filter(pk=LOG_FORWARDER_CONFIGURATION_PK, enabled=True).exists():
        return None
    with transaction.atomic():
        # AuditEvent updates serialize on their own row. Locking it here also makes
        # the monotonically increasing revision explicit for future call sites.
        AuditEvent.objects.select_for_update().only("pk").get(pk=event.pk)
        last_sequence = (
            LogForwardingDelivery.objects.filter(audit_event_id=event.pk).aggregate(value=Max("sequence"))["value"] or 0
        )
        return LogForwardingDelivery.objects.create(
            audit_event_id=event.pk,
            sequence=last_sequence + 1,
            payload=_safe_payload(event),
        )


def deliver_pending_log_events() -> int:
    config = configuration()
    if not config.enabled:
        return 0

    now = timezone.now()
    LogForwardingDelivery.objects.filter(
        status=LogForwardingDelivery.Status.SENDING,
        claimed_at__lt=now - CLAIM_TIMEOUT,
    ).update(status=LogForwardingDelivery.Status.PENDING, next_attempt_at=now, claimed_at=None)
    delivery_ids = list(
        LogForwardingDelivery.objects.filter(
            status=LogForwardingDelivery.Status.PENDING,
            next_attempt_at__lte=now,
        )
        .order_by("id")
        .values_list("id", flat=True)[:MAX_BATCH_SIZE]
    )
    delivered = 0
    for delivery_id in delivery_ids:
        delivery = _claim(delivery_id)
        if delivery is None:
            continue
        try:
            _send(config, _rfc5424_message(config, delivery))
        except (OSError, ssl.SSLError, ValueError, LogForwarderTrustError) as exc:
            logger.exception("Log forwarding delivery failed", extra={"delivery_id": delivery.id})
            error_code = _failure_code(exc)
            _mark_failed(delivery.id, error_code)
            LogForwarderConfiguration.objects.filter(pk=config.pk).update(
                last_error_at=timezone.now(), last_error_code=error_code
            )
            # Preserve event order: later messages must not overtake the first
            # unavailable one and make a recovered stream misleading.
            break
        else:
            delivered += 1
            delivered_at = timezone.now()
            LogForwardingDelivery.objects.filter(pk=delivery.id).update(
                status=LogForwardingDelivery.Status.SENT,
                delivered_at=delivered_at,
                claimed_at=None,
                last_error_code="",
            )
            LogForwarderConfiguration.objects.filter(pk=config.pk).update(
                last_success_at=delivered_at, last_error_code=""
            )

    LogForwardingDelivery.objects.filter(
        status=LogForwardingDelivery.Status.SENT,
        delivered_at__lt=now - SENT_RETENTION,
    ).delete()
    return delivered


def _claim(delivery_id: int) -> LogForwardingDelivery | None:
    with transaction.atomic():
        delivery = LogForwardingDelivery.objects.select_for_update().filter(pk=delivery_id).first()
        if delivery is None or delivery.status != LogForwardingDelivery.Status.PENDING:
            return None
        delivery.status = LogForwardingDelivery.Status.SENDING
        delivery.claimed_at = timezone.now()
        delivery.attempts += 1
        delivery.save(update_fields=["status", "claimed_at", "attempts"])
        return delivery


def _mark_failed(delivery_id: int, error_code: str) -> None:
    delivery = LogForwardingDelivery.objects.filter(pk=delivery_id).only("attempts").first()
    if delivery is None:
        return
    delay_seconds = min(3600, 15 * (2 ** min(max(delivery.attempts - 1, 0), 8)))
    LogForwardingDelivery.objects.filter(pk=delivery_id).update(
        status=LogForwardingDelivery.Status.PENDING,
        claimed_at=None,
        next_attempt_at=timezone.now() + timedelta(seconds=delay_seconds),
        last_error_code=error_code,
    )


def _send(config: LogForwarderConfiguration, message: bytes) -> None:
    address = (config.host, config.port)
    with socket.create_connection(address, timeout=10) as connection:
        if config.transport == LogForwarderConfiguration.Transport.TLS:
            record = trust()
            if not trust_applies_to(record, config.host, config.port):
                raise LogForwarderTrustError(
                    "The syslog destination's TLS certificate has not been approved for this host and port."
                )
            context = delivery_context(record)
            with context.wrap_socket(connection, server_hostname=config.host) as tls_connection:
                tls_connection.settimeout(10)
                assert_pinned_match(record, tls_connection.getpeercert(binary_form=True) or b"")
                tls_connection.sendall(message)
        else:
            connection.settimeout(10)
            connection.sendall(message)


def _failure_code(exc: BaseException) -> str:
    """Name the failure the operator actually has.

    Every failure used to collapse into `syslog_connection_failed`, so "the
    collector is unreachable" and "I do not trust the collector's certificate" were
    indistinguishable in the UI while the detail stayed correctly confined to
    protected logs. They need opposite responses — fix the network, or approve the
    certificate — so they get separate names.
    """
    if isinstance(exc, LogForwarderTrustError):
        return "syslog_tls_not_approved"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "syslog_tls_verification_failed"
    if isinstance(exc, ssl.SSLError):
        return "syslog_tls_failed"
    return "syslog_connection_failed"


def _rfc5424_message(config: LogForwarderConfiguration, delivery: LogForwardingDelivery) -> bytes:
    payload = dict(delivery.payload)
    payload["event_id"] = f"audit-{delivery.audit_event_id}"
    payload["sequence"] = delivery.sequence
    severity = _severity(str(payload.get("outcome") or ""))
    priority = config.facility * 8 + severity
    timestamp = str(payload.get("timestamp") or "-")
    hostname = _header_value(socket.gethostname(), 255)
    msg_id = _header_value(str(payload.get("action") or "audit"), 32)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    line = f"<{priority}>1 {timestamp} {hostname} pve-helper - {msg_id} - {body}".encode()
    # RFC 6587 octet-counting framing is unambiguous even if the JSON message
    # contains escaped line breaks.
    return str(len(line)).encode("ascii") + b" " + line


def _safe_payload(event: AuditEvent) -> dict[str, object]:
    payload: dict[str, object] = {
        "timestamp": event.timestamp.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "action": event.action,
        "outcome": event.outcome,
        "module": event.module,
        "username": event.username,
        "source_ip": str(event.source_ip or ""),
        "cluster": event.cluster_key_snapshot,
        "object_type": event.object_type,
        "object_id": event.object_id,
    }
    # These columns are model-owned scalar projections. Arbitrary `details` is
    # intentionally excluded: historical rows can contain provider exceptions,
    # URLs or other diagnostics that must not cross the public error boundary.
    if event.storage_id:
        payload["storage_id"] = event.storage_id
    if event.path:
        payload["path"] = event.path
    if event.target_preallocation:
        payload["target_preallocation"] = event.target_preallocation
    return payload


def _severity(outcome: str) -> int:
    normalized = outcome.lower()
    if normalized in {"failed", "failure", "error", "timeout", "stale"}:
        return 3
    if normalized in {"warning", "partial", "skipped", "cancelled", "missed"}:
        return 4
    return 6


def _header_value(value: str, max_length: int) -> str:
    normalized = re.sub(r"[^\x21-\x7e]", "_", value)[:max_length]
    return normalized or "-"


def _configure_schedule(enabled: bool) -> None:
    if not enabled:
        Schedule.objects.filter(name=LOG_FORWARDER_SCHEDULE_NAME).delete()
        return
    defaults = {
        "func": LOG_FORWARDER_FUNC,
        "schedule_type": Schedule.MINUTES,
        "minutes": 1,
        "next_run": timezone.now(),
        "repeats": -1,
        "cluster": settings.Q_CLUSTER.get("name"),
    }
    schedule, created = Schedule.objects.get_or_create(name=LOG_FORWARDER_SCHEDULE_NAME, defaults=defaults)
    if not created:
        for field, value in defaults.items():
            setattr(schedule, field, value)
        schedule.save(update_fields=[*defaults.keys()])

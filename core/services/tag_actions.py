from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from django_q.tasks import async_task

from core.models import AuditEvent, CurrentGuestInventory, ProxmoxCluster
from core.services.audit_events import record_audit_event
from core.services.current_guest_inventory import update_current_guest_config
from core.services.proxmox import (
    ProxmoxAPIError,
    VerifiedGuestInventory,
    fetch_verified_guest_inventory,
)
from core.services.refs import GuestRef, RefParseError
from core.services.tag_operation_confirmation import CHANGED_CONFIRMATION_ERROR, tag_membership_fingerprint
from core.services.tag_registry import (
    mutate_registered_tags,
    registered_tags,
)
from core.services.tags import (
    TAG_COLOR_ERROR,
    TAG_NAME_ERROR,
    RegisteredTag,
    TagValidationError,
    join_tags,
    parse_tags,
    readable_foreground,
    validate_color,
    validate_tag,
)
from core.services.task_queues import BULK_QUEUE_NAME, queued_task_ids


class TagOperationQueueError(RuntimeError):
    pass


class TagOperationRetryError(RuntimeError):
    pass


def enqueue_tag_operation(event: AuditEvent) -> str:
    """Queue a prepared fan-out and make enqueue failure terminal immediately."""
    details = dict(event.details or {})
    details["stage"] = "queued"
    details["queued_at"] = timezone.now().isoformat()
    details.pop("finished_at", None)
    details.pop("interrupted_at", None)
    details.pop("heartbeat_at", None)
    details.pop("error", None)
    details.pop("retryable", None)
    event.details = details
    event.outcome = "queued"
    event.save(update_fields=["details", "outcome"])
    try:
        task_id = async_task(
            "core.services.tag_actions.execute_tag_operation",
            event.id,
            int(details.get("retry_attempt") or 0),
            q_options={"cluster": BULK_QUEUE_NAME},
        )
    except Exception as exc:
        details = {
            **details,
            "stage": "enqueue failed",
            "error": "The background task could not be queued; retry is safe.",
            "queue_error_type": exc.__class__.__name__,
            "retryable": True,
            "finished_at": timezone.now().isoformat(),
        }
        event.details = details
        event.outcome = "failed"
        event.save(update_fields=["details", "outcome"])
        raise TagOperationQueueError(details["error"]) from exc
    event.details = {**details, "worker_task_id": task_id}
    event.save(update_fields=["details"])
    return task_id


def retry_tag_operation(event_id: int) -> str:
    """Atomically claim and safely requeue one failed idempotent fan-out."""
    with transaction.atomic():
        event = AuditEvent.objects.select_for_update().filter(pk=event_id, action="tag.bulk_operation").first()
        if event is None:
            raise TagOperationRetryError("Tag operation not found.")
        details = dict(event.details or {})
        if event.outcome != "failed" or not details.get("retryable"):
            raise TagOperationRetryError("This tag operation is not available for retry.")
        if not details.get("targets"):
            raise TagOperationRetryError("Tag operation has no durable target payload.")
        worker_task_id = str(details.get("worker_task_id") or "")
        if worker_task_id and worker_task_id in queued_task_ids({worker_task_id}):
            raise TagOperationRetryError("The original background task is still queued.")

        details["retry_attempt"] = int(details.get("retry_attempt") or 0) + 1
        details["stage"] = "retry requested"
        details["failed"] = []
        details.pop("finished_at", None)
        details.pop("interrupted_at", None)
        details.pop("heartbeat_at", None)
        details.pop("error", None)
        details.pop("queue_error", None)
        details.pop("queue_error_type", None)
        details.pop("retryable", None)
        details.pop("worker_task_id", None)
        event.details = details
        event.outcome = "queued"
        event.save(update_fields=["details", "outcome"])

    return enqueue_tag_operation(event)


def register_tag(tag: str, color: str = "", *, cluster) -> tuple[dict[str, RegisteredTag], str]:
    try:
        tag = validate_tag(tag)
    except TagValidationError:
        return {}, TAG_NAME_ERROR
    try:
        color = validate_color(color)
    except TagValidationError:
        return {}, TAG_COLOR_ERROR

    def mutate(names, colors):
        if tag not in names:
            names.append(tag)
        if color:
            colors[tag] = (color, readable_foreground(color))

    return mutate_registered_tags(
        mutate,
        postcondition=lambda actual: tag in actual and (not color or actual[tag].background == color),
        cluster=cluster,
    )


def recolor_tag(tag: str, color: str, *, cluster) -> tuple[dict[str, RegisteredTag], str]:
    try:
        tag = validate_tag(tag)
    except TagValidationError:
        return {}, TAG_NAME_ERROR
    try:
        color = validate_color(color)
    except TagValidationError:
        return {}, TAG_COLOR_ERROR

    def mutate(names, colors):
        if tag not in names:
            raise TagValidationError("Register the tag before assigning a color.")
        colors[tag] = (color, readable_foreground(color))

    try:
        return mutate_registered_tags(
            mutate,
            postcondition=lambda actual: tag in actual and actual[tag].background == color,
            cluster=cluster,
        )
    except TagValidationError:
        return {}, "Register the tag before assigning a color."


def unregister_tag(tag: str, *, cluster) -> tuple[dict[str, RegisteredTag], str]:
    try:
        tag = validate_tag(tag)
    except TagValidationError:
        return {}, TAG_NAME_ERROR

    def mutate(names, colors):
        names[:] = [name for name in names if name != tag]
        colors.pop(tag, None)

    return mutate_registered_tags(mutate, postcondition=lambda actual: tag not in actual, cluster=cluster)


def _target_from_guest(row, *, cluster) -> dict:
    cluster_key = getattr(getattr(row, "cluster", None), "key", "") or getattr(cluster, "key", "")
    ref = GuestRef(cluster_key, row.object_type, row.vmid, row.node)
    return {
        "guest_ref": ref.serialize(),
        "cluster_key": cluster_key,
        "node": row.node,
        "object_type": row.object_type,
        "vmid": row.vmid,
        "name": row.name,
    }


def latest_tag_targets(tag: str, *, cluster) -> tuple[list[dict], VerifiedGuestInventory]:
    """Union retained membership with an explicitly covered live inventory."""
    targets: dict[tuple[str, str, str, int], dict] = {}
    for row in CurrentGuestInventory.objects.filter(cluster=cluster):
        if tag in parse_tags(row.config):
            target = _target_from_guest(row, cluster=cluster)
            targets[(target["cluster_key"], target["node"], target["object_type"], target["vmid"])] = target
    live = fetch_verified_guest_inventory(cluster=cluster)
    for row in live.guests:
        if tag in parse_tags(row.tags):
            target = _target_from_guest(row, cluster=cluster)
            targets[(target["cluster_key"], target["node"], target["object_type"], target["vmid"])] = target
    return sorted(targets.values(), key=lambda item: (item["object_type"], item["vmid"], item["node"])), live


def prepare_tag_operation(
    event: AuditEvent,
    *,
    operation: str,
    source_tag: str,
    confirmed_membership_fingerprint: str,
    new_tag: str = "",
    cluster_key: str = "",
) -> str:
    try:
        source_tag = validate_tag(source_tag)
        new_tag = validate_tag(new_tag) if new_tag else ""
    except TagValidationError:
        return TAG_NAME_ERROR
    cluster = ProxmoxCluster.objects.filter(key=cluster_key).first()
    if cluster is None:
        return "The selected Proxmox cluster no longer exists."
    registered, error = registered_tags(cluster=cluster)
    if error:
        return error
    targets, membership = latest_tag_targets(source_tag, cluster=cluster)
    if tag_membership_fingerprint(targets) != confirmed_membership_fingerprint:
        return CHANGED_CONFIRMATION_ERROR
    if not targets and not membership.complete:
        return "Could not verify tag membership on every Proxmox endpoint; no changes were made."
    if operation == "rename":
        if new_tag in registered:
            return "The destination tag is already registered."
        old = registered.get(source_tag)
        _updated, error = register_tag(new_tag, old.background if old else "", cluster=cluster)
        if error:
            return error
    username = event.username or str((event.details or {}).get("username") or "")
    event.details = {
        "operation": operation,
        "cluster_key": cluster.key,
        "source_tag": source_tag,
        "new_tag": new_tag,
        "targets": targets,
        "succeeded": [],
        "skipped": [],
        "failed": [],
        "membership_complete": membership.complete,
        "membership_errors": list(membership.errors),
        "stage": "queued",
        "username": username,
    }
    event.outcome = "queued"
    event.cluster = cluster
    event.cluster_key_snapshot = cluster.key
    event.save(update_fields=["details", "outcome", "cluster", "cluster_key_snapshot"])
    if not targets:
        verification = fetch_verified_guest_inventory(cluster=cluster)
        remaining = [
            _target_from_guest(item, cluster=cluster)
            for item in verification.guests
            if source_tag in parse_tags(item.tags)
        ]
        event.details = {
            **event.details,
            "postcondition_complete": verification.complete,
            "postcondition_errors": list(verification.errors),
            "remaining_targets": remaining,
        }
        if not verification.complete:
            error = "Could not verify tag membership on every Proxmox endpoint."
        elif remaining:
            error = f"The source tag is still assigned to {len(remaining)} guest(s)."
        else:
            _updated, error = unregister_tag(source_tag, cluster=cluster)
        if error:
            event.outcome = "failed"
            event.details = {**event.details, "error": error, "finished_at": timezone.now().isoformat()}
        else:
            event.outcome = "success"
            event.details = {**event.details, "stage": "completed", "finished_at": timezone.now().isoformat()}
        event.save(update_fields=["details", "outcome"])
        if event.outcome == "success":
            _record_summary_audit(event)
    return ""


def _target_key(target: dict) -> str:
    try:
        return GuestRef.parse(str(target.get("guest_ref") or "")).serialize()
    except RefParseError:
        # Bounded reader for a queued pre-Phase-3 tag fan-out. New writers always
        # persist guest_ref; activation verifies that no legacy payload remains.
        return GuestRef(
            str(target.get("cluster_key") or ""),
            str(target.get("object_type") or ""),
            int(target.get("vmid") or 0),
            str(target.get("node") or ""),
        ).serialize()


def execute_tag_operation(event_id: int, retry_attempt: int | None = None) -> None:
    event = AuditEvent.objects.get(pk=event_id)
    details = dict(event.details or {})
    expected_attempt = int(details.get("retry_attempt") or 0)
    received_attempt = 0 if retry_attempt is None else int(retry_attempt)
    if received_attempt != expected_attempt or event.outcome != "queued":
        return
    details["stage"] = "updating guests"
    details["heartbeat_at"] = timezone.now().isoformat()
    event.outcome = "running"
    event.details = details
    event.save(update_fields=["details", "outcome"])
    terminal = {_target_key(item) for bucket in ("succeeded", "skipped") for item in details.get(bucket, [])}
    details["failed"] = []
    cluster = ProxmoxCluster.objects.filter(key=str(details.get("cluster_key") or "")).first()
    if cluster is None:
        event.outcome = "failed"
        details["stage"] = "failed"
        details["error"] = "The selected Proxmox cluster no longer exists."
        details["finished_at"] = timezone.now().isoformat()
        event.details = details
        event.save(update_fields=["details", "outcome"])
        return
    inventory = fetch_verified_guest_inventory(cluster=cluster)
    live_by_identity = {(item.node, item.object_type, item.vmid): item for item in inventory.guests}
    live_by_guest: dict[tuple[str, int], list] = {}
    for item in inventory.guests:
        live_by_guest.setdefault((item.object_type, item.vmid), []).append(item)
    for target in details.get("targets", []):
        if _target_key(target) in terminal:
            continue
        live_guest = live_by_identity.get((target["node"], target["object_type"], target["vmid"]))
        if live_guest is None:
            candidates = live_by_guest.get((target["object_type"], target["vmid"]), [])
            live_guest = candidates[0] if len(candidates) == 1 else None
        outcome, message = _update_target(details, target, live_guest, cluster=cluster)
        item = {**target, "reason": message} if message else dict(target)
        details.setdefault(outcome, []).append(item)
        details["heartbeat_at"] = timezone.now().isoformat()
        event.details = details
        event.save(update_fields=["details"])
    verification = fetch_verified_guest_inventory(cluster=cluster)
    details["postcondition_complete"] = verification.complete
    details["postcondition_errors"] = list(verification.errors)
    remaining = [
        _target_from_guest(item, cluster=cluster)
        for item in verification.guests
        if details["source_tag"] in parse_tags(item.tags)
    ]
    details["remaining_targets"] = remaining
    if not verification.complete:
        details.setdefault("failed", []).append(
            {"registry": True, "reason": "Could not verify tag membership on every Proxmox endpoint."}
        )
    elif remaining:
        details.setdefault("failed", []).append(
            {
                "registry": True,
                "reason": f"The source tag is still assigned to {len(remaining)} guest(s).",
            }
        )
    if details.get("failed"):
        event.outcome = "failed"
        details["stage"] = "partial failure"
        details["retryable"] = True
    else:
        _updated, error = unregister_tag(details["source_tag"], cluster=cluster)
        if error:
            event.outcome = "failed"
            details.setdefault("failed", []).append({"reason": error, "registry": True})
            details["retryable"] = True
        else:
            event.outcome = "success"
            details["stage"] = "completed"
    details["finished_at"] = timezone.now().isoformat()
    event.details = details
    event.save(update_fields=["details", "outcome"])
    if event.outcome == "success":
        _record_summary_audit(event)


def _update_target(details: dict, target: dict, live_guest, *, cluster) -> tuple[str, str]:
    target_cluster_key = str(target.get("cluster_key") or "")
    if target_cluster_key and target_cluster_key != cluster.key:
        return "failed", "Target belongs to a different Proxmox cluster."
    if live_guest is None:
        return "failed", "Guest was not found in live inventory."
    node = live_guest.node
    # Asking every configured client "do you have this vmid on this node?" is a
    # cross-cluster search: two clusters may each hold vm:500 on a node called
    # pve1, and the first to answer would win. Candidates are bounded to the
    # selected cluster, where an endpoint answering only proves transport.
    from core.services.cluster_resolver import ClusterResolutionError, cluster_clients

    try:
        candidates = cluster_clients(cluster)
    except ClusterResolutionError as exc:
        return "failed", str(exc)

    client = None
    config = None
    for candidate in candidates:
        try:
            config = candidate.guest_config(node=node, object_type=target["object_type"], vmid=target["vmid"])
            client = candidate
            break
        except ProxmoxAPIError:
            continue
    if client is None or config is None:
        return "failed", "Could not read current guest config."
    if config.get("lock"):
        return "failed", f"Guest is locked ({config['lock']})."
    current = parse_tags(config)
    source = details["source_tag"]
    if source not in current:
        return "skipped", "Already in desired state."
    if details["operation"] == "rename":
        next_tags = [details["new_tag"] if tag == source else tag for tag in current]
    else:
        next_tags = [tag for tag in current if tag != source]
    try:
        client.set_guest_config(
            node=node,
            object_type=target["object_type"],
            vmid=target["vmid"],
            updates={"tags": join_tags(next_tags)} if next_tags else {},
            delete=[] if next_tags else ["tags"],
            digest=config.get("digest"),
        )
    except ProxmoxAPIError as exc:
        return "failed", str(exc)
    update_current_guest_config(
        object_type=target["object_type"],
        vmid=target["vmid"],
        node=node,
        updates={"tags": join_tags(next_tags)} if next_tags else {},
        delete=[] if next_tags else ["tags"],
        cluster=cluster,
    )
    record_audit_event(
        username=event_username(details),
        action="tag.membership.renamed" if details["operation"] == "rename" else "tag.membership.removed",
        object_type="guest",
        cluster=cluster,
        guest_ref=GuestRef.parse(_target_key(target)),
        details={"source_tag": source, "new_tag": details.get("new_tag", ""), **target},
    )
    return "succeeded", ""


def event_username(details: dict) -> str:
    return str(details.get("username") or "system")


def _record_summary_audit(operation_event: AuditEvent) -> None:
    details = dict(operation_event.details or {})
    if details.get("summary_audit_id"):
        return
    operation = details.get("operation")
    summary = record_audit_event(
        username=event_username(details),
        action="tag.renamed" if operation == "rename" else "tag.deleted",
        object_type="tag",
        object_id=str(details.get("source_tag") or operation_event.object_id),
        cluster=operation_event.cluster,
        cluster_key_snapshot=operation_event.cluster_key_snapshot,
        details={
            "source_tag": details.get("source_tag", ""),
            "new_tag": details.get("new_tag", ""),
            "affected_count": len(details.get("targets", [])),
            "operation_event_id": operation_event.id,
        },
    )
    details["summary_audit_id"] = summary.id
    operation_event.details = details
    operation_event.save(update_fields=["details"])

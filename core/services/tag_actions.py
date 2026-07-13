from __future__ import annotations

from django.utils import timezone
from django.core.cache import cache

from core.models import AuditEvent, ProxmoxInventory, ScanRun
from core.services.proxmox import (
    ProxmoxAPIError,
    VerifiedGuestInventory,
    configured_clients,
    fetch_verified_guest_inventory,
)
from core.services.tags import (
    RegisteredTag,
    TagValidationError,
    join_tags,
    parse_color_map,
    parse_registered_tags,
    parse_tag_style,
    parse_tags,
    readable_foreground,
    serialize_color_map,
    serialize_tag_style,
    validate_color,
    validate_tag,
)


TAG_REGISTRY_CACHE_KEY = "pve-helper:tag-registry:v1"
TAG_REGISTRY_CACHE_SECONDS = 60


def cluster_options() -> tuple[object | None, dict, str]:
    error = "No Proxmox endpoint could read cluster tag options."
    for client in configured_clients():
        try:
            return client, client.cluster_options(), ""
        except ProxmoxAPIError as exc:
            error = str(exc)
    return None, {}, error


def registered_tags() -> tuple[dict[str, RegisteredTag], str]:
    cached = cache.get(TAG_REGISTRY_CACHE_KEY)
    if isinstance(cached, tuple) and len(cached) == 2:
        return cached
    _client, options, error = cluster_options()
    result = (parse_registered_tags(options), error)
    if not error:
        cache.set(TAG_REGISTRY_CACHE_KEY, result, TAG_REGISTRY_CACHE_SECONDS)
    return result


def _write_registry(mutator) -> tuple[dict[str, RegisteredTag], str]:
    client, options, error = cluster_options()
    if client is None:
        return {}, error
    names = list(parse_registered_tags(options))
    style = parse_tag_style(options.get("tag-style"))
    colors = parse_color_map(style.get("color-map", ""))
    mutator(names, colors)
    names = sorted(dict.fromkeys(names))
    updates, delete = {}, []
    if names:
        updates["registered-tags"] = join_tags(names)
    else:
        delete.append("registered-tags")
    if colors:
        style["color-map"] = serialize_color_map(colors)
    else:
        style.pop("color-map", None)
    serialized_style = serialize_tag_style(style)
    if serialized_style:
        updates["tag-style"] = serialized_style
    elif options.get("tag-style") is not None:
        delete.append("tag-style")
    try:
        client.set_cluster_options(updates, delete=delete)
        refreshed = dict(options)
        refreshed.update(updates)
        for key in delete:
            refreshed.pop(key, None)
        result = parse_registered_tags(refreshed)
        cache.set(TAG_REGISTRY_CACHE_KEY, (result, ""), TAG_REGISTRY_CACHE_SECONDS)
        return result, ""
    except ProxmoxAPIError as exc:
        return {}, str(exc)


def register_tag(tag: str, color: str = "") -> tuple[dict[str, RegisteredTag], str]:
    try:
        tag = validate_tag(tag)
        color = validate_color(color)
    except TagValidationError as exc:
        return {}, str(exc)

    def mutate(names, colors):
        if tag not in names:
            names.append(tag)
        if color:
            colors[tag] = (color, readable_foreground(color))

    return _write_registry(mutate)


def recolor_tag(tag: str, color: str) -> tuple[dict[str, RegisteredTag], str]:
    try:
        tag, color = validate_tag(tag), validate_color(color)
    except TagValidationError as exc:
        return {}, str(exc)

    def mutate(names, colors):
        if tag not in names:
            raise TagValidationError("Register the tag before assigning a color.")
        colors[tag] = (color, readable_foreground(color))

    try:
        return _write_registry(mutate)
    except TagValidationError as exc:
        return {}, str(exc)


def unregister_tag(tag: str) -> tuple[dict[str, RegisteredTag], str]:
    try:
        tag = validate_tag(tag)
    except TagValidationError as exc:
        return {}, str(exc)

    def mutate(names, colors):
        names[:] = [name for name in names if name != tag]
        colors.pop(tag, None)

    return _write_registry(mutate)


def _target_from_guest(row) -> dict:
    return {"node": row.node, "object_type": row.object_type, "vmid": row.vmid, "name": row.name}


def _latest_guest_snapshot() -> ScanRun | None:
    return (
        ScanRun.objects.filter(
            proxmox_objects__object_type__in=[
                ProxmoxInventory.ObjectType.VM,
                ProxmoxInventory.ObjectType.CT,
            ],
            proxmox_objects__vmid__isnull=False,
        )
        .order_by("-created_at")
        .distinct()
        .first()
    )


def latest_tag_targets(tag: str) -> tuple[list[dict], VerifiedGuestInventory]:
    """Union retained membership with an explicitly covered live inventory."""
    targets: dict[tuple[str, str, int], dict] = {}
    scan = _latest_guest_snapshot()
    if scan is not None:
        for row in ProxmoxInventory.objects.filter(
            scan_run=scan,
            object_type__in=[ProxmoxInventory.ObjectType.VM, ProxmoxInventory.ObjectType.CT],
        ):
            if tag in parse_tags(row.config):
                target = _target_from_guest(row)
                targets[(target["node"], target["object_type"], target["vmid"])] = target
    live = fetch_verified_guest_inventory()
    for row in live.guests:
        if tag in parse_tags(row.tags):
            target = _target_from_guest(row)
            targets[(target["node"], target["object_type"], target["vmid"])] = target
    return sorted(targets.values(), key=lambda item: (item["object_type"], item["vmid"], item["node"])), live


def prepare_tag_operation(event: AuditEvent, *, operation: str, source_tag: str, new_tag: str = "") -> str:
    try:
        source_tag = validate_tag(source_tag)
        new_tag = validate_tag(new_tag) if new_tag else ""
    except TagValidationError as exc:
        return str(exc)
    registered, error = registered_tags()
    if error:
        return error
    targets, membership = latest_tag_targets(source_tag)
    if not targets and not membership.complete:
        return "Could not verify tag membership on every Proxmox endpoint; no changes were made."
    if operation == "rename":
        if new_tag in registered:
            return "The destination tag is already registered."
        old = registered.get(source_tag)
        _updated, error = register_tag(new_tag, old.background if old else "")
        if error:
            return error
    username = event.username or str((event.details or {}).get("username") or "")
    event.details = {
        "operation": operation,
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
    event.save(update_fields=["details", "outcome"])
    if not targets:
        verification = fetch_verified_guest_inventory()
        remaining = [
            _target_from_guest(item)
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
            _updated, error = unregister_tag(source_tag)
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
    return f"{target.get('object_type')}:{target.get('vmid')}@{target.get('node')}"


def execute_tag_operation(event_id: int) -> None:
    event = AuditEvent.objects.get(pk=event_id)
    details = dict(event.details or {})
    details["stage"] = "updating guests"
    details["heartbeat_at"] = timezone.now().isoformat()
    event.outcome = "running"
    event.details = details
    event.save(update_fields=["details", "outcome"])
    terminal = {
        _target_key(item)
        for bucket in ("succeeded", "skipped")
        for item in details.get(bucket, [])
    }
    details["failed"] = []
    inventory = fetch_verified_guest_inventory()
    live_by_identity = {
        (item.node, item.object_type, item.vmid): item
        for item in inventory.guests
    }
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
        outcome, message = _update_target(details, target, live_guest)
        item = {**target, "reason": message} if message else dict(target)
        details.setdefault(outcome, []).append(item)
        details["heartbeat_at"] = timezone.now().isoformat()
        event.details = details
        event.save(update_fields=["details"])
    verification = fetch_verified_guest_inventory()
    details["postcondition_complete"] = verification.complete
    details["postcondition_errors"] = list(verification.errors)
    remaining = [
        _target_from_guest(item)
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
    else:
        _updated, error = unregister_tag(details["source_tag"])
        if error:
            event.outcome = "failed"
            details.setdefault("failed", []).append({"reason": error, "registry": True})
        else:
            event.outcome = "success"
            details["stage"] = "completed"
    details["finished_at"] = timezone.now().isoformat()
    event.details = details
    event.save(update_fields=["details", "outcome"])
    if event.outcome == "success":
        _record_summary_audit(event)


def _update_target(details: dict, target: dict, live_guest) -> tuple[str, str]:
    if live_guest is None:
        return "failed", "Guest was not found in live inventory."
    node = live_guest.node
    client = None
    config = None
    for candidate in configured_clients():
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
    scan = ScanRun.objects.filter(status=ScanRun.Status.COMPLETED).order_by("-created_at").first()
    if scan:
        row = ProxmoxInventory.objects.filter(
            scan_run=scan,
            object_type=target["object_type"],
            vmid=target["vmid"],
        ).first()
        if row:
            row.config = {**(row.config or {}), "tags": join_tags(next_tags)}
            row.node = node
            if not next_tags:
                row.config.pop("tags", None)
            row.save(update_fields=["config", "node", "updated_at"])
    AuditEvent.objects.create(
        username=event_username(details),
        action="tag.renamed" if details["operation"] == "rename" else "tag.removed",
        object_type="guest",
        object_id=_target_key(target),
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
    summary = AuditEvent.objects.create(
        username=event_username(details),
        action="tag.renamed" if operation == "rename" else "tag.deleted",
        object_type="tag",
        object_id=str(details.get("source_tag") or operation_event.object_id),
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

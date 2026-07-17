from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from django.core import signing


TAG_OPERATION_CONFIRMATION_SALT = "pve-helper.tag-operation-confirmation.v1"
TAG_OPERATION_CONFIRMATION_MAX_AGE_SECONDS = 15 * 60

INVALID_CONFIRMATION_ERROR = (
    "This confirmation is invalid or has expired. Reload the tag page and confirm the operation again."
)
CHANGED_CONFIRMATION_ERROR = (
    "Tag membership changed after the confirmation was shown. Review the updated object count and confirm again."
)


def _target_identity(target) -> tuple[str, str, str, int]:
    if isinstance(target, Mapping):
        return (
            str(target.get("cluster_key") or ""),
            str(target.get("node") or ""),
            str(target.get("object_type") or ""),
            int(target.get("vmid") or 0),
        )
    return (
        str(
            getattr(target, "cluster_key", "")
            or getattr(getattr(target, "cluster", None), "key", "")
        ),
        str(getattr(target, "node", "") or ""),
        str(getattr(target, "object_type", "") or ""),
        int(getattr(target, "vmid", 0) or 0),
    )


def tag_membership_fingerprint(targets: Iterable) -> str:
    identities = sorted({_target_identity(target) for target in targets})
    serialized = json.dumps(identities, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode()).hexdigest()


@dataclass(frozen=True)
class ConfirmedTagOperation:
    cluster_key: str
    operation: str
    tag: str
    guest_count: int
    membership_fingerprint: str


def issue_tag_operation_confirmation(
    *, operation: str, tag: str, summary, user_id, cluster_key: str
) -> str:
    if operation not in {"delete", "rename"}:
        raise ValueError("Unsupported tag operation confirmation")
    payload = {
        "version": 2,
        "cluster_key": cluster_key,
        "operation": operation,
        "tag": tag,
        "user_id": str(user_id),
        "guest_count": summary.guest_count,
        "membership_fingerprint": tag_membership_fingerprint(summary.guests),
        "registered": bool(summary.registered),
    }
    return signing.dumps(payload, salt=TAG_OPERATION_CONFIRMATION_SALT, compress=True)


def validate_tag_operation_confirmation(
    token: str,
    *,
    operation: str,
    tag: str,
    summary,
    user_id,
    cluster_key: str,
) -> tuple[ConfirmedTagOperation | None, str]:
    try:
        payload = signing.loads(
            token,
            salt=TAG_OPERATION_CONFIRMATION_SALT,
            max_age=TAG_OPERATION_CONFIRMATION_MAX_AGE_SECONDS,
        )
    except (signing.BadSignature, signing.SignatureExpired):
        return None, INVALID_CONFIRMATION_ERROR

    if not isinstance(payload, dict) or any(
        (
            payload.get("version") != 2,
            payload.get("cluster_key") != cluster_key,
            payload.get("operation") != operation,
            payload.get("tag") != tag,
            payload.get("user_id") != str(user_id),
        )
    ):
        return None, INVALID_CONFIRMATION_ERROR

    current_count = summary.guest_count if summary is not None else 0
    current_fingerprint = tag_membership_fingerprint(summary.guests if summary is not None else ())
    current_registered = bool(summary and summary.registered)
    if any(
        (
            summary is None,
            payload.get("guest_count") != current_count,
            payload.get("membership_fingerprint") != current_fingerprint,
            payload.get("registered") != current_registered,
        )
    ):
        return None, CHANGED_CONFIRMATION_ERROR

    return (
        ConfirmedTagOperation(
            cluster_key=cluster_key,
            operation=operation,
            tag=tag,
            guest_count=current_count,
            membership_fingerprint=current_fingerprint,
        ),
        "",
    )

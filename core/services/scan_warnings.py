from __future__ import annotations

from typing import Any

_PROXMOX_ACTION_LABELS = {
    "qemu.list": "QEMU inventory",
    "lxc.list": "LXC inventory",
}


def scan_warning_summary(error_details: Any, *, max_groups: int = 3) -> str:
    """Return a compact operator-facing summary of scan errors.

    Persisted scan payloads predate the public error boundary and may contain raw
    provider or Python exception text.  Treat every prose field as untrusted here:
    the summary uses only structured resource/operation context and text owned by
    this formatter.
    """
    if not isinstance(error_details, dict) or not error_details:
        return ""

    groups: list[str] = []
    proxmox_errors = error_details.get("proxmox")
    if isinstance(proxmox_errors, dict):
        for node, value in proxmox_errors.items():
            groups.append(_proxmox_warning(str(node), value))

    storage_errors = error_details.get("storage")
    if isinstance(storage_errors, dict):
        for storage_id, value in storage_errors.items():
            groups.append(_storage_warning(str(storage_id), value))

    if not groups:
        groups.append("The storage scan did not complete.")

    groups = [group for group in groups if group]
    shown = groups[: max(1, max_groups)]
    remaining = len(groups) - len(shown)
    if remaining:
        shown.append(f"and {remaining} more warning(s)")
    return "; ".join(shown)


def _proxmox_warning(node: str, value: Any) -> str:
    errors = _error_items(value)
    actions = _unique(
        _PROXMOX_ACTION_LABELS.get(str(item.get("action") or ""), str(item.get("action") or ""))
        for item in errors
        if item.get("action")
    )
    subject = _joined(actions)
    if subject:
        return f"{node}: {subject} could not be read"
    return f"{node}: Proxmox inventory could not be read"


def _storage_warning(storage_id: str, value: Any) -> str:
    errors = _error_items(value)
    paths = _unique(str(item.get("path") or "") for item in errors if item.get("path"))
    shown_paths = paths[:2]
    summary = f"{storage_id}: storage content could not be read"
    if shown_paths:
        summary += f" at {_joined(shown_paths)}"
    remaining = len(paths) - len(shown_paths)
    if remaining > 0:
        summary += f" and {remaining} more path(s)"
    return summary


def _error_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict) and isinstance(value.get("errors"), list):
        value = value["errors"]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _unique(values) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _joined(values: list[str]) -> str:
    if len(values) < 2:
        return values[0] if values else ""
    return f"{', '.join(values[:-1])} and {values[-1]}"

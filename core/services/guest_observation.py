"""Whether a guest's runtime was observed at all, kept apart from what it said.

Three different sources feed a guest's read model, and only one of them can go
silent in a way that looks like a value:

* The hypervisor answers power state, CPU, memory and uptime for *every* guest,
  agent or no agent — it owns the QEMU process, so it always knows. When these
  are missing, a node did not answer.
* The QEMU guest agent adds in-guest facts (OS name, hostname, IPs, filesystem).
  It is optional, and `core.services.guest_agent_info` already fails soft, so a
  guest without one is fully reported apart from those extras. A missing agent
  never makes power state unknown.
* The stored config supplies the totals (vCPU count, memory size, template flag).

So `unknown` is not a power state. `cluster/resources` does report `unknown` for
a RAM-suspended VM, but that case is resolved to `paused` in
`core.services.proxmox` before it ever reaches a read model. What still reads
`unknown` here is the absence of an answer, and it has to be published as absence
— a guest on an unreachable node is not "stopped", not "healthy" and not "0%".
"""

from __future__ import annotations

UNOBSERVED_STATUSES = frozenset({"", "unknown"})

UNOBSERVED_LABEL = "Unknown"
UNOBSERVED_VALUE_TEXT = "Not observed"

UNOBSERVED_EXPLANATION = (
    "No node answered for this guest, so its power state and usage are unknown "
    "rather than idle. Everything below comes from the last successful scan and "
    "may no longer be true."
)


def runtime_is_observed(status: str | None, runtime_observed_at=None) -> bool:
    """Whether the hypervisor actually reported this guest's runtime.

    Both halves matter: a guest that has never been scanned has no
    `runtime_observed_at`, while a guest on a node that stopped answering keeps
    an old timestamp from the sweep that noticed and writes `unknown`.
    """
    if runtime_observed_at is None:
        return False
    return str(status or "").strip() not in UNOBSERVED_STATUSES


def unobserved_scope_label(node: str = "", cluster_label: str = "") -> str:
    """Name what went silent, so the operator knows where to look.

    "Unknown" alone sends someone to the guest; naming the node sends them to the
    thing that is actually down.
    """
    if node and cluster_label:
        return f"node {node} in {cluster_label}"
    if node:
        return f"node {node}"
    if cluster_label:
        return cluster_label
    return "its node"

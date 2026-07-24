"""One vocabulary for "this cluster is managed but not operational".

Navigation, the per-cluster pages and their banners must not each invent their own
phrasing for the same two states. A disabled or quarantined cluster still owns its
retained inventory, schedules and Audit history and is reachable, so it needs a
label that says *why the buttons will refuse*, not one that hides it.

Retirement is deliberately absent: a retired cluster is not degraded, it is out of
the managed scope entirely and never reaches these surfaces.
"""

from __future__ import annotations

from core.models import ProxmoxCluster

DISABLED_LABEL = "Disabled"
QUARANTINED_LABEL = "Quarantined"

DISABLED_EXPLANATION = (
    "This connection is disabled, so refreshes, schedules, consoles and writes are "
    "refused. Retained inventory, schedules and history stay readable below and may "
    "be out of date."
)
QUARANTINED_EXPLANATION = (
    "This connection is quarantined because its cluster CA no longer matches the "
    "approved identity, so ingestion and writes are halted. Retained inventory, "
    "schedules and history stay readable below and may be out of date."
)


def cluster_degraded_label(cluster: ProxmoxCluster | None) -> str:
    """Short badge text, or an empty string when the cluster is fully operational.

    Quarantine outranks disabled: it is the state that says the stored identity is
    in doubt, and an operator who re-enables without re-approving has fixed nothing.
    """
    if cluster is None:
        return ""
    if cluster.ingestion_quarantined:
        return QUARANTINED_LABEL
    if not cluster.enabled:
        return DISABLED_LABEL
    return ""


def cluster_degraded_explanation(cluster: ProxmoxCluster | None) -> str:
    """The sentence that goes with the badge, or an empty string."""
    label = cluster_degraded_label(cluster)
    if label == QUARANTINED_LABEL:
        return QUARANTINED_EXPLANATION
    if label == DISABLED_LABEL:
        return DISABLED_EXPLANATION
    return ""


def cluster_degraded_context(cluster: ProxmoxCluster | None) -> dict:
    """Template context for the shared degraded-cluster notice partial."""
    return {
        "cluster_degraded_label": cluster_degraded_label(cluster),
        "cluster_degraded_explanation": cluster_degraded_explanation(cluster),
    }

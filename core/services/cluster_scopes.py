"""The three read scopes over ``ProxmoxCluster``, stated once and named.

Retirement makes "which clusters does this query mean" a decision with three
distinct answers, and overloading one helper with all three is how a retention
writer silently deletes a retired cluster's history or a provider call reaches a
row that must never be contacted. So each meaning is its own resolver:

* **managed** — ``retired_at IS NULL``: the live rows. Includes enabled, disabled
  and quarantined clusters, because their last-known read models and history stay
  readable as visibly stale. This is the scope for every ordinary selector, the
  navigation tree, and identity-uniqueness checks.
* **provider-acquirable** — managed *and* enabled *and* not quarantined *and*
  carrying the credential and transport trust a live call needs. The only scope
  allowed to originate a request to Proxmox. It is deliberately a subset of
  managed: a disabled or quarantined cluster is still managed (readable) but never
  acquirable.
* **historical** — every row, retired included. Used by the Connections archive,
  Audit, retained-history readers **and every retention/cleanup writer**. A writer
  that narrows to managed drops retired rows from its keep-set and deletes the
  history retirement promises to preserve; that is the specific mistake this scope
  exists to make un-writable-by-accident.

This module is the one place a bare ``ProxmoxCluster.objects`` is expected;
``core.tests_source_invariants`` ratchets every other call site toward one of
these names so the prose rule "resolvers reject retired clusters" is enforced
rather than merely documented.
"""

from __future__ import annotations

from django.db.models import QuerySet

from core.models import ProxmoxCluster


def managed_clusters() -> QuerySet[ProxmoxCluster]:
    """Live (non-retired) clusters: enabled, disabled or quarantined alike."""
    return ProxmoxCluster.objects.filter(retired_at__isnull=True)


def provider_acquirable_clusters() -> QuerySet[ProxmoxCluster]:
    """Managed clusters a live provider call may originate against.

    ``enabled`` is the immediate acquisition gate and quarantine halts ingestion
    on a CA mismatch; both exclude a cluster from acquisition without retiring it.
    The credential/trust join is what makes this a *usable* transport, not merely
    a permitted one — a cluster mid-onboarding has neither yet.
    """
    return managed_clusters().filter(
        enabled=True,
        ingestion_quarantined=False,
        credential__isnull=False,
        transport_trust__isnull=False,
    )


def historical_clusters() -> QuerySet[ProxmoxCluster]:
    """Every cluster, retired rows included. The scope for archives and cleanup.

    A retention or cleanup writer deciding what to keep per cluster must use this,
    never ``managed_clusters()``: a retired cluster excluded from the keep-set is a
    retired cluster whose last inventory generation gets pruned.
    """
    return ProxmoxCluster.objects.all()


def has_managed_clusters() -> bool:
    """Whether any live cluster exists.

    The named replacement for an ambiguous "does any ``ProxmoxCluster`` exist".
    An installation holding only retired rows has history to show but is not
    operational, and must not re-import environment bootstrap or look like it owns
    a live cluster.
    """
    return managed_clusters().exists()


def has_historical_clusters() -> bool:
    """Whether any cluster row exists at all, retired included.

    Distinguishes "brand-new install, show onboarding only" from "only retired
    rows, show onboarding plus the Connections archive".
    """
    return historical_clusters().exists()

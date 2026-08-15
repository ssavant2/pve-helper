from __future__ import annotations

import logging

from django.core import signing
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.cluster_forms import (
    ClusterConfirmForm,
    ClusterDisplayNameForm,
    ClusterInspectForm,
    CredentialRotationForm,
    EndpointConfirmForm,
    EndpointInspectForm,
    EndpointTrustConfirmForm,
    TopologyHandoffFinalForm,
    TrustCredentialForm,
)
from core.models import (
    ClusterCredential,
    ClusterTopologyHandoffStorageBinding,
    ClusterTransportTrust,
    CurrentGuestInventory,
    ProxmoxCluster,
    ProxmoxEndpoint,
    ProxmoxInventory,
)
from core.services.audit_events import record_audit_event
from core.services.cluster_activation import ClusterActivationError, enable_cluster
from core.services.cluster_credentials import ClusterCredentialError, set_cluster_credential
from core.services.cluster_deletion import (
    ClusterDeletionError,
    ClusterDeletionNotAllowed,
    delete_unused_cluster_connection,
)
from core.services.cluster_deletion_eligibility import unused_connection_deletion_eligibility
from core.services.cluster_inventory_bootstrap import (
    ClusterInventoryBootstrapAlreadyActive,
    ClusterInventoryBootstrapQueueError,
    queue_cluster_inventory_bootstrap,
)
from core.services.cluster_onboarding import (
    ClusterCandidate,
    ClusterOnboardingError,
    VerifiedConnection,
    disable_cluster,
    inspect_transport,
    persist_endpoint,
    persist_new_cluster,
    reapprove_cluster_identity,
    remove_stored_credential,
    set_endpoint_enabled,
    verify_cluster_connection,
    verify_endpoint_for_cluster,
    verify_new_cluster,
    verify_registered_endpoint,
    verify_replacement_credential,
)
from core.services.cluster_projection_read import read_cluster_projection
from core.services.cluster_resolver import enabled_endpoints
from core.services.cluster_retirement import (
    RETIREMENT_REASON_MAX_LENGTH,
    ClusterRetirementError,
    RetirementPreflightEndpointError,
    RetirementPreflightError,
    cluster_handoff_retirement_preflight,
    cluster_retirement_preflight,
    retire_cluster,
)
from core.services.cluster_scopes import historical_clusters, managed_clusters
from core.services.cluster_topology_handoff import (
    ClusterTopologyHandoffError,
    complete_topology_handoff,
    confirm_membership_recovery,
    inspect_membership_recovery,
    repair_unreadable_pending_transition,
    topology_handoff_snapshot,
)
from core.services.cluster_trust import TransportTrustError
from core.services.config import endpoint_name_from_url
from core.services.datastore_nav import datastore_url
from core.services.public_errors import public_failure
from core.services.secret_encryption import (
    EncryptionConfigurationError,
    decrypt_secret,
    encrypt_secret,
)

from ..common import app_login_required, navigation_context
from .enrollment import node_enrollment_rows, unattached_endpoints

# Curated, secret-free domain errors surfaced to the operator. Catching this
# explicit set — rather than the RuntimeError base they all share — keeps an
# unexpected RuntimeError from rendering its raw string into the page (the
# public_errors invariant); anything else 500s into the logs.
CLUSTER_OPERATION_ERRORS = (
    ClusterOnboardingError,
    ClusterCredentialError,
    ClusterActivationError,
    TransportTrustError,
    EncryptionConfigurationError,
    ClusterTopologyHandoffError,
    RetirementPreflightError,
    ClusterRetirementError,
)


_INSPECTION_SALT = "pve-helper.cluster-onboarding.inspection.v1"
_CANDIDATE_SALT = "pve-helper.cluster-onboarding.candidate.v1"
_ENDPOINT_INSPECTION_SALT = "pve-helper.endpoint-onboarding.inspection.v1"
_ENDPOINT_CANDIDATE_SALT = "pve-helper.endpoint-onboarding.candidate.v1"
_TOKEN_MAX_AGE_SECONDS = 10 * 60
_HANDOFF_SALT = "pve-helper.cluster-topology-handoff.v1"
_HANDOFF_CONFIRM_SALT = "pve-helper.cluster-topology-handoff-confirm.v1"
_MEMBERSHIP_RECOVERY_SALT = "pve-helper.cluster-membership-recovery.v1"

logger = logging.getLogger(__name__)


def _retirement_error_response(exc: Exception, *, operation: str, status: int = 409):
    failure = public_failure(
        exc,
        operation=operation,
        fallback="Cluster retirement failed safely. Review the connection state and try again.",
    )
    return JsonResponse(
        {
            "ok": False,
            "error": {
                "code": failure.code,
                "message": failure.message,
            },
        },
        status=status,
    )


def _retirement_blockers(preflight) -> list[dict[str, str]]:
    messages = {
        "active_scan": "Wait for the installation-wide scan to finish, then run preflight again.",
        "storage_consumers": (
            "Release this cluster's storage consumer relationships from their datastore pages, "
            "then run preflight again."
        ),
        "scheduled_actions_active": "Wait for active scheduled runs to finish before verified retirement.",
        "scheduled_actions_unknown": "A scheduled run has an unclassified state and must be resolved first.",
        "consoles_active": "Close active console sessions before verified retirement.",
        "consoles_unknown": "A console session has an unclassified state and must be resolved first.",
        "audit_operations_active": "Wait for active provider operations to finish before verified retirement.",
        "audit_operations_unknown": "A provider operation has an unclassified state and must be resolved first.",
    }
    return [
        {"code": code, "message": messages.get(code, "Retirement is blocked by unresolved work.")}
        for code in preflight.blocker_codes
    ]


def _retirement_impact_payload(cluster: ProxmoxCluster, preflight) -> dict:
    endpoint = next(
        (candidate for candidate in cluster.endpoints.all() if candidate.pk == preflight.endpoint_id),
        None,
    )
    consumers = [
        {
            "storage_id": consumer.storage_id,
            "storage_name": consumer.storage_name,
            "node": consumer.node,
            "last_observed_at": consumer.last_observed_at.isoformat() if consumer.last_observed_at else "",
            "url": datastore_url("core:api_storage_summary", cluster.key, consumer.storage_id),
        }
        for consumer in preflight.storage.consumers
    ]
    return {
        "mode": preflight.mode,
        "identity_verification": preflight.identity_verification,
        "endpoint": endpoint.name if endpoint is not None else "",
        "observed_at": preflight.observed_at.isoformat(),
        "counts": {
            "schedules": preflight.scheduled_actions.active_schedule_count,
            "schedule_runs_not_started": preflight.scheduled_actions.not_started_run_count,
            "schedule_runs_active": preflight.scheduled_actions.active_run_count,
            "current_projections": CurrentGuestInventory.objects.filter(cluster_id=cluster.pk).count(),
            "history": ProxmoxInventory.objects.filter(cluster_id=cluster.pk).count(),
            "storage_definitions": preflight.storage.definition_count,
            "storage_consumers": len(preflight.storage.consumers),
            "consoles_pending": preflight.consoles.pending_count,
            "consoles_active": preflight.consoles.active_count,
            "provider_operations_queued": preflight.audit_operations.queued_count,
            "provider_operations_running": preflight.audit_operations.running_count,
            "active_scans": preflight.active_scan_count,
        },
        "storage_consumers": consumers,
        "blockers": _retirement_blockers(preflight),
    }


def _record_retirement_verification_failure(
    request,
    cluster: ProxmoxCluster,
    *,
    mode: str,
    endpoint_id: int | None,
    error_code: str,
) -> None:
    try:
        record_audit_event(
            request,
            action="cluster.retirement_verification_failed",
            object_type="cluster",
            object_id=cluster.key,
            outcome="refused",
            cluster=cluster,
            cluster_key_snapshot=cluster.key,
            details={
                "cluster_key": cluster.key,
                "retirement_mode": mode,
                "verified_endpoint_id": endpoint_id,
                "reason_code": error_code,
            },
        )
    except Exception:
        logger.exception(
            "Could not record cluster retirement verification failure",
            extra={"cluster_pk": cluster.pk, "reason_code": error_code},
        )


def _cluster_retirement_preflight_response(request, cluster: ProxmoxCluster):
    mode = request.POST.get("mode", "")
    raw_endpoint_id = request.POST.get("endpoint_id", "").strip()
    endpoint_id = None
    try:
        if raw_endpoint_id:
            try:
                endpoint_id = int(raw_endpoint_id)
            except ValueError as exc:
                raise RetirementPreflightEndpointError("Choose an enabled endpoint for verified retirement.") from exc
        preflight = cluster_retirement_preflight(
            cluster,
            mode=mode,
            endpoint_id=endpoint_id,
        )
    except RetirementPreflightError as exc:
        failure = public_failure(exc, operation="cluster_retirement.preflight")
        _record_retirement_verification_failure(
            request,
            cluster,
            mode=mode,
            endpoint_id=endpoint_id,
            error_code=failure.code,
        )
        return JsonResponse(
            {
                "ok": False,
                "error": {
                    "code": failure.code,
                    "message": failure.message,
                },
            },
            status=409,
        )
    except Exception as exc:
        failure = public_failure(
            exc,
            operation="cluster_retirement.preflight",
            fallback="Retirement preflight failed safely. No cluster state was changed.",
        )
        _record_retirement_verification_failure(
            request,
            cluster,
            mode=mode,
            endpoint_id=endpoint_id,
            error_code=failure.code,
        )
        return JsonResponse(
            {
                "ok": False,
                "error": {
                    "code": failure.code,
                    "message": failure.message,
                },
            },
            status=500,
        )

    return JsonResponse(
        {
            "ok": True,
            "ready": preflight.gate_clear,
            "confirmation": preflight.confirmation,
            "cluster": {
                "key": cluster.key,
                "display_name": cluster.display_name,
            },
            "impact": _retirement_impact_payload(cluster, preflight),
        }
    )


def _cluster_retirement_final_response(request, cluster: ProxmoxCluster):
    try:
        result = retire_cluster(
            cluster,
            confirmation=request.POST.get("confirmation", ""),
            actor=request.user,
            typed_cluster_key=request.POST.get("typed_cluster_key", ""),
            reason=request.POST.get("reason", ""),
            permanent_unavailability_asserted=(request.POST.get("permanent_unavailability_asserted", "") == "yes"),
        )
    except (RetirementPreflightError, ClusterRetirementError) as exc:
        return _retirement_error_response(exc, operation="cluster_retirement.final")
    except Exception as exc:
        return _retirement_error_response(
            exc,
            operation="cluster_retirement.final",
            status=500,
        )
    return JsonResponse(
        {
            "ok": True,
            "mode": result.mode,
            "redirect_url": reverse("core:clusters_overview"),
        }
    )


def _deletion_error_response(exc: Exception, *, status: int = 409):
    failure = public_failure(
        exc,
        operation="cluster_deletion.final",
        fallback="Deleting the unused connection failed safely. No changes were committed.",
    )
    return JsonResponse(
        {
            "ok": False,
            "error": {
                "code": failure.code,
                "message": failure.message,
            },
        },
        status=status,
    )


def _unused_deletion_preflight_response(request, cluster: ProxmoxCluster):
    """Re-prove eligibility right before the typed-key ceremony.

    A pure read: no lock, no mutation and no provider call. It re-checks
    eligibility so a page that has since acquired footprint shows the blocker
    rather than leading the operator through a typed-key confirmation that the
    under-lock re-check would only reject at the end.
    """
    eligibility = unused_connection_deletion_eligibility(cluster)
    credential_token = ClusterCredential.objects.filter(cluster=cluster).values_list("token_id", flat=True).first()
    trust = ClusterTransportTrust.objects.filter(cluster=cluster).first()
    return JsonResponse(
        {
            "ok": True,
            "eligible": eligibility.eligible,
            "cluster": {
                "key": cluster.key,
                "display_name": cluster.display_name,
            },
            "blockers": [
                {
                    "relation": blocker.relation,
                    "kind": blocker.kind,
                    "count": blocker.count,
                    "detail": blocker.detail,
                }
                for blocker in eligibility.blockers
            ],
            "config": {
                "endpoints": [
                    {"name": endpoint.name, "url": endpoint.normalized_url}
                    for endpoint in cluster.endpoints.order_by("name")
                ],
                "token_id": credential_token or "",
                "trust_mode": trust.get_mode_display() if trust is not None else "",
                "ca_uuid": cluster.discovered_ca_uuid,
            },
        }
    )


def _unused_deletion_final_response(request, cluster: ProxmoxCluster):
    # The typed permanent key is enforced server-side, not only in the dialog:
    # the exact-key ceremony is a real gate, mirroring forced retirement. The
    # service's own under-lock eligibility re-check closes the TOCTOU.
    if request.POST.get("typed_cluster_key", "").strip() != cluster.key:
        return _deletion_error_response(
            ClusterDeletionNotAllowed("Type the exact permanent cluster key to delete this connection."),
            status=400,
        )
    try:
        delete_unused_cluster_connection(cluster, actor=request.user)
    except ClusterDeletionError as exc:
        return _deletion_error_response(exc)
    except Exception as exc:
        return _deletion_error_response(exc, status=500)
    return JsonResponse(
        {
            "ok": True,
            "redirect_url": reverse("core:clusters_overview"),
        }
    )


@app_login_required
def clusters_overview(request):
    historical = list(historical_clusters().prefetch_related("endpoints").order_by("display_name", "key"))
    clusters = [cluster for cluster in historical if not cluster.is_retired]
    retired_clusters = [cluster for cluster in historical if cluster.is_retired]
    credential_ids = {row.cluster_id: row.token_id for row in ClusterCredential.objects.filter(cluster__in=clusters)}
    trust_modes = {
        row.cluster_id: row.get_mode_display() for row in ClusterTransportTrust.objects.filter(cluster__in=clusters)
    }
    for cluster in clusters:
        cluster.endpoint_count = len(cluster.endpoints.all())
        cluster.enabled_endpoint_count = sum(1 for endpoint in cluster.endpoints.all() if endpoint.enabled)
        cluster.token_id = credential_ids.get(cluster.pk, "")
        cluster.trust_label = trust_modes.get(cluster.pk, "Not configured")
    return render(
        request,
        "core/clusters.html",
        {
            **navigation_context("clusters"),
            "clusters": clusters,
            "retired_clusters": retired_clusters,
        },
    )


def _handoff_payload(request, raw: str = "") -> tuple[dict | None, ProxmoxCluster | None, object | None]:
    if not raw:
        return None, None, None
    payload = _load(request, raw, _HANDOFF_SALT, "topology-handoff")
    cluster = historical_clusters().filter(pk=payload.get("cluster_pk"), key=payload.get("cluster_key")).first()
    if cluster is None or cluster.is_retired:
        raise ClusterTopologyHandoffError("The source connection is no longer available for hand-off.")
    snapshot = topology_handoff_snapshot(cluster)
    if snapshot.digest != payload.get("snapshot_digest"):
        raise ClusterTopologyHandoffError("The topology hand-off changed. Start from Connections again.")
    return payload, cluster, snapshot


def _new_handoff_token(request, cluster: ProxmoxCluster) -> tuple[str, object]:
    snapshot = topology_handoff_snapshot(cluster)
    return (
        _sign(
            request,
            _HANDOFF_SALT,
            {
                "kind": "topology-handoff",
                "cluster_pk": cluster.pk,
                "cluster_key": cluster.key,
                "snapshot_digest": snapshot.digest,
                "pending_role": snapshot.pending_role,
            },
        ),
        snapshot,
    )


@app_login_required
def cluster_add(request):
    context = {**navigation_context("clusters", page_title="Add host/cluster"), "step": "identity"}
    if request.method == "GET":
        handoff_key = request.GET.get("handoff_from", "").strip()
        if handoff_key:
            cluster = get_object_or_404(managed_clusters(), key=handoff_key)
            try:
                handoff_token, snapshot = _new_handoff_token(request, cluster)
            except ClusterTopologyHandoffError as exc:
                return _render_cluster_connection(request, cluster, operation_error=str(exc))
            context.update(
                {
                    "handoff": handoff_token,
                    "handoff_snapshot": snapshot,
                    "handoff_source": cluster,
                }
            )
        context["inspect_form"] = ClusterInspectForm()
        return render(request, "core/cluster_add.html", context)

    action = request.POST.get("action", "")
    if action == "inspect":
        form = ClusterInspectForm(request.POST)
        context["inspect_form"] = form
        handoff_raw = request.POST.get("handoff", "")
        try:
            _handoff, handoff_source, handoff_snapshot = _handoff_payload(request, handoff_raw)
        except ClusterTopologyHandoffError as exc:
            form.add_error(None, str(exc))
            return render(request, "core/cluster_add.html", context)
        if handoff_source is not None:
            context.update(
                {"handoff": handoff_raw, "handoff_source": handoff_source, "handoff_snapshot": handoff_snapshot}
            )
        if form.is_valid():
            try:
                certificate = inspect_transport(form.cleaned_data["endpoint_url"])
                inspection = _sign(
                    request,
                    _INSPECTION_SALT,
                    {
                        "kind": "cluster-inspection",
                        "display_name": form.cleaned_data["display_name"],
                        "cluster_key": form.cleaned_data["cluster_key"],
                        "endpoint_url": form.cleaned_data["endpoint_url"].rstrip("/"),
                        "endpoint_name": form.cleaned_data["endpoint_name"]
                        or endpoint_name_from_url(form.cleaned_data["endpoint_url"]),
                        "certificate": _certificate_data(certificate),
                        "handoff": handoff_raw,
                    },
                )
            except ClusterOnboardingError as exc:
                form.add_error("endpoint_url", str(exc))
            else:
                context.update(
                    {
                        "step": "trust",
                        "certificate": certificate,
                        "candidate_meta": signing.loads(inspection, salt=_INSPECTION_SALT),
                        "trust_form": TrustCredentialForm(initial={"inspection": inspection}),
                    }
                )
        return render(request, "core/cluster_add.html", context)

    if action == "verify":
        form = TrustCredentialForm(request.POST)
        context.update({"step": "trust", "trust_form": form})
        try:
            inspection = _load(request, request.POST.get("inspection", ""), _INSPECTION_SALT, "cluster-inspection")
        except ClusterOnboardingError as exc:
            form.add_error(None, str(exc))
            return render(request, "core/cluster_add.html", context)
        context.update(
            {
                "candidate_meta": inspection,
                "certificate": _certificate_from_data(inspection["certificate"]),
            }
        )
        if form.is_valid():
            candidate = _candidate_from_inspection(inspection, form.cleaned_data)
            try:
                handoff_raw = str(inspection.get("handoff") or "")
                _handoff, handoff_source, handoff_snapshot = _handoff_payload(request, handoff_raw)
                candidate, verified = verify_new_cluster(
                    candidate,
                    expected_certificate_fingerprint=inspection["certificate"]["sha256_fingerprint"],
                    handoff_from=handoff_source,
                )
                candidate_token = _sign(
                    request,
                    _CANDIDATE_SALT,
                    {
                        "kind": "cluster-candidate",
                        "candidate": _candidate_data(candidate),
                        "token_secret_sealed": encrypt_secret(candidate.token_secret),
                        "verified": _verified_data(verified),
                        "handoff": handoff_raw,
                    },
                )
            except CLUSTER_OPERATION_ERRORS as exc:
                form.add_error(None, str(exc))
            else:
                context.update(
                    {
                        "step": "confirm",
                        "verified": verified,
                        "candidate": candidate,
                        "confirm_form": ClusterConfirmForm(initial={"candidate": candidate_token}),
                        "handoff": handoff_raw,
                        "handoff_source": handoff_source,
                        "handoff_snapshot": handoff_snapshot,
                    }
                )
        return render(request, "core/cluster_add.html", context)

    if action == "confirm":
        form = ClusterConfirmForm(request.POST)
        context.update({"step": "confirm", "confirm_form": form})
        try:
            payload = _load(request, request.POST.get("candidate", ""), _CANDIDATE_SALT, "cluster-candidate")
            candidate = _candidate_from_data(payload["candidate"], decrypt_secret(payload["token_secret_sealed"]))
            handoff_raw = str(payload.get("handoff") or "")
            _handoff, handoff_source, handoff_snapshot = _handoff_payload(request, handoff_raw)
            context.update(
                {
                    "candidate": candidate,
                    "verified": _verified_from_data(payload["verified"]),
                    "handoff": handoff_raw,
                    "handoff_source": handoff_source,
                    "handoff_snapshot": handoff_snapshot,
                }
            )
        except CLUSTER_OPERATION_ERRORS as exc:
            form.add_error(None, str(exc))
            return render(request, "core/cluster_add.html", context)
        if form.is_valid():
            try:
                candidate, verified = verify_new_cluster(
                    candidate,
                    expected_certificate_fingerprint=payload["verified"]["certificate"]["sha256_fingerprint"],
                    handoff_from=handoff_source,
                )
                _assert_verified_unchanged(payload["verified"], verified)
                if handoff_source is not None:
                    raw_selected_ids = request.POST.getlist("storage_binding")
                    if any(not str(value).isdigit() for value in raw_selected_ids):
                        raise ClusterTopologyHandoffError("The selected storage mapping list is invalid.")
                    selected_ids = tuple(sorted({int(value) for value in raw_selected_ids}))
                    available = {row.pk: row for row in handoff_snapshot.storage_bindings}
                    if not set(selected_ids) <= set(available):
                        raise ClusterTopologyHandoffError("The selected storage mapping list changed.")
                    confirmation = _sign(
                        request,
                        _HANDOFF_CONFIRM_SALT,
                        {
                            "kind": "topology-handoff-confirmation",
                            "candidate_token": request.POST.get("candidate", ""),
                            "snapshot_digest": handoff_snapshot.digest,
                            "selected_storage_binding_ids": list(selected_ids),
                        },
                    )
                    context.update(
                        {
                            "step": "handoff-confirm",
                            "final_form": TopologyHandoffFinalForm(initial={"handoff_confirmation": confirmation}),
                            "selected_storage_bindings": [available[row_id] for row_id in selected_ids],
                        }
                    )
                    return render(request, "core/cluster_add.html", context)
                with transaction.atomic():
                    cluster = persist_new_cluster(candidate, verified)
                    record_audit_event(
                        request,
                        action="cluster.added",
                        object_type="cluster",
                        object_id=cluster.key,
                        cluster=cluster,
                        details={
                            "cluster_key": cluster.key,
                            "display_name": cluster.display_name,
                            "endpoint_name": candidate.endpoint_name,
                            "endpoint_url": cluster.endpoints.get(name=candidate.endpoint_name).normalized_url,
                            "trust_mode": candidate.trust_mode,
                            "token_id": candidate.token_id,
                            "ca_uuid": verified.identity.ca_uuid,
                        },
                    )
            except CLUSTER_OPERATION_ERRORS as exc:
                form.add_error(None, str(exc))
            else:
                _queue_first_inventory(request, cluster)
                return redirect("core:cluster_connection", cluster_key=cluster.key)
        return render(request, "core/cluster_add.html", context)

    if action == "complete-handoff":
        form = TopologyHandoffFinalForm(request.POST)
        context.update({"step": "handoff-confirm", "final_form": form})
        try:
            confirmation_payload = _load(
                request,
                request.POST.get("handoff_confirmation", ""),
                _HANDOFF_CONFIRM_SALT,
                "topology-handoff-confirmation",
            )
            candidate_token = str(confirmation_payload.get("candidate_token") or "")
            payload = _load(request, candidate_token, _CANDIDATE_SALT, "cluster-candidate")
            candidate = _candidate_from_data(payload["candidate"], decrypt_secret(payload["token_secret_sealed"]))
            handoff_raw = str(payload.get("handoff") or "")
            _handoff, handoff_source, handoff_snapshot = _handoff_payload(request, handoff_raw)
            if handoff_source is None or confirmation_payload.get("snapshot_digest") != handoff_snapshot.digest:
                raise ClusterTopologyHandoffError("The topology hand-off changed. Start from Connections again.")
            raw_selected_ids = confirmation_payload.get("selected_storage_binding_ids", [])
            if not isinstance(raw_selected_ids, list):
                raise ClusterTopologyHandoffError("The signed storage mapping list is invalid.")
            selected_ids = tuple(int(value) for value in raw_selected_ids)
            available = {row.pk: row for row in handoff_snapshot.storage_bindings}
            if len(set(selected_ids)) != len(selected_ids) or not set(selected_ids) <= set(available):
                raise ClusterTopologyHandoffError("The signed storage mapping list is invalid or changed.")
            context.update(
                {
                    "candidate": candidate,
                    "verified": _verified_from_data(payload["verified"]),
                    "handoff": handoff_raw,
                    "handoff_source": handoff_source,
                    "handoff_snapshot": handoff_snapshot,
                    "selected_storage_bindings": [available[row_id] for row_id in selected_ids],
                }
            )
        except TypeError, ValueError:
            form.add_error(None, "The signed storage mapping list is invalid.")
            return render(request, "core/cluster_add.html", context)
        except CLUSTER_OPERATION_ERRORS as exc:
            form.add_error(None, str(exc))
            return render(request, "core/cluster_add.html", context)
        if form.is_valid():
            try:
                candidate, verified = verify_new_cluster(
                    candidate,
                    expected_certificate_fingerprint=payload["verified"]["certificate"]["sha256_fingerprint"],
                    handoff_from=handoff_source,
                )
                _assert_verified_unchanged(payload["verified"], verified)
                preflight = cluster_handoff_retirement_preflight(
                    handoff_source,
                    replacement_ca_uuid=verified.identity.ca_uuid,
                )
                if not preflight.gate_clear or not preflight.confirmation:
                    raise ClusterTopologyHandoffError(
                        "The source connection has retirement blockers: "
                        + ", ".join(preflight.blocker_codes or ("unknown",))
                        + "."
                    )
                cluster = complete_topology_handoff(
                    old_cluster=handoff_source,
                    candidate=candidate,
                    verified=verified,
                    expected_snapshot_digest=handoff_snapshot.digest,
                    selected_storage_binding_ids=selected_ids,
                    retirement_confirmation=preflight.confirmation,
                    actor=request.user,
                )
            except CLUSTER_OPERATION_ERRORS as exc:
                form.add_error(None, str(exc))
            else:
                _queue_first_inventory(request, cluster)
                return redirect("core:cluster_connection", cluster_key=cluster.key)
        return render(request, "core/cluster_add.html", context)

    raise Http404("Unknown onboarding step")


def _queue_first_inventory(request, cluster) -> None:
    """Start the new connection's first inventory, without risking the add.

    Strictly after the onboarding transaction has committed: the bootstrap's own
    audit row must not be able to roll the connection back, and a queue that is
    down must not turn a verified, persisted connection into an error page. The
    failure is already durable either way — `queue_cluster_inventory_bootstrap`
    finalizes its row as failed before raising — so the operator can see it in
    Recent Tasks and refresh manually.
    """
    try:
        queue_cluster_inventory_bootstrap(cluster=cluster, request=request)
    except ClusterInventoryBootstrapAlreadyActive, ClusterInventoryBootstrapQueueError:
        logger.warning("First inventory could not be queued for cluster=%s", cluster.key, exc_info=True)


@app_login_required
def cluster_connection(request, cluster_key: str):
    cluster = get_object_or_404(historical_clusters().select_related("retired_by"), key=cluster_key)
    if cluster.is_retired:
        return render(
            request,
            "core/cluster_connection_retired.html",
            {
                **navigation_context(
                    "clusters",
                    page_title=(cluster.display_name, "Retired connection"),
                ),
                "cluster": cluster,
            },
        )
    return _render_cluster_connection(request, cluster)


def _verified_retirement_blocker(
    cluster: ProxmoxCluster,
    *,
    credential,
    trust,
    endpoints,
) -> str:
    """The one precondition verified retirement is missing, named for the operator.

    Only statically knowable preconditions belong here. Whether the site actually
    answers is exactly what verified retirement's provider call decides, so it can
    never gate the control — that is the case forced retirement exists for, and
    hiding the escape hatch behind a reachability guess would strand a dead site.
    """
    if cluster.enabled:
        return "Disable the cluster first"
    if not credential or not trust:
        return "A stored credential and transport trust are required"
    if not cluster.discovered_ca_uuid:
        return "A pinned Proxmox CA identity is required"
    if not endpoints:
        return "An enabled endpoint is required"
    return ""


def _render_cluster_connection(
    request,
    cluster: ProxmoxCluster,
    *,
    operation_error: str = "",
    show_force_retire: bool = False,
    membership_recovery_candidate=None,
    membership_recovery_token: str = "",
):
    credential = ClusterCredential.objects.filter(cluster=cluster).first()
    trust = ClusterTransportTrust.objects.filter(cluster=cluster).first()
    retirement_endpoints = enabled_endpoints(cluster)
    endpoints = list(cluster.endpoints.order_by("name"))
    node_rows = node_enrollment_rows(cluster)
    topology_state = read_cluster_projection(cluster.key)
    membership_coverage = topology_state.membership_coverage
    topology_handoff_storage_bindings = list(
        ClusterTopologyHandoffStorageBinding.objects.filter(cluster=cluster)
        .select_related("mount")
        .order_by("storage_id", "scope", "node", "pk")
    )
    return render(
        request,
        "core/cluster_connection.html",
        {
            **navigation_context(
                "clusters",
                page_title=(cluster.display_name, "Connection"),
                cluster_key=cluster.key,
            ),
            "cluster": cluster,
            "endpoints": endpoints,
            # One panel, two row types. A transport is not an inventory, but a second
            # table of the same node names taught the opposite, so the address moved
            # into the node row it was observed on and the endpoints no node accounts
            # for keep their own rows underneath.
            "unattached_endpoints": unattached_endpoints(endpoints, node_rows),
            "retirement_endpoints": retirement_endpoints,
            "verified_retirement_blocker": _verified_retirement_blocker(
                cluster,
                credential=credential,
                trust=trust,
                endpoints=retirement_endpoints,
            ),
            "retirement_reason_max_length": RETIREMENT_REASON_MAX_LENGTH,
            "deletion_eligibility": unused_connection_deletion_eligibility(cluster),
            "credential": credential,
            "trust": trust,
            "display_name_form": ClusterDisplayNameForm(initial={"display_name": cluster.display_name}),
            "credential_form": CredentialRotationForm(initial={"token_id": credential.token_id if credential else ""}),
            "operation_error": operation_error,
            "show_force_retire": show_force_retire,
            "topology_state": topology_state,
            "membership_coverage": membership_coverage,
            "node_rows": node_rows,
            "membership_recovery_candidate": membership_recovery_candidate,
            "membership_recovery_token": membership_recovery_token,
            "topology_handoff_storage_bindings": topology_handoff_storage_bindings,
        },
    )


@require_POST
@app_login_required
def cluster_connection_action(request, cluster_key: str):
    cluster = get_object_or_404(managed_clusters(), key=cluster_key)
    action = request.POST.get("action", "")
    if action == "retirement-preflight":
        return _cluster_retirement_preflight_response(request, cluster)
    if action == "retire":
        return _cluster_retirement_final_response(request, cluster)
    if action == "delete-unused-preflight":
        return _unused_deletion_preflight_response(request, cluster)
    if action == "delete-unused-connection":
        return _unused_deletion_final_response(request, cluster)
    error = ""
    try:
        if action == "display-name":
            form = ClusterDisplayNameForm(request.POST)
            if not form.is_valid():
                raise ClusterOnboardingError("Enter a valid display name.")
            with transaction.atomic():
                cluster.display_name = form.cleaned_data["display_name"].strip()
                cluster.save(update_fields=["display_name", "updated_at"])
                record_audit_event(
                    request,
                    action="cluster.display_name_changed",
                    object_type="cluster",
                    object_id=cluster.key,
                    cluster=cluster,
                    details={"cluster_key": cluster.key, "display_name": cluster.display_name},
                )
        elif action == "disable":
            with transaction.atomic():
                cluster = disable_cluster(cluster)
                record_audit_event(
                    request,
                    action="cluster.disabled",
                    object_type="cluster",
                    object_id=cluster.key,
                    cluster=cluster,
                    details={"cluster_key": cluster.key},
                )
        elif action == "enable":
            verified = verify_cluster_connection(cluster)
            with transaction.atomic():
                cluster = enable_cluster(cluster)
                record_audit_event(
                    request,
                    action="cluster.enabled",
                    object_type="cluster",
                    object_id=cluster.key,
                    cluster=cluster,
                    details={"cluster_key": cluster.key, "ca_uuid": verified.identity.ca_uuid},
                )
        elif action == "rotate-credential":
            form = CredentialRotationForm(request.POST)
            if not form.is_valid():
                raise ClusterOnboardingError("Both token ID and token secret are required.")
            token_id = form.cleaned_data["token_id"].strip()
            token_secret = form.cleaned_data["token_secret"].strip()
            verified = verify_replacement_credential(
                cluster,
                token_id=token_id,
                token_secret=token_secret,
            )
            with transaction.atomic():
                set_cluster_credential(cluster, token_id=token_id, token_secret=token_secret)
                record_audit_event(
                    request,
                    action="cluster.credential_rotated",
                    object_type="cluster_credential",
                    object_id=cluster.key,
                    cluster=cluster,
                    details={"cluster_key": cluster.key, "token_id": token_id, "ca_uuid": verified.identity.ca_uuid},
                )
        elif action == "remove-credential":
            with transaction.atomic():
                token_id = (
                    ClusterCredential.objects.filter(cluster=cluster).values_list("token_id", flat=True).first() or ""
                )
                remove_stored_credential(cluster)
                record_audit_event(
                    request,
                    action="cluster.credential_removed",
                    object_type="cluster_credential",
                    object_id=cluster.key,
                    cluster=cluster,
                    details={"cluster_key": cluster.key, "token_id": token_id},
                )
        elif action == "reapprove-identity":
            with transaction.atomic():
                identity = reapprove_cluster_identity(cluster)
                record_audit_event(
                    request,
                    action="cluster.identity_reapproved",
                    object_type="cluster",
                    object_id=cluster.key,
                    cluster=cluster,
                    details={"cluster_key": cluster.key, "ca_uuid": identity.ca_uuid},
                )
        elif action == "repair-topology-pending":
            repair_unreadable_pending_transition(
                cluster,
                typed_cluster_key=request.POST.get("typed_cluster_key", "").strip(),
                actor=request.user,
            )
        elif action == "inspect-membership-recovery":
            candidate = inspect_membership_recovery(
                cluster,
                endpoint_id=(
                    int(request.POST["endpoint_id"]) if request.POST.get("endpoint_id", "").isdigit() else None
                ),
            )
            token = _sign(
                request,
                _MEMBERSHIP_RECOVERY_SALT,
                {
                    "kind": "membership-recovery",
                    "cluster_pk": cluster.pk,
                    "cluster_key": cluster.key,
                    "endpoint_id": candidate.endpoint_id,
                    "candidate_digest": candidate.digest,
                },
            )
            return _render_cluster_connection(
                request,
                cluster,
                membership_recovery_candidate=candidate,
                membership_recovery_token=token,
            )
        elif action == "confirm-membership-recovery":
            payload = _load(
                request,
                request.POST.get("recovery", ""),
                _MEMBERSHIP_RECOVERY_SALT,
                "membership-recovery",
            )
            if payload.get("cluster_pk") != cluster.pk or payload.get("cluster_key") != cluster.key:
                raise ClusterTopologyHandoffError("This membership recovery belongs to another connection.")
            if request.POST.get("confirm_members", "") != "yes":
                raise ClusterTopologyHandoffError("Confirm the displayed member set before replacing it.")
            confirm_membership_recovery(
                cluster,
                endpoint_id=int(payload["endpoint_id"]),
                expected_digest=str(payload["candidate_digest"]),
                actor=request.user,
            )
        else:
            raise Http404("Unknown cluster action")
    except CLUSTER_OPERATION_ERRORS as exc:
        error = public_failure(exc, operation="cluster_connection_action").message

    if error:
        return _render_cluster_connection(
            request,
            cluster,
            operation_error=error,
            show_force_retire=(action == "disable" and cluster.enabled),
        )
    return redirect("core:cluster_connection", cluster_key=cluster.key)


@app_login_required
def cluster_endpoint_add(request, cluster_key: str):
    cluster = get_object_or_404(managed_clusters(), key=cluster_key)
    context = {
        **navigation_context(
            "clusters",
            page_title=(cluster.display_name, "Add endpoint"),
            cluster_key=cluster.key,
        ),
        "cluster": cluster,
        "step": "inspect",
    }
    if request.method == "GET":
        context["inspect_form"] = EndpointInspectForm()
        return render(request, "core/cluster_endpoint_add.html", context)

    action = request.POST.get("action", "")
    if action == "inspect":
        form = EndpointInspectForm(request.POST)
        context["inspect_form"] = form
        if form.is_valid():
            endpoint_url = form.cleaned_data["endpoint_url"].rstrip("/")
            endpoint_name = form.cleaned_data["endpoint_name"] or endpoint_name_from_url(endpoint_url)
            try:
                certificate = inspect_transport(endpoint_url)
                inspection = _sign(
                    request,
                    _ENDPOINT_INSPECTION_SALT,
                    {
                        "kind": "endpoint-inspection",
                        "cluster_key": cluster.key,
                        "endpoint_url": endpoint_url,
                        "endpoint_name": endpoint_name,
                        "certificate": _certificate_data(certificate),
                    },
                )
            except ClusterOnboardingError as exc:
                form.add_error("endpoint_url", str(exc))
            else:
                context.update(
                    {
                        "step": "trust",
                        "certificate": certificate,
                        "endpoint_meta": {"endpoint_url": endpoint_url, "endpoint_name": endpoint_name},
                        "trust_form": EndpointTrustConfirmForm(initial={"inspection": inspection}),
                    }
                )
        return render(request, "core/cluster_endpoint_add.html", context)

    if action == "verify":
        form = EndpointTrustConfirmForm(request.POST)
        context.update({"step": "trust", "trust_form": form})
        try:
            inspection = _load(
                request,
                request.POST.get("inspection", ""),
                _ENDPOINT_INSPECTION_SALT,
                "endpoint-inspection",
            )
            if inspection["cluster_key"] != cluster.key:
                raise ClusterOnboardingError("Endpoint inspection belongs to a different cluster.")
        except ClusterOnboardingError as exc:
            form.add_error(None, str(exc))
            return render(request, "core/cluster_endpoint_add.html", context)
        context.update(
            {
                "certificate": _certificate_from_data(inspection["certificate"]),
                "endpoint_meta": inspection,
            }
        )
        if form.is_valid():
            try:
                verified = verify_endpoint_for_cluster(
                    cluster,
                    endpoint_url=inspection["endpoint_url"],
                    endpoint_name=inspection["endpoint_name"],
                    expected_certificate_fingerprint=inspection["certificate"]["sha256_fingerprint"],
                )
                endpoint_token = _sign(
                    request,
                    _ENDPOINT_CANDIDATE_SALT,
                    {
                        **inspection,
                        "kind": "endpoint-candidate",
                        "verified": _verified_data(verified),
                    },
                )
            except CLUSTER_OPERATION_ERRORS as exc:
                form.add_error(None, str(exc))
            else:
                context.update(
                    {
                        "step": "confirm",
                        "verified": verified,
                        "confirm_form": EndpointConfirmForm(initial={"endpoint": endpoint_token}),
                    }
                )
        return render(request, "core/cluster_endpoint_add.html", context)

    if action == "confirm":
        form = EndpointConfirmForm(request.POST)
        context.update({"step": "confirm", "confirm_form": form})
        try:
            payload = _load(
                request,
                request.POST.get("endpoint", ""),
                _ENDPOINT_CANDIDATE_SALT,
                "endpoint-candidate",
            )
            if payload["cluster_key"] != cluster.key:
                raise ClusterOnboardingError("Endpoint candidate belongs to a different cluster.")
            context.update(
                {
                    "endpoint_meta": payload,
                    "certificate": _certificate_from_data(payload["certificate"]),
                    "verified": _verified_from_data(payload["verified"]),
                }
            )
        except ClusterOnboardingError as exc:
            form.add_error(None, str(exc))
            return render(request, "core/cluster_endpoint_add.html", context)
        if form.is_valid():
            try:
                verified = verify_endpoint_for_cluster(
                    cluster,
                    endpoint_url=payload["endpoint_url"],
                    endpoint_name=payload["endpoint_name"],
                    expected_certificate_fingerprint=payload["certificate"]["sha256_fingerprint"],
                )
                _assert_verified_unchanged(payload["verified"], verified)
                with transaction.atomic():
                    endpoint = persist_endpoint(
                        cluster,
                        endpoint_url=payload["endpoint_url"],
                        endpoint_name=payload["endpoint_name"],
                    )
                    record_audit_event(
                        request,
                        action="cluster.endpoint_added",
                        object_type="cluster_endpoint",
                        object_id=f"{cluster.key}:{endpoint.name}",
                        cluster=cluster,
                        details={
                            "cluster_key": cluster.key,
                            "endpoint_name": endpoint.name,
                            "endpoint_url": endpoint.normalized_url,
                            "ca_uuid": verified.identity.ca_uuid,
                        },
                    )
            except CLUSTER_OPERATION_ERRORS as exc:
                form.add_error(None, str(exc))
            else:
                return redirect("core:cluster_connection", cluster_key=cluster.key)
        return render(request, "core/cluster_endpoint_add.html", context)

    raise Http404("Unknown endpoint onboarding step")


@require_POST
@app_login_required
def cluster_endpoint_action(request, cluster_key: str, endpoint_id: int):
    cluster = get_object_or_404(managed_clusters(), key=cluster_key)
    endpoint = get_object_or_404(ProxmoxEndpoint, pk=endpoint_id, cluster=cluster)
    action = request.POST.get("action", "")
    if action not in {"enable", "disable"}:
        raise Http404("Unknown endpoint action")
    try:
        if action == "enable":
            verify_registered_endpoint(cluster, endpoint)
        with transaction.atomic():
            endpoint = set_endpoint_enabled(endpoint, enabled=action == "enable")
            record_audit_event(
                request,
                action=f"cluster.endpoint_{action}d",
                object_type="cluster_endpoint",
                object_id=f"{cluster.key}:{endpoint.name}",
                cluster=cluster,
                details={
                    "cluster_key": cluster.key,
                    "endpoint_name": endpoint.name,
                    "endpoint_url": endpoint.normalized_url,
                },
            )
    except ClusterOnboardingError as exc:
        return _render_cluster_connection(request, cluster, operation_error=str(exc))
    return redirect("core:cluster_connection", cluster_key=cluster.key)


def _actor_key(request) -> str:
    user = getattr(request, "user", None)
    return str(user.pk) if user is not None and getattr(user, "is_authenticated", False) else "anonymous-dev"


def _sign(request, salt: str, payload: dict) -> str:
    return signing.dumps({**payload, "actor": _actor_key(request)}, salt=salt, compress=True)


def _load(request, raw: str, salt: str, kind: str) -> dict:
    try:
        payload = signing.loads(raw, salt=salt, max_age=_TOKEN_MAX_AGE_SECONDS)
    except signing.SignatureExpired as exc:
        raise ClusterOnboardingError("This verification expired. Start the step again.") from exc
    except signing.BadSignature as exc:
        raise ClusterOnboardingError("This verification is invalid. Start the step again.") from exc
    if not isinstance(payload, dict) or payload.get("kind") != kind or payload.get("actor") != _actor_key(request):
        raise ClusterOnboardingError("This verification belongs to another workflow or operator.")
    return payload


def _certificate_data(certificate) -> dict:
    return {
        "subject": certificate.subject,
        "issuer": certificate.issuer,
        "sha256_fingerprint": certificate.sha256_fingerprint,
    }


def _certificate_from_data(data: dict):
    from core.services.cluster_trust import InspectedCertificate

    return InspectedCertificate(
        subject=str(data.get("subject") or ""),
        issuer=str(data.get("issuer") or ""),
        sha256_fingerprint=str(data.get("sha256_fingerprint") or ""),
    )


def _candidate_data(candidate: ClusterCandidate) -> dict:
    return {
        "key": candidate.key,
        "display_name": candidate.display_name,
        "endpoint_url": candidate.endpoint_url,
        "endpoint_name": candidate.endpoint_name,
        "trust_mode": candidate.trust_mode,
        "token_id": candidate.token_id,
        "ca_pem": candidate.ca_pem,
    }


def _candidate_from_data(data: dict, token_secret: str) -> ClusterCandidate:
    return ClusterCandidate(
        key=str(data.get("key") or ""),
        display_name=str(data.get("display_name") or ""),
        endpoint_url=str(data.get("endpoint_url") or ""),
        endpoint_name=str(data.get("endpoint_name") or ""),
        trust_mode=str(data.get("trust_mode") or ""),
        token_id=str(data.get("token_id") or ""),
        token_secret=token_secret,
        ca_pem=str(data.get("ca_pem") or ""),
    )


def _candidate_from_inspection(inspection: dict, values: dict) -> ClusterCandidate:
    return ClusterCandidate(
        key=inspection["cluster_key"],
        display_name=inspection["display_name"],
        endpoint_url=inspection["endpoint_url"],
        endpoint_name=inspection["endpoint_name"],
        trust_mode=values["trust_mode"],
        token_id=values["token_id"],
        token_secret=values["token_secret"],
        ca_pem=values.get("ca_pem", ""),
    )


def _verified_data(verified: VerifiedConnection) -> dict:
    return {
        "certificate": _certificate_data(verified.certificate),
        "identity": {
            "ca_uuid": verified.identity.ca_uuid,
            "ca_fingerprint": verified.identity.ca_fingerprint,
        },
        "node_names": list(verified.node_names),
        "version": verified.version,
        "discovered_name": verified.discovered_name,
        "administrator_privileges": list(verified.administrator_privileges),
        "topology_role": verified.topology_role.value,
        "membership_complete": verified.membership_complete,
        "local_node_name": verified.local_node_name,
    }


def _verified_from_data(data: dict) -> VerifiedConnection:
    from core.services.cluster_identity import ObservedClusterIdentity
    from core.services.cluster_topology_role import TopologyRole

    identity = data.get("identity") or {}
    return VerifiedConnection(
        certificate=_certificate_from_data(data.get("certificate") or {}),
        identity=ObservedClusterIdentity(
            ca_uuid=str(identity.get("ca_uuid") or ""),
            ca_fingerprint=str(identity.get("ca_fingerprint") or ""),
        ),
        node_names=tuple(str(value) for value in data.get("node_names") or []),
        version=str(data.get("version") or ""),
        discovered_name=str(data.get("discovered_name") or ""),
        administrator_privileges=tuple(str(value) for value in data.get("administrator_privileges") or []),
        topology_role=(
            TopologyRole(str(data.get("topology_role") or "unknown"))
            if str(data.get("topology_role") or "unknown") in {role.value for role in TopologyRole}
            else TopologyRole.UNKNOWN
        ),
        membership_complete=data.get("membership_complete") is True,
        local_node_name=str(data.get("local_node_name") or ""),
    )


def _assert_verified_unchanged(expected: dict, current: VerifiedConnection) -> None:
    expected_identity = expected.get("identity") or {}
    if (
        str(expected_identity.get("ca_uuid") or "") != current.identity.ca_uuid
        or str(expected_identity.get("ca_fingerprint") or "") != current.identity.ca_fingerprint
        or str(expected.get("topology_role") or "unknown") != current.topology_role.value
        or (expected.get("membership_complete") is True) != current.membership_complete
        # Without this the node an Add-node candidate represents is proven at the
        # verify step and never re-bound at confirm, so a URL that resolves to a
        # different member in between (VIP, round-robin DNS, a re-pointed host)
        # commits an enrollment against a stale proof.
        or str(expected.get("local_node_name") or "") != current.local_node_name
    ):
        raise ClusterOnboardingError(
            "The verified Proxmox identity or topology changed. Restart onboarding and review it again."
        )

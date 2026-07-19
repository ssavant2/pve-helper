from __future__ import annotations

from django.core import signing
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.cluster_forms import (
    ClusterConfirmForm,
    ClusterDisplayNameForm,
    ClusterInspectForm,
    CredentialRotationForm,
    EndpointConfirmForm,
    EndpointInspectForm,
    EndpointTrustConfirmForm,
    TrustCredentialForm,
)
from core.models import ClusterCredential, ClusterTransportTrust, ProxmoxCluster, ProxmoxEndpoint
from core.services.audit_events import record_audit_event
from core.services.cluster_activation import ClusterActivationError, enable_cluster
from core.services.cluster_credentials import ClusterCredentialError, set_cluster_credential
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
from core.services.cluster_trust import TransportTrustError
from core.services.config import endpoint_name_from_url
from core.services.secret_encryption import (
    EncryptionConfigurationError,
    decrypt_secret,
    encrypt_secret,
)

from .common import app_login_required, navigation_context

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
)


_INSPECTION_SALT = "pve-helper.cluster-onboarding.inspection.v1"
_CANDIDATE_SALT = "pve-helper.cluster-onboarding.candidate.v1"
_ENDPOINT_INSPECTION_SALT = "pve-helper.endpoint-onboarding.inspection.v1"
_ENDPOINT_CANDIDATE_SALT = "pve-helper.endpoint-onboarding.candidate.v1"
_TOKEN_MAX_AGE_SECONDS = 10 * 60


@app_login_required
def clusters_overview(request):
    clusters = list(ProxmoxCluster.objects.prefetch_related("endpoints").order_by("display_name", "key"))
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
        {**navigation_context("clusters"), "clusters": clusters},
    )


@app_login_required
def cluster_add(request):
    context = {**navigation_context("clusters"), "step": "identity"}
    if request.method == "GET":
        context["inspect_form"] = ClusterInspectForm()
        return render(request, "core/cluster_add.html", context)

    action = request.POST.get("action", "")
    if action == "inspect":
        form = ClusterInspectForm(request.POST)
        context["inspect_form"] = form
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
                candidate, verified = verify_new_cluster(
                    candidate,
                    expected_certificate_fingerprint=inspection["certificate"]["sha256_fingerprint"],
                )
                candidate_token = _sign(
                    request,
                    _CANDIDATE_SALT,
                    {
                        "kind": "cluster-candidate",
                        "candidate": _candidate_data(candidate),
                        "token_secret_sealed": encrypt_secret(candidate.token_secret),
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
                        "candidate": candidate,
                        "confirm_form": ClusterConfirmForm(initial={"candidate": candidate_token}),
                    }
                )
        return render(request, "core/cluster_add.html", context)

    if action == "confirm":
        form = ClusterConfirmForm(request.POST)
        context.update({"step": "confirm", "confirm_form": form})
        try:
            payload = _load(request, request.POST.get("candidate", ""), _CANDIDATE_SALT, "cluster-candidate")
            candidate = _candidate_from_data(payload["candidate"], decrypt_secret(payload["token_secret_sealed"]))
            context.update({"candidate": candidate, "verified": _verified_from_data(payload["verified"])})
        except CLUSTER_OPERATION_ERRORS as exc:
            form.add_error(None, str(exc))
            return render(request, "core/cluster_add.html", context)
        if form.is_valid():
            try:
                candidate, verified = verify_new_cluster(
                    candidate,
                    expected_certificate_fingerprint=payload["verified"]["certificate"]["sha256_fingerprint"],
                )
                _assert_verified_unchanged(payload["verified"], verified)
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
                return redirect("core:cluster_connection", cluster_key=cluster.key)
        return render(request, "core/cluster_add.html", context)

    raise Http404("Unknown onboarding step")


@app_login_required
def cluster_connection(request, cluster_key: str):
    cluster = get_object_or_404(ProxmoxCluster, key=cluster_key)
    return _render_cluster_connection(request, cluster)


def _render_cluster_connection(request, cluster: ProxmoxCluster, *, operation_error: str = ""):
    credential = ClusterCredential.objects.filter(cluster=cluster).first()
    trust = ClusterTransportTrust.objects.filter(cluster=cluster).first()
    return render(
        request,
        "core/cluster_connection.html",
        {
            **navigation_context("clusters", cluster_key=cluster.key),
            "cluster": cluster,
            "endpoints": cluster.endpoints.order_by("name"),
            "credential": credential,
            "trust": trust,
            "display_name_form": ClusterDisplayNameForm(initial={"display_name": cluster.display_name}),
            "credential_form": CredentialRotationForm(initial={"token_id": credential.token_id if credential else ""}),
            "operation_error": operation_error,
        },
    )


@require_POST
@app_login_required
def cluster_connection_action(request, cluster_key: str):
    cluster = get_object_or_404(ProxmoxCluster, key=cluster_key)
    action = request.POST.get("action", "")
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
        else:
            raise Http404("Unknown cluster action")
    except CLUSTER_OPERATION_ERRORS as exc:
        error = str(exc)

    if error:
        return _render_cluster_connection(request, cluster, operation_error=error)
    return redirect("core:cluster_connection", cluster_key=cluster.key)


@app_login_required
def cluster_endpoint_add(request, cluster_key: str):
    cluster = get_object_or_404(ProxmoxCluster, key=cluster_key)
    context = {
        **navigation_context("clusters", cluster_key=cluster.key),
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
    cluster = get_object_or_404(ProxmoxCluster, key=cluster_key)
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
    }


def _verified_from_data(data: dict) -> VerifiedConnection:
    from core.services.cluster_identity import ObservedClusterIdentity

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
    )


def _assert_verified_unchanged(expected: dict, current: VerifiedConnection) -> None:
    expected_identity = expected.get("identity") or {}
    if (
        str(expected_identity.get("ca_uuid") or "") != current.identity.ca_uuid
        or str(expected_identity.get("ca_fingerprint") or "") != current.identity.ca_fingerprint
    ):
        raise ClusterOnboardingError(
            "The Proxmox CA identity changed after verification. Restart onboarding and review it again."
        )

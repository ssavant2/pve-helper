"""Connections node-enrollment surface (phase 5a1H-1).

The operator's answer to *which nodes may pve-helper look at*. Nothing here filters
publication — that is 5a1I. Until then every enrolled state is configuration whose
only visible effect is on this panel.

Discovery is read through :func:`read_cluster_projection` and nowhere else: a view
may not touch the projection models directly, and may not import the membership
publisher, so :class:`VerifiedConnection` is the only legal carrier for the
candidate-node proof.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError

from django.core import signing
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.cluster_forms import (
    EndpointInspectForm,
    EndpointTrustConfirmForm,
    NodeEnrollmentChangeForm,
    NodeEnrollmentConfirmForm,
)
from core.models import ClusterNodeEnrollment, ClusterTransportTrust, CurrentGuestInventory, ProxmoxEndpoint
from core.services.audit_events import record_audit_event
from core.services.cluster_ca_trust import ClusterCaTrustError, adopt_cluster_ca
from core.services.cluster_enrollment import (
    ClusterEnrollmentError,
    activate_cluster_enrollment,
    change_enrollment_mode,
    enroll_node,
    enrollments_by_node,
    node_change_blockers,
    remove_enrollment,
)
from core.services.cluster_host_refresh import (
    ClusterHostRefreshAlreadyActive,
    ClusterHostRefreshQueueError,
    queue_cluster_host_refresh,
)
from core.services.cluster_identity import ca_uuid_in
from core.services.cluster_onboarding import (
    ClusterOnboardingError,
    ClusterTrustMismatchError,
    inspect_transport,
    persist_endpoint,
    verify_endpoint_for_cluster,
)
from core.services.cluster_projection_read import ClusterProjectionNotFound, read_cluster_projection
from core.services.cluster_scopes import managed_clusters
from core.services.config import endpoint_name_from_url, normalize_endpoint_url

from ..common import app_login_required, navigation_context

logger = logging.getLogger(__name__)

_NODE_INSPECTION_SALT = "cluster-node-inspection"
_NODE_CANDIDATE_SALT = "cluster-node-candidate"
_NODE_IMPACT_SALT = "cluster-node-impact"
_TOKEN_MAX_AGE_SECONDS = 1800
#: How long a prefill may spend in DNS. A suggestion is worth a moment, never a
#: page that appears to hang while a resolver times out on its own schedule.
_DNS_SUGGESTION_TIMEOUT_SECONDS = 2.0

#: Row states. Derived from projection presence × enrollment row, never stored.
STATE_MANAGED = "managed"
STATE_SAFETY_ONLY = "safety_only"
STATE_DISCOVERED = "discovered"
#: Discovered, unenrolled, but pve-helper already has a transport to it. Worth its
#: own state because "not added" reads as "nothing is known about this node", while
#: the truth is narrower and more actionable: the connection works, only the
#: publication decision is missing.
STATE_DISCOVERED_WITH_ENDPOINT = "discovered_with_endpoint"
STATE_ENROLLED_ABSENT = "enrolled_absent"
STATE_ENROLLED_UNDISCOVERED = "enrolled_undiscovered"

STATE_LABELS = {
    STATE_MANAGED: "Managed",
    STATE_SAFETY_ONLY: "Safety only",
    STATE_DISCOVERED: "Discovered, not added",
    STATE_DISCOVERED_WITH_ENDPOINT: "Discovered, endpoint ready",
    STATE_ENROLLED_ABSENT: "Enrolled, no longer present",
    STATE_ENROLLED_UNDISCOVERED: "Enrolled, not yet discovered",
}


def _actor_key(request) -> str:
    user = getattr(request, "user", None)
    return str(user.pk) if user is not None and getattr(user, "is_authenticated", False) else "anonymous-dev"


def _actor(request):
    user = getattr(request, "user", None)
    return user if user is not None and getattr(user, "is_authenticated", False) else None


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


def _candidate_url_suggestion(ring_address: str) -> tuple[str, str]:
    """A *suggestion* built from the ring address, with its confirmed name if DNS has one.

    Returns ``(url, resolved_name)``; the name is empty unless DNS answered, and the
    template says which of the two the field was filled from.

    Still never a *synthesized* name — no ``f"{node}.{domain}"`` assembled from a
    node name and a sibling endpoint's suffix, which is a guess wearing a hostname's
    clothes. A forward-confirmed PTR is the opposite: DNS is asked, and its answer is
    only used if resolving that name returns the address we started from. An
    unconfirmed PTR is discarded, because a reverse zone is often controlled by
    whoever holds the address and is not evidence on its own.

    Preferring the confirmed name matters beyond neatness: an IP URL can never match
    a publicly trusted certificate, so an IP suggestion silently steers a `public`
    trust profile into a rejected chain. The address remains the fallback — it is
    what the provider actually reported — and neither form is proven reachable or
    proven to be this node. That is what the inspection and the ``local=1`` check
    after it are for.
    """

    ring_address = str(ring_address or "").strip()
    if not ring_address or "/" in ring_address or " " in ring_address:
        return "", ""
    resolved = _forward_confirmed_name(ring_address)
    host = resolved or (f"[{ring_address}]" if ":" in ring_address else ring_address)
    return f"https://{host}:8006", resolved


def _forward_confirmed_name(address: str) -> str:
    """The PTR name for `address`, but only if it resolves back to `address`.

    Off-thread with a hard deadline: this runs while rendering a page, and a resolver
    that is unreachable rather than empty answers by hanging. `gethostbyaddr` is a
    libc call that ignores socket timeouts, so the timeout has to be imposed from
    outside it. A slow lookup then costs the suggestion, not the page.
    """

    try:
        parsed_address = ipaddress.ip_address(address)
    except ValueError:
        # Already a name — the provider reported it, and there is nothing to confirm
        # it against that is stronger than what the provider said.
        return ""

    def lookup() -> str:
        name = socket.gethostbyaddr(address)[0].strip().rstrip(".")
        if not name:
            return ""
        family = socket.AF_INET6 if parsed_address.version == 6 else socket.AF_INET
        confirmed = {info[4][0] for info in socket.getaddrinfo(name, None, family, socket.SOCK_STREAM)}
        return name if address in confirmed else ""

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(lookup).result(timeout=_DNS_SUGGESTION_TIMEOUT_SECONDS)
    except (OSError, FuturesTimeoutError, UnicodeError):
        return ""
    finally:
        # `wait=True` — the `with` form's default — would block here for exactly as
        # long as the timeout above just refused to wait, which is the whole point.
        pool.shutdown(wait=False)


def _trust_diagnosis(exc: Exception):
    """The structured half of a trust rejection, or nothing for any other failure.

    Every onboarding failure keeps rendering as its sentence; only this one has a
    comparison to lay out, and only the class that carries one may claim the panel.
    """

    return exc.diagnosis if isinstance(exc, ClusterTrustMismatchError) else None


def _endpoints_by_node(cluster) -> dict[str, ProxmoxEndpoint]:
    """This cluster's endpoints keyed by the node each one answers as.

    A scan stores the answering node in `details["node"]`, which is the only proof of
    which member a URL actually reaches — the endpoint's own name is operator-chosen
    and a VIP or alias may not be a node name at all. An endpoint that has never
    answered contributes nothing rather than being guessed at from its name.
    """

    by_node: dict[str, ProxmoxEndpoint] = {}
    for endpoint in ProxmoxEndpoint.objects.filter(cluster=cluster).order_by("name"):
        node_name = str((endpoint.details or {}).get("node") or "")
        if node_name:
            by_node.setdefault(node_name, endpoint)
    return by_node


def node_enrollment_rows(cluster) -> list[dict]:
    """Compose the Nodes panel from one projection read plus the enrollment rows.

    Two directions have to be walked, not one. Iterating discovery alone hides an
    enrollment created at onboarding before the first membership reconcile, leaving
    a row the operator can neither hide nor remove.
    """

    try:
        projection = read_cluster_projection(cluster.key)
    except ClusterProjectionNotFound:
        discovered = {}
    else:
        discovered = {node.node_name: node for node in projection.nodes}

    enrollments = enrollments_by_node(cluster)
    # Endpoints are a separate axis from enrollment: one is a transport, the other a
    # publication decision, and adding an endpoint deliberately never enrols. The
    # panel still has to *show* both, or a node with a working, recently scanned
    # endpoint is indistinguishable from one only ever seen through its neighbours.
    # Resolved from the endpoint rows rather than from `onboarded_via_endpoint`,
    # which exists only where an enrollment does.
    endpoints_by_node = _endpoints_by_node(cluster)

    rows = []
    for node_name in sorted(set(discovered) | set(enrollments)):
        node = discovered.get(node_name)
        enrollment = enrollments.get(node_name)
        endpoint = endpoints_by_node.get(node_name)
        if enrollment is None:
            state = STATE_DISCOVERED_WITH_ENDPOINT if endpoint else STATE_DISCOVERED
        elif node is None:
            state = STATE_ENROLLED_UNDISCOVERED
        elif not node.present:
            state = STATE_ENROLLED_ABSENT
        elif enrollment.mode == ClusterNodeEnrollment.Mode.SAFETY_ONLY:
            state = STATE_SAFETY_ONLY
        else:
            state = STATE_MANAGED
        rows.append(
            {
                "node_name": node_name,
                "state": state,
                "state_label": STATE_LABELS[state],
                "enrolled": enrollment is not None,
                "mode": enrollment.mode if enrollment else "",
                "present": bool(node.present) if node else False,
                "online": bool(node.online) if node else False,
                "nodeid": node.nodeid if node else None,
                "reported_ring_address": node.reported_ring_address if node else "",
                "last_discovered_at": node.last_discovered_at if node else None,
                "first_discovered_at": node.first_discovered_at if node else None,
                "endpoint": (enrollment.onboarded_via_endpoint if enrollment else None) or endpoint,
            }
        )
    return rows


def unattached_endpoints(endpoints, node_rows) -> list[ProxmoxEndpoint]:
    """This cluster's endpoints that no node row accounts for.

    The second row type of the merged panel, and the reason merging is safe at all.
    The node↔endpoint pairing is partial in both directions and unstable: an endpoint
    that has never answered belongs to no node, a VIP may answer as a different member
    each scan, and a node reached by two URLs is shown with one of them. Every
    endpoint those cases would have dropped lands here instead of vanishing with the
    panel it used to have — which is exactly when transport is what the operator came
    to look at.
    """

    claimed = {row["endpoint"].pk for row in node_rows if row["endpoint"] is not None}
    return [endpoint for endpoint in endpoints if endpoint.pk not in claimed]


def _impact(cluster, node_name: str, action: str) -> dict:
    """What the operator loses by hiding or removing this node, stated before the act."""

    guests = list(
        CurrentGuestInventory.objects.filter(cluster=cluster, node=node_name)
        .order_by("object_type", "vmid")
        .values_list("object_type", "vmid", "name")[:50]
    )
    guest_count = CurrentGuestInventory.objects.filter(cluster=cluster, node=node_name).count()
    consequences = [
        f"{guest_count} guest(s) currently placed on {node_name} disappear from pve-helper's operational views.",
        "Nothing changes on Proxmox itself. This is a local policy change only.",
    ]
    if action == "remove":
        consequences.append(
            "pve-helper stops reading this node entirely, so shared storage it could consume drops to "
            "'unknown' coverage. File actions on those storages then ask for an acknowledgement naming "
            "the node — they are not blocked."
        )
    else:
        consequences.append(
            "pve-helper keeps reading this node for disk references, so storage coverage and orphan "
            "classification are unaffected."
        )
    return {
        "node_name": node_name,
        "action": action,
        "guest_count": guest_count,
        "guests": [{"object_type": kind, "vmid": vmid, "name": name} for kind, vmid, name in guests],
        "consequences": consequences,
        "blockers": node_change_blockers(cluster, node_name),
    }


@app_login_required
def cluster_node_add(request, cluster_key: str):
    """Add node: inspect → trust → verify → confirm, bound to one discovered node."""

    cluster = get_object_or_404(managed_clusters(), key=cluster_key)
    node_name = str(request.GET.get("node") or request.POST.get("node_name") or "").strip()
    if not node_name:
        raise Http404("Add node requires a discovered node.")
    context = {
        **navigation_context(
            "clusters",
            page_title=(cluster.display_name, f"Add node {node_name}"),
            cluster_key=cluster.key,
        ),
        "cluster": cluster,
        "node_name": node_name,
        "step": "inspect",
    }

    if request.method == "GET":
        suggestion = ""
        for row in node_enrollment_rows(cluster):
            if row["node_name"] == node_name:
                suggestion, resolved_name = _candidate_url_suggestion(row["reported_ring_address"])
                context["reported_ring_address"] = row["reported_ring_address"]
                context["resolved_ring_hostname"] = resolved_name
                break
        context["inspect_form"] = EndpointInspectForm(initial={"endpoint_url": suggestion, "endpoint_name": node_name})
        return render(request, "core/cluster_node_add.html", context)

    action = request.POST.get("action", "")
    if request.POST.get("trust_ca"):
        # A submit button inside the trust step's own form, not a nested form: the
        # diagnosis renders inside that form so the evidence follows the sentence
        # that introduces it, and a <form> inside a <form> is not valid HTML. Its own
        # field name rather than a second `action` value, because two `action` inputs
        # in one submission make the dispatch depend on field order.
        return _adopt_cluster_ca(request, cluster, node_name, context)

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
                    _NODE_INSPECTION_SALT,
                    {
                        "kind": "node-inspection",
                        "cluster_key": cluster.key,
                        "node_name": node_name,
                        "endpoint_url": endpoint_url,
                        "endpoint_name": endpoint_name,
                        "certificate": {
                            "subject": certificate.subject,
                            "issuer": certificate.issuer,
                            "sha256_fingerprint": certificate.sha256_fingerprint,
                        },
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
        return render(request, "core/cluster_node_add.html", context)

    if action == "verify":
        form = EndpointTrustConfirmForm(request.POST)
        context.update({"step": "trust", "trust_form": form})
        try:
            inspection = _load(request, request.POST.get("inspection", ""), _NODE_INSPECTION_SALT, "node-inspection")
            if inspection["cluster_key"] != cluster.key or inspection["node_name"] != node_name:
                raise ClusterOnboardingError("This inspection belongs to a different cluster or node.")
        except ClusterOnboardingError as exc:
            form.add_error(None, str(exc))
            return render(request, "core/cluster_node_add.html", context)
        context["endpoint_meta"] = inspection
        if form.is_valid():
            try:
                verified = verify_endpoint_for_cluster(
                    cluster,
                    endpoint_url=inspection["endpoint_url"],
                    endpoint_name=inspection["endpoint_name"],
                    expected_certificate_fingerprint=inspection["certificate"]["sha256_fingerprint"],
                )
                _assert_represents_node(verified, node_name)
                candidate = _sign(
                    request,
                    _NODE_CANDIDATE_SALT,
                    {
                        **inspection,
                        "kind": "node-candidate",
                        "local_node_name": verified.local_node_name,
                        "ca_uuid": verified.identity.ca_uuid,
                    },
                )
            except ClusterOnboardingError as exc:
                _record_enrollment_failure(request, cluster, node_name, str(exc))
                form.add_error(None, str(exc))
                context["trust_diagnosis"] = _trust_diagnosis(exc)
            else:
                context.update(
                    {
                        "step": "confirm",
                        "verified": verified,
                        "confirm_form": NodeEnrollmentConfirmForm(
                            initial={"candidate": candidate, "mode": ClusterNodeEnrollment.Mode.MANAGED}
                        ),
                    }
                )
        return render(request, "core/cluster_node_add.html", context)

    if action == "confirm":
        form = NodeEnrollmentConfirmForm(request.POST)
        context.update({"step": "confirm", "confirm_form": form})
        try:
            payload = _load(request, request.POST.get("candidate", ""), _NODE_CANDIDATE_SALT, "node-candidate")
            if payload["cluster_key"] != cluster.key or payload["node_name"] != node_name:
                raise ClusterOnboardingError("This candidate belongs to a different cluster or node.")
            context["endpoint_meta"] = payload
        except ClusterOnboardingError as exc:
            form.add_error(None, str(exc))
            return render(request, "core/cluster_node_add.html", context)
        if form.is_valid():
            try:
                write = _commit_node_enrollment(request, cluster, node_name, payload, form.cleaned_data["mode"])
            except (ClusterOnboardingError, ClusterEnrollmentError) as exc:
                _record_enrollment_failure(request, cluster, node_name, str(exc))
                form.add_error(None, str(exc))
                # The final step re-verifies, so trust can be refused here too — a
                # certificate can be replaced between the two calls.
                context["trust_diagnosis"] = _trust_diagnosis(exc)
            else:
                _queue_node_reconciliation(request, cluster, node_name, write)
                return redirect("core:cluster_connection", cluster_key=cluster.key)
        return render(request, "core/cluster_node_add.html", context)

    raise Http404("Unknown node enrollment step")


def _assert_represents_node(verified, node_name: str) -> None:
    """The candidate must *be* the chosen node, proven by the ``local=1`` row.

    An empty ``local_node_name`` means the cluster status payload was not strict
    enough to normalize. Ordinary onboarding tolerates that deliberately; enrollment
    does not extend the tolerance, because the alternative is inferring node identity
    from an endpoint name — the exact inference this whole feature exists to remove.
    """

    if not verified.local_node_name:
        raise ClusterOnboardingError(
            "This endpoint's cluster status response did not identify which node served it "
            "(no 'local' marker), so pve-helper cannot prove it represents "
            f"'{node_name}'. Enrollment is refused rather than guessed."
        )
    if verified.local_node_name != node_name:
        raise ClusterOnboardingError(
            f"This endpoint represents node '{verified.local_node_name}', not '{node_name}'. "
            "Enter the URL of the node you are adding."
        )


def _commit_node_enrollment(request, cluster, node_name: str, payload: dict, mode: str):
    """Re-verify, then persist the endpoint and the enrollment in one transaction."""

    verified = verify_endpoint_for_cluster(
        cluster,
        endpoint_url=payload["endpoint_url"],
        endpoint_name=payload["endpoint_name"],
        expected_certificate_fingerprint=payload["certificate"]["sha256_fingerprint"],
    )
    # Re-bind the proof: a URL can resolve to a different member between the verify
    # step and this one (VIP, round-robin DNS, a re-pointed host).
    _assert_represents_node(verified, node_name)
    if payload.get("local_node_name") and payload["local_node_name"] != verified.local_node_name:
        raise ClusterOnboardingError(
            "The node this endpoint represents changed since it was verified. Start over and review it again."
        )
    if payload.get("ca_uuid") and payload["ca_uuid"] != verified.identity.ca_uuid:
        raise ClusterOnboardingError(
            "The verified Proxmox identity changed since this candidate was approved. Start over."
        )

    with transaction.atomic():
        # The normalized-URL uniqueness constraint is installation-wide, so an
        # already-registered URL is looked up the same way and its owner checked:
        # reuse it when it is this cluster's transport, refuse when it is another's.
        existing = ProxmoxEndpoint.objects.filter(
            normalized_url=normalize_endpoint_url(payload["endpoint_url"])
        ).first()
        if existing is not None:
            if existing.cluster_id != cluster.pk:
                raise ClusterOnboardingError("That URL is already registered as an endpoint of another connection.")
            # `persist_endpoint` would reject the duplicate, and a second row for the
            # same URL is not what the spec's "persist/update" means.
            endpoint = existing
        else:
            endpoint = persist_endpoint(
                cluster,
                endpoint_url=payload["endpoint_url"],
                endpoint_name=payload["endpoint_name"],
            )
        write = enroll_node(
            cluster,
            node_name=node_name,
            mode=mode,
            actor=_actor(request),
            endpoint=endpoint,
        )
        record_audit_event(
            request,
            action="cluster.node.enrolled",
            object_type="cluster_node",
            object_id=f"{cluster.key}:{node_name}",
            cluster=cluster,
            details={
                "cluster_key": cluster.key,
                "node_name": node_name,
                "mode": mode,
                "endpoint_name": endpoint.name,
                "endpoint_url": endpoint.normalized_url,
                "proven_local_node": verified.local_node_name,
                "ca_uuid": verified.identity.ca_uuid,
                "enrollment_generation": write.generation,
                "already_enrolled": not write.changed,
            },
        )
    return write


def _adopt_cluster_ca(request, cluster, node_name: str, context: dict):
    """Trust this cluster's own CA, then return the operator to the trust step.

    The wizard's own step, not a Connections detour: the operator is here because
    this node was refused here, and the repair is only offered when that refusal said
    it applies. It does not verify the node afterwards — approving transport and
    sending a credential stay two separate confirmations, as they are at onboarding.
    """
    # The form stays unbound: this step approves transport, so re-validating the
    # node-verification checkbox would report an unticked box as the reason the CA
    # was not trusted. Its own message key instead, rendered where the form's is.
    context.update(
        {
            "step": "trust",
            "trust_form": EndpointTrustConfirmForm(initial={"inspection": request.POST.get("inspection", "")}),
        }
    )
    try:
        inspection = _load(request, request.POST.get("inspection", ""), _NODE_INSPECTION_SALT, "node-inspection")
        if inspection["cluster_key"] != cluster.key or inspection["node_name"] != node_name:
            raise ClusterOnboardingError("This inspection belongs to a different cluster or node.")
    except ClusterOnboardingError as exc:
        context["ca_trust_error"] = str(exc)
        return render(request, "core/cluster_node_add.html", context)

    context["endpoint_meta"] = inspection
    if ca_uuid_in(inspection["certificate"]["issuer"]) != cluster.discovered_ca_uuid:
        # Decision 8 enforced where the state changes, not only where the page is
        # drawn. The signed inspection carries the certificate this endpoint actually
        # presented, so the same condition the offer was rendered from is re-asked
        # from evidence the operator cannot edit — a rendered button is not authority.
        context["ca_trust_error"] = (
            f"{inspection['endpoint_name']} did not present a certificate issued by this connection's pinned "
            "Proxmox CA, so trusting that CA would not accept it. Nothing was changed."
        )
        return render(request, "core/cluster_node_add.html", context)

    try:
        adopted = adopt_cluster_ca(cluster)
    except ClusterCaTrustError as exc:
        record_audit_event(
            request,
            action="cluster.transport.approve",
            object_type="cluster",
            object_id=cluster.key,
            cluster=cluster,
            outcome="failed",
            details={"cluster_key": cluster.key, "mode": ClusterTransportTrust.Mode.PUBLIC_PLUS_CA, "reason": str(exc)},
        )
        context["ca_trust_error"] = str(exc)
        return render(request, "core/cluster_node_add.html", context)

    record_audit_event(
        request,
        action="cluster.transport.approve",
        object_type="cluster",
        object_id=cluster.key,
        cluster=cluster,
        outcome="success",
        details={
            "cluster_key": cluster.key,
            "mode": ClusterTransportTrust.Mode.PUBLIC_PLUS_CA,
            **adopted.as_details(),
        },
    )
    context["ca_adopted"] = adopted
    return render(request, "core/cluster_node_add.html", context)


def _record_enrollment_failure(request, cluster, node_name: str, message: str) -> None:
    record_audit_event(
        request,
        action="cluster.node.enrollment_failed",
        object_type="cluster_node",
        object_id=f"{cluster.key}:{node_name}",
        cluster=cluster,
        outcome="failed",
        details={"cluster_key": cluster.key, "node_name": node_name, "reason": message},
    )


def _queue_node_reconciliation(request, cluster, node_name: str, write) -> None:
    """Targeted read-only refresh for the node just enrolled.

    Deliberately after the commit and deliberately non-fatal. The enrollment is
    already durable; a refusal here (retired/disabled cluster, a 5a1G transition in
    flight, a refresh already running for this scope) is a scheduling condition, not
    a reason to undo the operator's configuration change.
    """

    if not write.changed:
        return
    try:
        queue_cluster_host_refresh(cluster=cluster, scope="node_runtime", node_name=node_name, request=request)
    except (ClusterHostRefreshQueueError, ClusterHostRefreshAlreadyActive) as exc:
        logger.info(
            "Node enrollment committed but its targeted refresh was not queued",
            extra={"cluster_key": cluster.key, "node_name": node_name, "reason": str(exc)},
        )


@require_POST
@app_login_required
def cluster_enrollment_activate(request, cluster_key: str):
    """Review the discovered set once, then move this connection off legacy publication.

    Two steps for the same reason every other irreversible control here has two: the
    version can never be cleared, so the operator sees the exact set and its
    consequence before it becomes permanent.
    """

    from .connections import _render_cluster_connection

    cluster = get_object_or_404(managed_clusters(), key=cluster_key)
    if cluster.enrollment_contract_version >= 1:
        return _render_cluster_connection(
            request, cluster, operation_error="This connection already uses the enrollment contract."
        )

    if request.POST.get("step") != "confirm":
        selections = {
            name: mode
            for name, mode in (
                (row["node_name"], request.POST.get(f"mode_{row['node_name']}", ""))
                for row in node_enrollment_rows(cluster)
                if row["present"]
            )
            if mode in {"managed", "safety_only"}
        }
        if not selections:
            return _render_cluster_connection(
                request,
                cluster,
                operation_error=(
                    "Select at least one node. Activating with an empty set would publish nothing for this connection."
                ),
            )
        return render(
            request,
            "core/cluster_enrollment_activate.html",
            {
                **navigation_context(
                    "clusters",
                    page_title=(cluster.display_name, "Activate enrollment"),
                    cluster_key=cluster.key,
                ),
                "cluster": cluster,
                "selections": sorted(selections.items()),
                "hidden_nodes": sorted(name for name, mode in selections.items() if mode == "safety_only"),
                "unselected": sorted(
                    row["node_name"]
                    for row in node_enrollment_rows(cluster)
                    if row["present"] and row["node_name"] not in selections
                ),
                "activate_form": NodeEnrollmentChangeForm(
                    initial={
                        "impact": _sign(
                            request,
                            _NODE_IMPACT_SALT,
                            {
                                "kind": "enrollment-activation",
                                "cluster_key": cluster.key,
                                "selections": selections,
                            },
                        )
                    }
                ),
            },
        )

    form = NodeEnrollmentChangeForm(request.POST)
    if not form.is_valid():
        return _render_cluster_connection(request, cluster, operation_error="Confirm the reviewed set first.")
    try:
        payload = _load(request, form.cleaned_data["impact"], _NODE_IMPACT_SALT, "enrollment-activation")
        if payload["cluster_key"] != cluster.key:
            raise ClusterOnboardingError("This confirmation belongs to a different connection.")
    except ClusterOnboardingError as exc:
        return _render_cluster_connection(request, cluster, operation_error=str(exc))

    try:
        result = activate_cluster_enrollment(cluster, selections=payload["selections"], actor=_actor(request))
    except ClusterEnrollmentError as exc:
        return _render_cluster_connection(request, cluster, operation_error=str(exc))

    for node_name, mode in result.enrolled:
        record_audit_event(
            request,
            action="cluster.node.enrolled",
            object_type="cluster_node",
            object_id=f"{cluster.key}:{node_name}",
            cluster=cluster,
            details={
                "cluster_key": cluster.key,
                "node_name": node_name,
                "mode": mode,
                "via": "enrollment_activation",
                "enrollment_contract_version": result.contract_version,
                "enrollment_generation": result.generation,
            },
        )
    return redirect("core:cluster_connection", cluster_key=cluster.key)


@require_POST
@app_login_required
def cluster_node_action(request, cluster_key: str, node_name: str):
    """Hide, unhide or remove one enrollment. Two steps: preview, then signed confirm."""

    from .connections import _render_cluster_connection

    cluster = get_object_or_404(managed_clusters(), key=cluster_key)
    action = request.POST.get("action", "")
    if action not in {"hide", "manage", "remove"}:
        raise Http404("Unknown node enrollment action")

    if request.POST.get("step") != "confirm":
        impact = _impact(cluster, node_name, action)
        return render(
            request,
            "core/cluster_node_change.html",
            {
                **navigation_context(
                    "clusters",
                    page_title=(cluster.display_name, f"{action.title()} node {node_name}"),
                    cluster_key=cluster.key,
                ),
                "cluster": cluster,
                "node_name": node_name,
                "action": action,
                "impact": impact,
                "change_form": NodeEnrollmentChangeForm(
                    initial={
                        "impact": _sign(
                            request,
                            _NODE_IMPACT_SALT,
                            {
                                "kind": "node-impact",
                                "cluster_key": cluster.key,
                                "node_name": node_name,
                                "action": action,
                                "blockers": impact["blockers"],
                            },
                        )
                    }
                ),
            },
        )

    form = NodeEnrollmentChangeForm(request.POST)
    if not form.is_valid():
        return _render_cluster_connection(request, cluster, operation_error="Confirm the listed consequences first.")
    try:
        payload = _load(request, form.cleaned_data["impact"], _NODE_IMPACT_SALT, "node-impact")
        if payload["cluster_key"] != cluster.key or payload["node_name"] != node_name or payload["action"] != action:
            raise ClusterOnboardingError("This confirmation belongs to a different change.")
    except ClusterOnboardingError as exc:
        return _render_cluster_connection(request, cluster, operation_error=str(exc))

    reason = form.cleaned_data.get("reason", "")
    try:
        if action == "remove":
            write = remove_enrollment(cluster, node_name=node_name)
            audit_action = "cluster.node.removed"
        else:
            mode = ClusterNodeEnrollment.Mode.SAFETY_ONLY if action == "hide" else ClusterNodeEnrollment.Mode.MANAGED
            write = change_enrollment_mode(
                cluster, node_name=node_name, mode=mode, actor=_actor(request), reason=reason
            )
            audit_action = "cluster.node.mode_changed"
    except ClusterEnrollmentError as exc:
        return _render_cluster_connection(request, cluster, operation_error=str(exc))

    if write.changed:
        record_audit_event(
            request,
            action=audit_action,
            object_type="cluster_node",
            object_id=f"{cluster.key}:{node_name}",
            cluster=cluster,
            details={
                "cluster_key": cluster.key,
                "node_name": node_name,
                "previous_mode": write.previous_mode,
                "mode": "" if action == "remove" else write.enrollment.mode,
                "reason": reason,
                "enrollment_generation": write.generation,
            },
        )
    return redirect("core:cluster_connection", cluster_key=cluster.key)

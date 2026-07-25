"""Storage-mount registration and binding."""

from __future__ import annotations

import uuid
from pathlib import Path

from django.db.models import Count

from core.models import (
    ClusterStorage,
    ClusterStorageMount,
    ClusterStorageVolumeObservation,
    CurrentGuestInventory,
)
from core.services.filesystem import mountinfo_mounts
from core.services.storage_backends import backend_profile
from core.services.storage_mounts import (
    StorageMountError,
    bind_storage_mount,
    compare_mount_source,
    derived_backend_identity,
    mount_health,
    near_match_mounts,
    normalized_backend_identity,
    unbind_storage_mount,
)
from core.services.storage_paths import (
    default_trash_relative_path,
    normalized_relative_path,
)

from ..common import (
    StorageMount,
    app_login_required,
    navigation_context,
    record_audit_event,
    render,
    settings,
)
from ._shared import (
    _GUESTS_SHOWN_IN_CONFIRM,
)


def _mount_candidates() -> list[dict[str, str]]:
    root = Path(settings.PVE_HELPER_STORAGE_CONTAINER_ROOT)
    mounted = {mount.mount_point: mount for mount in mountinfo_mounts()}
    try:
        children = [child for child in root.iterdir() if child.is_dir() and not child.is_symlink()]
    except OSError:
        return []
    rows = []
    for child in sorted(children, key=lambda item: item.name.lower()):
        # A directory that is not a mount point of its own has no source, and the
        # empty string is what makes the backend comparison stand down.
        mount = mounted.get(str(child))
        rows.append(
            {
                "relative_path": child.relative_to(root).as_posix(),
                "filesystem_type": mount.filesystem_type if mount else "directory",
                "source": mount.source if mount else "",
            }
        )
    return rows


def _guest_references_by_storage(bindings) -> dict[tuple[int, str], list[tuple[bool, str, str]]]:
    """Which guests reference each datastore, resolved in one pass per cluster.

    A volid is ``storage:content/file``, so the segment before the first colon
    names the datastore exactly. Splitting once per reference and grouping by
    that name answers every binding at the same time, where testing each guest
    against one binding's ``storage_id:`` prefix re-read the cluster's whole
    guest table once per binding — several full scans inside a passive page
    render, growing with the number of registered associations.
    """
    cluster_ids = {binding.cluster_storage.cluster_id for binding in bindings}
    references: dict[tuple[int, str], list[tuple[bool, str, str]]] = {}
    if not cluster_ids:
        return references
    guests = CurrentGuestInventory.objects.filter(cluster_id__in=cluster_ids).only(
        "cluster_id", "object_type", "vmid", "status", "disk_references"
    )
    for guest in guests:
        entry = (guest.status == "running", f"{guest.object_type}:{guest.vmid}", guest.status or "unknown")
        for storage_id in {str(ref).split(":", 1)[0] for ref in guest.disk_references or [] if ":" in str(ref)}:
            references.setdefault((guest.cluster_id, storage_id), []).append(entry)
    # Running guests first: they are the ones the operator must see, and they
    # must never be the entries a display cap silently drops.
    for entries in references.values():
        entries.sort(key=lambda item: (not item[0], item[1]))
    return references


def _volume_counts_by_storage(bindings) -> dict[int, int]:
    """Recorded volume observations per datastore, as one grouped aggregate."""
    definition_ids = {binding.cluster_storage_id for binding in bindings}
    if not definition_ids:
        return {}
    counts = (
        ClusterStorageVolumeObservation.objects.filter(cluster_storage_id__in=definition_ids)
        .values("cluster_storage_id")
        .order_by()
        .annotate(total=Count("id"))
    )
    return {row["cluster_storage_id"]: row["total"] for row in counts}


def _binding_rows() -> list[dict[str, object]]:
    """Registered associations, each with what it is currently carrying.

    Unbinding is one click and immediately removes file browsing, writes and
    API-catalog classification for that datastore, so the confirmation has to
    say what is actually in use rather than only asking whether the operator is
    sure. The evidence comes from the published projection — a passive page
    render must not fan out to Proxmox to answer this.
    """
    rows = []
    bindings = list(
        ClusterStorageMount.objects.select_related("cluster_storage__cluster", "mount")
        .filter(
            cluster_storage__cluster__retired_at__isnull=True,
            cluster_storage__unmanaged_at__isnull=True,
        )
        .order_by("cluster_storage__cluster__display_name", "cluster_storage__storage_id", "node")
    )
    guests_by_storage = _guest_references_by_storage(bindings)
    volume_counts = _volume_counts_by_storage(bindings)
    for binding in bindings:
        definition = binding.cluster_storage
        guests = guests_by_storage.get((definition.cluster_id, definition.storage_id), [])
        running = sum(1 for is_running, _label, _status in guests if is_running)
        volumes = volume_counts.get(definition.pk, 0)
        usage = []
        if definition.content:
            usage.append("content types " + ", ".join(definition.content))
        if volumes:
            usage.append(f"{volumes} catalog volume(s)")
        if guests:
            shown = ", ".join(f"{label} ({status})" for _is_running, label, status in guests[:_GUESTS_SHOWN_IN_CONFIRM])
            hidden = len(guests) - _GUESTS_SHOWN_IN_CONFIRM
            if hidden > 0:
                shown += f", and {hidden} more"
            running_note = f", {running} of them running" if running else ""
            usage.append(f"referenced by {len(guests)} guest(s){running_note}: {shown}")
        label = f"{definition.cluster.display_name} \u00b7 {definition.storage_id}"
        in_use = "; ".join(usage) or "no catalog volumes or guest references are currently recorded"
        rows.append(
            {
                "binding": binding,
                "guest_count": len(guests),
                "confirm": (
                    f"{label} is currently in use: {in_use}. "
                    "Removing the association stops file browsing, file writes and orphan "
                    "classification for this datastore until a mount is registered again."
                ),
                "confirm_second": (f"Are you really sure? {label} is still in use: {in_use}."),
            }
        )
    return rows


@app_login_required
def storage_mount_register(request):
    definitions = list(
        ClusterStorage.objects.select_related("cluster")
        .filter(
            cluster__enabled=True,
            cluster__retired_at__isnull=True,
            unmanaged_at__isnull=True,
            present=True,
        )
        .prefetch_related("node_states")
        .order_by("cluster__display_name", "storage_id")
    )
    definitions = [row for row in definitions if backend_profile(row.storage_type).filesystem_eligible]
    definition_options = []
    for row in definitions:
        scope = "Shared" if row.shared else "Node-local"
        # Only an instance that is present, active and enabled can be the one a
        # host mount represents; anything else would fail the same server-side
        # check the operator is trying to satisfy.
        nodes = sorted(
            state.node for state in row.node_states.all() if state.present and state.active and state.enabled
        )
        definition_options.append(
            {
                "pk": row.pk,
                "label": f"{row.cluster.display_name} \u00b7 {row.storage_id} ({row.storage_type}) \u2014 {scope}",
                "shared": bool(row.shared),
                "derived_identity": derived_backend_identity(row),
                "nodes": nodes,
                # Node-local storage with no usable instance cannot be bound at
                # all; say so in the option rather than on submit.
                "unavailable_reason": "" if (row.shared or nodes) else "no active node instance",
            }
        )
    errors: list[str] = []
    warnings: list[str] = []
    form_values: dict[str, str] = {}
    confirm_distinct_backend = False
    confirm_backend_mismatch = False
    confirmed_mismatch = False
    confirmed_distinct = False
    # A question about the values in the form belongs beside them, not in a banner
    # above the whole page: the answer is a button in this form, and the operator
    # has to re-read the two fields to give it. Page-level notices stay for things
    # that are not about a pending submission.
    confirmation_headline = ""
    confirmation_details: list[str] = []
    if request.method == "POST":
        if request.POST.get("action") == "remove_binding":
            try:
                binding_id = int(str(request.POST.get("binding_id") or ""))
            except ValueError:
                binding_id = 0
            binding = (
                ClusterStorageMount.objects.select_related("cluster_storage__cluster", "mount")
                .filter(
                    pk=binding_id,
                    cluster_storage__cluster__retired_at__isnull=True,
                    cluster_storage__unmanaged_at__isnull=True,
                )
                .first()
            )
            if binding is None:
                errors.append("Mount association no longer exists.")
            else:
                cluster = binding.cluster_storage.cluster
                mount = binding.mount
                try:
                    unbind_storage_mount(binding)
                except StorageMountError as exc:
                    errors.append(str(exc))
                else:
                    record_audit_event(
                        request=request,
                        user=request.user,
                        username=request.user.get_username(),
                        action="storage.mount.unregistered",
                        object_type="storage_mount",
                        object_id=mount.mount_ref,
                        cluster=cluster,
                        details={
                            "cluster_key": cluster.key,
                            "storage_id": binding.cluster_storage.storage_id,
                            "mount_ref": mount.mount_ref,
                            "scope": binding.node or "shared",
                            # The pairing, not just its key: Recent Tasks and Audit
                            # are the only places this outcome is reported now, so
                            # they carry both halves of what was undone.
                            "display_name": mount.display_name,
                            "path": mount.path,
                        },
                    )
        else:
            definition = next(
                (row for row in definitions if str(row.pk) == str(request.POST.get("cluster_storage"))),
                None,
            )
            relative = str(request.POST.get("relative_path") or "")
            node = str(request.POST.get("node") or "").strip()
            display_name = str(request.POST.get("display_name") or "").strip()
            submitted_identity = str(request.POST.get("backend_identity") or "").strip()
            confirmed_distinct = request.POST.get("confirm_distinct_backend") == "1"
            confirmed_mismatch = request.POST.get("confirm_backend_mismatch") == "1"
            form_values = {
                "cluster_storage": str(request.POST.get("cluster_storage") or ""),
                "relative_path": relative,
                "node": node,
                "display_name": display_name,
                "backend_identity": submitted_identity,
            }
            try:
                backend_identity = normalized_backend_identity(submitted_identity)
            except StorageMountError as exc:
                backend_identity = ""
                backend_identity_error = True
                errors.append(str(exc))
            else:
                backend_identity_error = False
            derived = derived_backend_identity(definition) if definition is not None else ""
            identity_source = (
                StorageMount.IdentitySource.DERIVED
                if derived and backend_identity == derived
                else StorageMount.IdentitySource.MANUAL
            )
            candidates = {item["relative_path"]: item for item in _mount_candidates()}
            if definition is None:
                errors.append("Choose a current file-based cluster storage.")
            if relative not in candidates:
                errors.append("Choose a directory currently visible beneath /storages.")
            if not display_name:
                errors.append("Display name is required.")
            if not backend_identity and not backend_identity_error:
                errors.append("Backend/export identity is required.")
            if definition is not None and not definition.shared:
                permitted = set(definition.node_states.filter(present=True).values_list("node", flat=True))
                if not node or node not in permitted:
                    errors.append("Choose the node-local storage instance this mount represents.")
            elif definition is not None:
                node = ""
            # Before comparing the identity against other registrations, compare it
            # against the kernel: a near-match check only knows what was typed
            # before, while the mount source says what this directory actually is.
            if not errors and backend_identity and not confirmed_mismatch:
                agreement = compare_mount_source(backend_identity, candidates[relative].get("source", ""))
                if not agreement.agrees:
                    confirm_backend_mismatch = True
                    confirmation_headline = "The chosen directory is not mounted from the datastore's own export."
                    confirmation_details.append(agreement.reason)
            if not errors and not confirmation_headline and backend_identity and not confirmed_distinct:
                near_matches = near_match_mounts(backend_identity)
                if near_matches:
                    confirm_distinct_backend = True
                    confirmation_headline = "Backend identity looks like an existing backend spelled differently."
                    for other in near_matches:
                        confirmation_details.append(
                            f"'{other.backend_identity}' ({other.display_name}) exports the same path under a "
                            "different host spelling. If that is the same physical backend, register it with the "
                            "identical identity — otherwise the cross-cluster in-use check cannot fire."
                        )
            if not errors and not confirmation_headline and definition is not None:
                profile = backend_profile(definition.storage_type)
                candidate = candidates[relative]
                existing = StorageMount.objects.filter(relative_path=relative).first()
                if existing and existing.backend_identity != backend_identity and existing.cluster_bindings.exists():
                    errors.append(
                        "This host path is registered with a different backend identity. "
                        "Remove its existing associations before remapping it."
                    )
                mount = existing or StorageMount(
                    storage_id=f"mount-{uuid.uuid4().hex[:12]}",
                    display_name=display_name,
                    path=f"/storages/{relative}",
                    relative_path=normalized_relative_path(relative),
                    trash_path=f"/storages/{default_trash_relative_path(relative)}",
                    trash_relative_path=default_trash_relative_path(relative),
                    filesystem_type=candidate["filesystem_type"],
                    backend_identity=backend_identity,
                    identity_source=identity_source,
                    enabled=True,
                )
                if not errors:
                    if existing:
                        mount.display_name = display_name
                        mount.backend_identity = backend_identity
                        mount.identity_source = identity_source
                        mount.filesystem_type = candidate["filesystem_type"]
                        mount.enabled = True
                    health = mount_health(mount, profile)
                    if not health.available:
                        errors.append(health.reason)
                    else:
                        mount.save()
                        try:
                            bind_storage_mount(cluster_storage=definition, mount=mount, node=node)
                        except StorageMountError as exc:
                            if not existing:
                                mount.delete()
                            errors.append(str(exc))
                        else:
                            record_audit_event(
                                request=request,
                                user=request.user,
                                username=request.user.get_username(),
                                action="storage.mount.registered",
                                object_type="storage_mount",
                                object_id=mount.mount_ref,
                                cluster=definition.cluster,
                                details={
                                    "cluster_key": definition.cluster.key,
                                    "storage_id": definition.storage_id,
                                    "mount_ref": mount.mount_ref,
                                    "scope": node or "shared",
                                    "display_name": mount.display_name,
                                    "path": mount.path,
                                },
                            )
                            # Nothing announces the success on this page — the new
                            # row in the table above and the Recent Tasks entry do.
                            # An emptied form is what says the submission is spent,
                            # and it is also what stops an accidental resubmit.
                            form_values = {}
    return render(
        request,
        "core/settings_storage.html",
        {
            **navigation_context("pve_settings"),
            "active_settings_tab": "storage",
            "definition_options": definition_options,
            "candidates": _mount_candidates(),
            "errors": errors,
            "warnings": warnings,
            "form_values": form_values,
            "confirm_distinct_backend": confirm_distinct_backend,
            "confirm_backend_mismatch": confirm_backend_mismatch,
            "confirmation_headline": confirmation_headline,
            "confirmation_details": confirmation_details,
            # A confirmation already given has to survive the next submit, or two
            # pending confirmations would each clear the other and never resolve.
            "confirmed_backend_mismatch": confirmed_mismatch,
            "confirmed_distinct_backend": confirmed_distinct,
            "bindings": _binding_rows(),
        },
    )

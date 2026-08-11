from __future__ import annotations

import ast
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from core.services.cluster_node_runtime import RUNTIME_FIELDS

NATIVE_DIALOG_PATTERN = re.compile(
    r"\bwindow\.(?:alert|confirm)\s*\("
    r"|(?:^|[;{}=:(,!&|?])\s*(?:alert|confirm)\s*\("
    r"|\b(?:return|await)\s+(?:alert|confirm)\s*\("
)
PROMPT_PATTERN = re.compile(r"\b(?:window\.)?prompt\s*\(")
CONSOLE_PROMPT_PATH = Path("static/js/app/console.js")


# Phase 1b removed the global client fan-out entirely: it selected clients from
# settings with no cluster scope, so a caller could reach another cluster's guest
# by VMID. Nothing may reintroduce it — provider access goes through
# core.services.cluster_resolver with an explicit cluster.
FORBIDDEN_GLOBAL_FAN_OUT = "configured_clients"

# Phase 4 deleted the implicit sole-enabled-cluster adapter. Keep the old symbol
# as a source-level tripwire so it cannot quietly return under another migration.
LEGACY_CLUSTER_SCOPE_ADAPTER_ALLOWLIST = frozenset()

LEGACY_ADAPTER_NAME = "require_sole_enabled_cluster_for_legacy_caller"

# R1a: retirement makes "which clusters does this query mean" a three-way decision
# (managed / provider-acquirable / historical), and a bare `ProxmoxCluster.objects`
# answers none of them -- it silently includes retired rows in a live selector or
# excludes them from a retention writer. Access must name a scope via
# core.services.cluster_scopes. Roughly thirty call sites predate that module and are
# migrated across R1b..R4; this allowlist freezes the set so no *new* bare caller
# appears, and a file migrated onto the scope resolvers must be struck from the list.
# The prose rule would otherwise decay exactly as confined_filesystem adoption did.
CLUSTER_SCOPE_MODULE = "core/services/cluster_scopes.py"
BARE_CLUSTER_OBJECTS_ALLOWLIST = frozenset(
    {
        # Installation booleans and passive reads; later phases replace the
        # remaining exists()/get()/first() here with named scope decisions.
        # (context_processors.py was fully converted in R1b and struck from here.)
        # Only the pk__in re-fetch in legacy_node_redirect: the scope decision was
        # already made by the cluster__enabled filters that produced those ids, so the
        # re-fetch orders rows rather than selecting them. Its unscoped sibling
        # cluster_from_path() was deleted in Review 11.
        "core/views/cluster_scope.py",
        "core/services/runtime_bootstrap.py",
        # The lifecycle lock reloads a specific cluster under select_for_update to
        # check retired_at *after* locking, so it must reach retired rows too — the
        # one acquisition primitive scopes cannot express.
        "core/services/cluster_lifecycle_lock.py",
        # Onboarding / activation / credential / trust write paths (Phase 5 surface).
        "core/services/cluster_activation.py",
        "core/services/cluster_credentials.py",
        "core/services/cluster_onboarding.py",
        "core/services/cluster_trust.py",
        # Management commands operate on a single operator-named key.
        "core/management/commands/add_cluster.py",
        "core/management/commands/approve_cluster_transport.py",
        "core/management/commands/enable_cluster.py",
        "core/management/commands/reapprove_cluster_identity.py",
        "core/management/commands/set_cluster_credential.py",
        "core/management/commands/set_initial_cluster_key.py",
        # Footprint stamping is not a cluster *selection*: it is a scope-agnostic,
        # set-based UPDATE keyed by primary key that must reach the row whatever its
        # scope (a disabled cluster still stamps), so a scope resolver cannot express
        # it. The IS NULL filter is what makes it monotonic, not a scope.
        "core/services/cluster_footprint.py",
        # Read/mutation/worker call sites migrated as R1b/R2 reach each module.
        # The cluster-qualified URL views were converted in Review 11 and struck
        # from here: every one of them now resolves its path cluster through
        # core.views.cluster_scope.managed_cluster_from_path.
        "core/services/audit_events.py",
        "core/services/current_guest_inventory.py",
        "core/services/durable_guest_operations.py",
        "core/services/file_actions.py",
        "core/services/storage_actions.py",
        "core/services/storage_catalog_refresh.py",
        "core/services/tag_actions.py",
        "core/services/tag_inventory_refresh.py",
        "core/tasks.py",
    }
)


class ClusterScopeSourceInvariantTests(SimpleTestCase):
    """Phase 1b: cluster selection must be explicit, and the legacy surface may
    only shrink. These invariants are what stop a half-migrated system from quietly
    growing new unqualified callers."""

    def _python_sources(self) -> list[Path]:
        root = Path(settings.BASE_DIR)
        return [
            path
            for path in sorted((root / "core").rglob("*.py"))
            if "migrations" not in path.parts and not path.name.startswith("tests")
        ]

    def _modules_containing(self, needle: str) -> set[str]:
        root = Path(settings.BASE_DIR)
        found = set()
        for path in self._python_sources():
            if needle in path.read_text():
                found.add(str(path.relative_to(root)))
        return found

    def test_the_global_client_fan_out_is_not_reintroduced(self):
        offenders = sorted(self._modules_containing(FORBIDDEN_GLOBAL_FAN_OUT))

        self.assertEqual(
            offenders,
            [],
            "Provider clients must be resolved from an explicit cluster via "
            "core.services.cluster_resolver. The global fan-out was removed in "
            f"Phase 1b and must not come back: {', '.join(offenders)}",
        )

    def test_legacy_scope_adapter_stays_on_its_allowlist(self):
        offenders = sorted(self._modules_containing(LEGACY_ADAPTER_NAME) - LEGACY_CLUSTER_SCOPE_ADAPTER_ALLOWLIST)

        self.assertEqual(
            offenders,
            [],
            f"{LEGACY_ADAPTER_NAME}() may only be called from allowlisted entry points "
            f"and is deleted before activation: {', '.join(offenders)}",
        )

    def test_cluster_derived_cache_calls_use_the_shared_namespace(self):
        known_cluster_cache_modules = {
            "core/services/datastore_nav.py",
            "core/services/proxmox.py",
            "core/services/tag_registry.py",
            "core/views/guests/read_model_support.py",
        }
        bare_cluster_key = re.compile(r"pve-helper:(?:live-guest|guest-|tag-registry|nav-datastores)")
        offenders = []
        for path in self._python_sources():
            source = path.read_text()
            relative = str(path.relative_to(settings.BASE_DIR))
            if relative in known_cluster_cache_modules and "cluster_cache_key(" not in source:
                offenders.append(relative)
            if bare_cluster_key.search(source):
                offenders.append(relative)

        self.assertEqual(
            offenders,
            [],
            "Cluster-derived cache state must use cluster_cache_key(); bare guest/node "
            f"keys can collide across clusters: {', '.join(offenders)}",
        )

    def test_cluster_operation_locks_use_cluster_identity(self):
        root = Path(settings.BASE_DIR)
        lifecycle_lock_allowlist = {
            "core/services/cluster_activation.py",
            "core/services/runtime_bootstrap.py",
            "core/services/cluster_credentials.py",
            "core/services/cluster_trust.py",
        }
        advisory_call = re.compile(r"pg_(?:try_)?advisory_(?:xact_)?lock")
        offenders = []
        for path in self._python_sources():
            relative = str(path.relative_to(root))
            source = path.read_text()
            if (
                advisory_call.search(source)
                and relative not in lifecycle_lock_allowlist
                and "cluster_advisory_lock_id(" not in source
            ):
                offenders.append(relative)
        self.assertEqual(
            offenders,
            [],
            f"Cluster operations may not use one global overlap/advisory lock: {', '.join(offenders)}",
        )

    def test_bare_cluster_objects_stays_on_the_scope_module_and_allowlist(self):
        offenders = self._modules_containing("ProxmoxCluster.objects")
        offenders.discard(CLUSTER_SCOPE_MODULE)

        unexpected = sorted(offenders - BARE_CLUSTER_OBJECTS_ALLOWLIST)
        self.assertEqual(
            unexpected,
            [],
            "A new bare ProxmoxCluster.objects appeared. Retirement made cluster "
            "selection a three-way scope decision; resolve through "
            "core.services.cluster_scopes (managed / provider_acquirable / historical) "
            f"instead: {', '.join(unexpected)}",
        )

        stale = sorted(BARE_CLUSTER_OBJECTS_ALLOWLIST - offenders)
        self.assertEqual(
            stale,
            [],
            "These files were migrated off bare ProxmoxCluster.objects; strike them from "
            f"BARE_CLUSTER_OBJECTS_ALLOWLIST so the ratchet only tightens: {', '.join(stale)}",
        )

    def test_production_proxmox_clients_are_built_only_by_scoped_factories(self):
        allowed = {
            "core/services/cluster_resolver.py",
            "core/services/cluster_onboarding.py",
        }
        offenders = sorted(self._modules_containing("ProxmoxClient(") - allowed)
        self.assertEqual(
            offenders,
            [],
            "Production provider clients must carry an explicit cluster credential "
            "and trust profile; construct them only in the scoped factories: "
            f"{', '.join(offenders)}",
        )


# 5a0A / node enrollment: "which nodes exist" is about to stop being the same
# question as "which nodes may pve-helper publish". Every raw membership read is
# therefore a future consumer of the enrollment filter, and the plan's prose
# inventory of them was already incomplete when it was written -- it named the
# node_names() callers and missed the guest HA card's direct cluster/status read.
# Freeze the set so the next one cannot be added silently. A module that migrates
# onto the filtered read service is struck from the list.
RAW_MEMBERSHIP_READ_NAMES = ("node_names(", '"cluster/status"')
MEMBERSHIP_READ_OWNER = "core/services/proxmox.py"
MEMBERSHIP_PROJECTION_OWNER = "core/services/cluster_membership.py"
RAW_MEMBERSHIP_READ_ALLOWLIST = frozenset(
    {
        # Legacy node-list primitive. N4 migrates publication consumers away.
        MEMBERSHIP_READ_OWNER,
        # Canonical completeness-carrying membership publisher (5a1B).
        MEMBERSHIP_PROJECTION_OWNER,
        # Onboarding must read membership before any enrollment exists, by
        # definition: it is proving what the candidate transport is attached to.
        # This one stays raw after the filter lands.
        "core/services/cluster_onboarding.py",
        # Scan pass-2 gap fill. Becomes the *safety* read set (managed +
        # safety_only), not the published one -- it stays a raw caller on purpose.
        "core/tasks.py",
        # Node target lists offered to an operator. All four must move onto the
        # filtered read in N4: a hidden node must never appear as a placement,
        # migration, clone or replication target.
        "core/services/guest_create.py",
        "core/services/scheduled_actions.py",
        "core/views/guests/_core.py",
        "core/views/guests/replication.py",
        # Known debt, found by this invariant rather than by review: the guest
        # Summary HA card reads cluster/status live from a request-rendering path
        # and derives its node count from raw provider truth. That already breaks
        # the Module 5 rule that no rendering path performs a broad provider read,
        # and after enrollment it would report three nodes while the workspace
        # shows two. Owned by 5d1 (HA cluster-manager read); struck from here when
        # it moves onto a projection.
        "core/views/guests/read_model_support.py",
    }
)


# 5a0A: the narrow rule Module 5 actually needs from the `nodes/` axis is not
# "freeze every nodes/ path" -- that would allowlist ~40 legitimate guest- and
# UPID-scoped call sites and enforce nothing. It is: **a view may address a guest
# through its node, but may not read node state.** Node state belongs to the 5a1
# projection, and a view that reads it live is a request-path fan-out that scales
# with cluster size.
#
# The ledger first deferred this ratchet to 5a1F on the grounds that it could not
# be written before the projection existed. Re-review falsified that: the rule is
# writable now, the known offenders are few, and deferring it leaves the door open
# for every phase in between. 5a1F strikes entries; it does not author the rule.
#
# Guest-scoped means a vmid follows: `nodes/<node>/qemu/{vmid}/...`. A *vmid-less*
# `nodes/<node>/qemu` is a per-node listing and is deliberately caught.
NODE_SCOPED_VIEW_READ = re.compile(
    r"nodes/\{[^}]*\}/"  # nodes/<node>/
    r"(?!(?:qemu|lxc)/\{)"  # ...not addressing one guest by vmid
    r"[a-z][a-z0-9_-]*"  # ...a literal next segment: status, network, storage, qemu
)
NODE_SCOPED_VIEW_READ_ALLOWLIST = frozenset(
    {
        # The migrate dialog's CPU/bridge helpers (`capabilities/qemu/cpu`,
        # `status`, `network`) plus the vzdump write. The three read helpers are
        # the uncached 3N + 2 fan-out recorded in the ledger; 5a4B/5a4E replace
        # them with projection reads and strike this entry.
        "core/views/guests/_core.py",
        # Volume delete addresses a storage on a node. A write owned by the
        # storage domain, not node state.
        "core/views/guests/mutations.py",
    }
)


class NodeScopedViewReadInvariantTests(SimpleTestCase):
    """A view addresses guests through a node; it does not read the node itself.

    **What this does not catch**, established by probing rather than assumed — the
    sibling membership ratchet declares its blind spots and so must this one, or the
    ledger citing it overstates what is enforced:

    * a literal node name, ``"nodes/pve1/status"``;
    * concatenation, ``"nodes/" + node + "/status"``, including the
      ``base = f"nodes/{node}"`` then ``base + "/network"`` shape that already
      appears in this codebase (``views/guests/operation_lifecycle.py``);
    * ``%``-formatting, ``"nodes/%s/status" % node``;
    * a variable second segment, ``f"nodes/{node}/{segment}"``;
    * request-path reads that live in a **service** rather than a view --
      ``services/guest_create.py`` issues a per-create-form-render ``network`` read
      that this rule is morally aimed at and structurally does not reach;
    * **fan-out itself.** This flags path-string authorship. ``views/guests/dialogs.py``
      contains the O(N) loop but no node path, so relocating that loop to a new view
      module trips nothing.

    It also false-positives on a literal vmid (``f"nodes/{node}/qemu/100/config"``),
    which is the safe direction: a spurious failure is argued, a missed one is not.

    So this is a ratchet against the naive reintroduction -- the common case, and the
    one that actually recurs -- not a proof of absence. The behavioral rule ("no
    request-rendering path performs a broad provider read") still needs 5a1F, which
    can assert it against the projection's consumers instead of against strings.
    """

    def _view_sources(self) -> list[Path]:
        root = Path(settings.BASE_DIR)
        return [path for path in sorted((root / "core" / "views").rglob("*.py")) if not path.name.startswith("tests")]

    def _offending_modules(self) -> set[str]:
        root = Path(settings.BASE_DIR)
        return {
            str(path.relative_to(root))
            for path in self._view_sources()
            if NODE_SCOPED_VIEW_READ.search(path.read_text())
        }

    def test_views_do_not_read_node_state_directly(self):
        offenders = sorted(self._offending_modules() - NODE_SCOPED_VIEW_READ_ALLOWLIST)

        self.assertEqual(
            offenders,
            [],
            "A view may address a guest through its node but must not read node "
            "state live -- that is a request-path fan-out that grows with cluster "
            "size. Consume the node projection, or add the module to "
            f"NODE_SCOPED_VIEW_READ_ALLOWLIST with the phase that removes it: {', '.join(offenders)}",
        )

    def test_the_allowlist_does_not_outlive_its_call_sites(self):
        stale = sorted(NODE_SCOPED_VIEW_READ_ALLOWLIST - self._offending_modules())

        self.assertEqual(
            stale,
            [],
            "These view modules no longer read node state, so their allowlist "
            f"entries would silently re-permit a future one. Strike them: {', '.join(stale)}",
        )

    def test_guest_scoped_paths_are_not_caught(self):
        # The ratchet is only useful if it ignores Module 3's ordinary addressing.
        self.assertIsNone(NODE_SCOPED_VIEW_READ.search('f"nodes/{node}/qemu/{vmid}/status/current"'))
        self.assertIsNone(NODE_SCOPED_VIEW_READ.search('f"nodes/{node}/lxc/{vmid}/config"'))
        # ...and only useful if it does catch node state and vmid-less listings.
        self.assertIsNotNone(NODE_SCOPED_VIEW_READ.search('f"nodes/{node}/status"'))
        self.assertIsNotNone(NODE_SCOPED_VIEW_READ.search('f"nodes/{node}/qemu"'))


class MembershipReadInvariantTests(SimpleTestCase):
    """Raw cluster-membership reads are a closed, named set.

    The node-enrollment plan turns membership into a two-part question -- provider
    coverage versus publication scope -- and every caller below has to pick a side.
    The risk is not the callers that exist; it is the one added next quarter that
    quietly reintroduces "the provider returned it, so show it".
    """

    def _python_sources(self) -> list[Path]:
        root = Path(settings.BASE_DIR)
        return [
            path
            for path in sorted((root / "core").rglob("*.py"))
            if "migrations" not in path.parts and not path.name.startswith("tests")
        ]

    def _modules_reading_membership(self) -> set[str]:
        root = Path(settings.BASE_DIR)
        found = set()
        for path in self._python_sources():
            text = path.read_text()
            if any(needle in text for needle in RAW_MEMBERSHIP_READ_NAMES):
                found.add(str(path.relative_to(root)))
        return found

    def test_raw_membership_reads_stay_on_their_allowlist(self):
        offenders = sorted(self._modules_reading_membership() - RAW_MEMBERSHIP_READ_ALLOWLIST)

        self.assertEqual(
            offenders,
            [],
            "A raw cluster-membership read (node_names() or cluster/status) may only "
            "appear in modules that have declared how they treat an unpublished node. "
            "Add the module to RAW_MEMBERSHIP_READ_ALLOWLIST with that reason, or "
            f"consume the filtered read service instead: {', '.join(offenders)}",
        )

    def test_the_allowlist_does_not_outlive_its_call_sites(self):
        stale = sorted(RAW_MEMBERSHIP_READ_ALLOWLIST - self._modules_reading_membership())

        self.assertEqual(
            stale,
            [],
            "These modules no longer read raw membership, so their allowlist entries "
            "are stale and would silently re-permit a future one. Strike them: "
            f"{', '.join(stale)}",
        )

    def test_legacy_node_names_helper_has_one_definition(self):
        root = Path(settings.BASE_DIR)
        owner = root / MEMBERSHIP_READ_OWNER
        definitions = [
            node.name
            for node in ast.walk(ast.parse(owner.read_text()))
            if isinstance(node, ast.FunctionDef) and node.name == "node_names"
        ]

        self.assertEqual(
            definitions,
            ["node_names"],
            "Legacy node_names() must remain one compatibility helper on "
            f"{MEMBERSHIP_READ_OWNER}. Canonical completeness-carrying membership "
            f"publication belongs to {MEMBERSHIP_PROJECTION_OWNER}.",
        )

    def test_cluster_status_projection_has_one_canonical_owner(self):
        root = Path(settings.BASE_DIR)
        owner_source = (root / MEMBERSHIP_PROJECTION_OWNER).read_text()
        owners = {
            str(path.relative_to(root))
            for path in self._python_sources()
            if 'client.get("cluster/status")' in path.read_text()
        }
        self.assertIn(MEMBERSHIP_PROJECTION_OWNER, owners)
        self.assertEqual(
            owner_source.count('client.get("cluster/status")'),
            1,
            "The canonical membership publisher must issue cluster/status through one explicit call site.",
        )
        projection_writers = {
            relative for relative in owners if "ClusterMembershipState" in (root / relative).read_text()
        }
        self.assertEqual(
            projection_writers,
            {MEMBERSHIP_PROJECTION_OWNER},
            "Raw onboarding/presentation readers may not become competing membership projection writers.",
        )


class FrontendSourceInvariantTests(SimpleTestCase):
    def _frontend_sources(self) -> list[Path]:
        root = Path(settings.BASE_DIR)
        return sorted((root / "static/js/app").glob("*.js")) + sorted((root / "templates/core").rglob("*.html"))

    def test_native_alert_and_confirm_are_not_used(self):
        root = Path(settings.BASE_DIR)
        violations = []
        for path in self._frontend_sources():
            for line_number, line in enumerate(path.read_text().splitlines(), start=1):
                if NATIVE_DIALOG_PATTERN.search(line):
                    violations.append(f"{path.relative_to(root)}:{line_number}")

        self.assertEqual(
            violations,
            [],
            "Use the shared application dialog and local feedback instead of "
            f"native alert()/confirm(): {', '.join(violations)}",
        )

    def test_prompt_is_reserved_for_console_paste_safeguard(self):
        root = Path(settings.BASE_DIR)
        prompt_locations = []
        for path in self._frontend_sources():
            for line_number, line in enumerate(path.read_text().splitlines(), start=1):
                if PROMPT_PATTERN.search(line):
                    prompt_locations.append((path.relative_to(root), line_number))

        self.assertEqual(len(prompt_locations), 1)
        self.assertEqual(
            prompt_locations[0][0],
            CONSOLE_PROMPT_PATH,
            "window.prompt() is reserved for the console paste safeguard; use the shared application dialog elsewhere.",
        )

    def test_templates_do_not_reintroduce_inline_scripts(self):
        root = Path(settings.BASE_DIR)
        inline_script = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.IGNORECASE)
        violations = []
        for path in sorted((root / "templates").rglob("*.html")):
            for line_number, line in enumerate(path.read_text().splitlines(), start=1):
                if inline_script.search(line):
                    violations.append(f"{path.relative_to(root)}:{line_number}")

        self.assertEqual(
            violations,
            [],
            "Inline scripts weaken the enforced Content Security Policy; add same-origin static JavaScript instead: "
            f"{', '.join(violations)}",
        )

    def test_only_the_base_template_loads_scripts(self):
        """A `<script>` in a content block is dead code after the first soft
        navigation.

        `replacePageFromDocument` swaps the content block with `innerHTML`, and
        `innerHTML` never executes a script — so a page-local script runs exactly
        once, on a full load, and is silently absent afterwards. The datastore
        Refresh button was wired that way: after any file action its form had no
        handler, the shell's submit handler POSTed it as a navigation, got JSON
        back, and fell through to a full page reload. Feature code belongs in a
        module initialised from `bootstrap.js`, which reruns on every navigation.
        """
        root = Path(settings.BASE_DIR)
        script_tag = re.compile(r"<script\b", re.IGNORECASE)
        violations = []
        for path in sorted((root / "templates").rglob("*.html")):
            relative_path = path.relative_to(root)
            if relative_path == Path("templates/base.html"):
                continue
            for line_number, line in enumerate(path.read_text().splitlines(), start=1):
                if script_tag.search(line):
                    violations.append(f"{relative_path}:{line_number}")

        self.assertEqual(
            violations,
            [],
            "Only templates/base.html may load JavaScript; a page-local <script> does not "
            f"survive soft navigation. Initialise a module from bootstrap.js instead: {', '.join(violations)}",
        )

    def test_template_comments_do_not_span_lines(self):
        """`{# … #}` is a single-line comment, and a multi-line one is not a comment
        at all.

        Django's tokeniser compiles `({%.*?%}|{{.*?}}|{#.*?#})` without `DOTALL`,
        so `.` stops at a newline and a `{#` that closes on a later line never
        matches. The whole thing stays plain text and is served to the browser
        verbatim. Two of them shipped that way — a note about where the browser
        title is composed appeared inside `<head>` on every page in the
        application, and a note about the catalog-refresh marker appeared above
        the datastore view. Nothing failed, which is why they survived: a comment
        that leaks looks exactly like a comment that works, unless someone reads
        the delivered HTML. Use `{% comment %}` for anything longer than a line.
        """
        root = Path(settings.BASE_DIR)
        violations = []
        for path in sorted((root / "templates").rglob("*.html")):
            text = path.read_text()
            for match in re.finditer(r"\{#", text):
                end = text.find("#}", match.end())
                if end == -1:
                    continue
                if "\n" in text[match.start() : end]:
                    violations.append(f"{path.relative_to(root)}:{text[: match.start()].count('\n') + 1}")

        self.assertEqual(
            violations,
            [],
            "A `{# #}` comment that closes on a later line is rendered into the page as text; "
            f"use `{{% comment %}}` instead: {', '.join(violations)}",
        )

    def test_every_css_partial_is_linked_by_the_base_template(self):
        """Every CSS partial is loaded globally, and the list has to stay honest.

        Page-scoping a stylesheet is not available here: soft navigation swaps the
        content, tree and status blocks with `innerHTML` and never touches `<head>`,
        so a `<link>` emitted by a per-page block would survive the full page load
        that put it there and vanish on the first navigation away and back. The page
        would then render unstyled with nothing raising — the same failure as a
        page-local `<script>`, minus the noise. So `base.html` owns all of them, and
        the only thing that can go wrong is drift: a new partial nobody linked (dead
        file, styles that never apply) or a link to a partial that was deleted (a 404
        on every page). Both directions are checked, because either one alone leaves
        the list quietly wrong.
        """
        root = Path(settings.BASE_DIR)
        partial_dir = root / "static" / "css" / "app"
        on_disk = {path.name for path in partial_dir.glob("*.css")}
        linked = set(re.findall(r"css/app/([\w.-]+\.css)", (root / "templates" / "base.html").read_text()))

        self.assertEqual(
            sorted(on_disk - linked),
            [],
            "A CSS partial exists but no `<link>` in base.html loads it, so its styles never apply; "
            "add it to the list in cascade order or delete the file.",
        )
        self.assertEqual(
            sorted(linked - on_disk),
            [],
            "base.html links a CSS partial that does not exist, which is a 404 on every page.",
        )


class DialogModuleInvariantTests(SimpleTestCase):
    """A modal element belongs to one modal.

    Reusing a single ``<dialog>`` for every confirmation is the obvious economy
    and it breaks chained confirmations without a trace. ``dialog.close()`` fires
    ``close`` from a queued task rather than synchronously, so the dialog opened
    in the awaited continuation of the previous one is already showing when the
    previous dialog's close event arrives; on a shared element that event reaches
    the *new* dialog's handler, which reads it as a dismissal. The risk question
    after Rename and the second question before a permanent delete both resolved
    themselves that way — no dialog, no request, no error, a button that did
    nothing.

    That is not a bug a reader recognises in a diff, which is why it is asserted
    here rather than only explained in a comment.
    """

    DIALOG_MODULE = Path("static/js/app/dialogs.js")

    def _dialog_source(self) -> str:
        return (Path(settings.BASE_DIR) / self.DIALOG_MODULE).read_text()

    def test_each_modal_gets_its_own_element(self):
        source = self._dialog_source()
        self.assertIn(
            'document.createElement("dialog")',
            source,
            "Modals must build their own element.",
        )
        self.assertNotIn(
            'document.querySelector("[data-vm-action-dialog]")',
            source,
            "Looking up an existing modal element means sharing one between dialogs, "
            "which silently turns a chained confirmation into a dismissal.",
        )

    def test_a_closed_modal_leaves_the_document(self):
        self.assertIn(
            'dialog.addEventListener("close", () => dialog.remove())',
            self._dialog_source(),
            "A closed modal must detach, so a queued close event cannot reach a later dialog.",
        )

    def test_no_module_holds_on_to_a_modal_element_between_dialogs(self):
        root = Path(settings.BASE_DIR)
        violations = []
        for path in sorted((root / "static/js/app").glob("*.js")):
            for line_number, line in enumerate(path.read_text().splitlines(), start=1):
                if "[data-vm-action-dialog]" in line and "dataset.vmActionDialog" not in line:
                    violations.append(f"{path.relative_to(root)}:{line_number}")

        self.assertEqual(
            violations,
            [],
            "The modal element is per-dialog and transient; reaching for it by selector "
            f"assumes a shared one: {', '.join(violations)}",
        )


class DjangoAdminSurfaceInvariantTests(SimpleTestCase):
    """Django admin bypasses every validated service the app writes through. It is a
    dev/E2E convenience, and audit must be append-only wherever it is mounted."""

    def test_audit_events_cannot_be_written_or_deleted_through_admin(self):
        from django.contrib import admin as django_admin

        from core.models import AuditEvent

        model_admin = django_admin.site._registry[AuditEvent]

        self.assertFalse(model_admin.has_add_permission(None))
        self.assertFalse(model_admin.has_change_permission(None))
        self.assertFalse(model_admin.has_delete_permission(None))

    def test_admin_is_routed_only_where_it_is_deliberately_enabled(self):
        source = (Path(settings.BASE_DIR) / "pve_helper/urls.py").read_text()
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        mounts = [line for line in code.splitlines() if "admin.site.urls" in line]

        self.assertEqual(len(mounts), 1, f"Expected exactly one admin mount, found: {mounts}")
        self.assertLess(
            code.index("if settings.DJANGO_ADMIN_ENABLED:"),
            code.index("admin.site.urls"),
            "Django admin must stay behind DJANGO_ADMIN_ENABLED. Mounting it where login "
            "is enforced restores a browser-reachable write path over endpoints, mounts, "
            "schedules and audit that bypasses the app's validated services.",
        )


class ConfinedFilesystemAdoptionInvariantTests(SimpleTestCase):
    """`core.services.confined_filesystem` is only worth having if it is the only
    way these modules touch mounted storage.

    The module was written, adopted at one call site, and then stopped being
    adopted while `AGENTS.md` went on stating the invariant as prose. That is the
    failure this class exists to prevent: not an unsafe call someone argued for,
    but an unsafe call nobody noticed, because the rule lived in a document rather
    than in the suite.
    """

    # Modules that write to, or delete from, operator-visible mounted storage.
    # The storage view package is covered by directory rather than by filename:
    # it was one 3908-line module until Review 11 split it, and naming its members
    # here would mean a new domain module silently leaving the scan.
    STORAGE_WRITE_SERVICES = (
        "core/services/storage_actions.py",
        "core/services/vm_register.py",
        "core/services/ovf_import.py",
    )
    STORAGE_WRITE_VIEW_PACKAGE = "core/views/storage"

    # Each of these resolves a name a second time, at the moment it acts on it.
    # Between the containment check and the call, Proxmox or another storage
    # client can change what the name refers to.
    RESOLVE_THEN_ACT_CALLS = re.compile(
        r"shutil\.(?:rmtree|copy2|copyfile|copytree|move)\("
        r"|os\.(?:chown|chmod|link|symlink|rename|replace|unlink|remove|mkdir|makedirs|rmdir)\("
        # `replace` is deliberately absent from the bound-method alternation:
        # `str.replace` is unrelated and far more common. `os.replace` above
        # already covers the filesystem call this is about.
        r"|\.(?:rename|unlink|rmdir|mkdir|touch|write_bytes|write_text)\("
    )

    # `Path.resolve()` is legitimate for establishing the trusted root itself,
    # which is configuration rather than request input, and for turning a
    # configured absolute path back into a relative one. It is never legitimate
    # for producing something to write to.
    TRUSTED_ROOT_RESOLVERS = {
        "core/services/storage_actions.py": {"_storage_root", "_trash_root_relative"},
    }

    def _storage_write_modules(self) -> list[str]:
        root = Path(settings.BASE_DIR)
        package = sorted(str(path.relative_to(root)) for path in (root / self.STORAGE_WRITE_VIEW_PACKAGE).glob("*.py"))
        self.assertTrue(package, "The storage view package was not found; the scan covers nothing.")
        return [*self.STORAGE_WRITE_SERVICES, *package]

    def _code_lines(self, relative_path: str) -> list[tuple[int, str]]:
        source = (Path(settings.BASE_DIR) / relative_path).read_text()
        return [
            (number, line)
            for number, line in enumerate(source.splitlines(), start=1)
            if not line.lstrip().startswith("#")
        ]

    def test_storage_writes_do_not_resolve_a_path_and_then_act_on_it(self):
        offenders: list[str] = []
        for relative_path in self._storage_write_modules():
            for number, line in self._code_lines(relative_path):
                if self.RESOLVE_THEN_ACT_CALLS.search(line):
                    offenders.append(f"{relative_path}:{number}: {line.strip()}")

        self.assertEqual(
            offenders,
            [],
            "Mounted-storage mutation must go through core.services.confined_filesystem, "
            "which walks every untrusted component by directory descriptor with O_NOFOLLOW "
            "and mutates with no-replace semantics. A path-based call re-resolves the name "
            "at the moment it acts, so a component swapped in between aims the write "
            "somewhere else. Offenders:\n" + "\n".join(offenders),
        )

    def test_path_resolution_in_storage_writes_is_confined_to_root_discovery(self):
        offenders: list[str] = []
        for relative_path in self._storage_write_modules():
            allowed = self.TRUSTED_ROOT_RESOLVERS.get(relative_path, set())
            current_function = ""
            for number, line in self._code_lines(relative_path):
                match = re.match(r"\s*def\s+(\w+)", line)
                if match:
                    current_function = match.group(1)
                if ".resolve(" in line and current_function not in allowed:
                    offenders.append(f"{relative_path}:{number} in {current_function}(): {line.strip()}")

        self.assertEqual(
            offenders,
            [],
            "Path.resolve() may establish the trusted root, which comes from configuration, "
            "but must not produce a path that is then written to. Offenders:\n" + "\n".join(offenders),
        )

    def test_the_path_component_validator_stays_one_import_away(self):
        """The module split exists for a reason no reader can see from the code.

        `confined_path_component` guards five syscalls that CodeQL reports as
        py/path-injection. The exception is a barrier in the model pack, and a
        barrier there resolves an API-graph node — which matches a call reaching
        a helper through an *import*, never a validator called inside its own
        module. Tidying the function back into confined_filesystem.py silently
        reintroduces five findings locally and in GitHub code scanning, and
        nothing in the diff would say so.
        """
        root = Path(settings.BASE_DIR)
        names_module = root / "core/services/confined_names.py"
        confined = (root / "core/services/confined_filesystem.py").read_text()
        model = (root / ".github/codeql/extensions/pve-helper-storage-python/models/storage.model.yml").read_text()

        self.assertTrue(
            names_module.is_file(),
            "core/services/confined_names.py must exist; it is the import boundary the "
            "CodeQL barrier resolves against.",
        )
        self.assertIn(
            "from core.services.confined_names import",
            confined,
            "confined_filesystem must import the validator rather than define it.",
        )
        self.assertNotIn(
            "def confined_path_component",
            confined,
            "confined_path_component must not be defined in confined_filesystem: a call to "
            "a validator inside its own module is not an API-graph node, so the CodeQL "
            "barrier stops applying and the five syscalls report py/path-injection again.",
        )
        self.assertIn(
            "Member[confined_names].Member[confined_path_component].ReturnValue",
            model,
            "The CodeQL model must name the validator, or the barrier does not exist.",
        )

    def test_the_parallel_path_safety_helper_is_not_reintroduced(self):
        root = Path(settings.BASE_DIR)
        offenders = [
            str(path.relative_to(root))
            for path in sorted((root / "core").rglob("*.py"))
            if path.name != "tests_source_invariants.py" and "_storage_child_path" in path.read_text()
        ]

        self.assertEqual(
            offenders,
            [],
            "_storage_child_path was a second, weaker copy of the confined boundary living "
            "inside a service - exactly what AGENTS.md forbids. Reuse confined_filesystem "
            "instead of reintroducing it. Found in: " + ", ".join(offenders),
        )


class ScanEntryClassifierInvariantTests(SimpleTestCase):
    """One classification implementation, because two of them already drifted.

    The full scan overruled `classify_entry` with the API storage catalog for disk
    images; the partial directory refresh did not. A catalog-referenced disk
    therefore came back `classification-blocked` after every rename, move, trash or
    restore until the next full scan undid it. Both paths now go through
    `ScanEntryClassifier`, and nothing else may assemble its own verdict.
    """

    OWNER = Path("core/services/entry_classification.py")
    # Where a scan writes FileInventory rows. These must not classify by hand.
    SCAN_PATHS = (Path("core/tasks.py"), Path("core/services/partial_scan.py"))

    def test_only_the_shared_classifier_composes_a_verdict(self):
        root = Path(settings.BASE_DIR)
        offenders = []
        for path in sorted((root / "core").rglob("*.py")):
            relative_path = path.relative_to(root)
            if (
                relative_path == self.OWNER
                or "migrations" in relative_path.parts
                or path.name.startswith("tests")
                # The two modules that define the pieces; the rule is about who
                # calls them.
                or relative_path in {Path("core/services/storage_catalog.py"), Path("core/services/classification.py")}
            ):
                continue
            source = path.read_text()
            for name in ("classify_entry", "MountedVolumeClassifier", "classify_mounted_volume"):
                if name in source:
                    offenders.append(f"{relative_path}:{name}")

        self.assertEqual(
            offenders,
            [],
            "A scanned entry is classified by core.services.entry_classification."
            "ScanEntryClassifier and nowhere else; calling the legacy or catalog "
            f"classifier directly is how the two scan paths drifted apart: {', '.join(offenders)}",
        )

    def test_every_scan_path_uses_it(self):
        root = Path(settings.BASE_DIR)
        for relative_path in self.SCAN_PATHS:
            source = (root / relative_path).read_text()
            self.assertIn(
                "ScanEntryClassifier",
                source,
                f"{relative_path} writes FileInventory rows and must classify through the shared classifier.",
            )

    def test_the_catalog_still_overrules_the_legacy_verdict_for_disk_images(self):
        """The point of the shared step. If this list is emptied, both scan paths
        silently fall back to volid matching against one scan's inventory rows."""
        from core.services.entry_classification import CATALOG_AUTHORITATIVE_CATEGORIES

        self.assertEqual(set(CATALOG_AUTHORITATIVE_CATEGORIES), {"vm_disk", "base_image"})


class PublicErrorBoundaryInvariantTests(SimpleTestCase):
    """Review 10: provider and Python exception strings belong in protected logs,
    never in a durable failure payload or a rendered task row.

    Two rules, because one alone is not enough. Sanitising the writers is
    pointless if a domain exception laundered the provider's text into its own
    message first; marking classes public is pointless if a writer bypasses them
    with `str(exc)`. The pair is what makes `PublicMessageError` mean something.
    """

    #: An exception object reaching a stored/rendered failure field.
    _EXCEPTION_TEXT = re.compile(r"str\(\s*exc\b|\{\s*exc\b|\{\s*type\(exc\)|\{\s*exc\.__class__")
    #: The shapes a durable failure payload is written in.
    _DURABLE_FIELD = re.compile(r"""["']error(?:_details|_code)?["']\s*[:\]]\s*=?|\berror(?:_details)?\s*=(?!=)""")
    #: `public_errors` itself documents these shapes; the boundary lives there.
    _ALLOWED = frozenset({Path("core/services/public_errors.py")})

    def _production_sources(self) -> list[tuple[Path, Path]]:
        root = Path(settings.BASE_DIR)
        found = []
        for package in ("core", "console_app", "pve_helper"):
            for path in sorted((root / package).rglob("*.py")):
                relative_path = path.relative_to(root)
                if "migrations" in relative_path.parts or path.name.startswith("tests"):
                    continue
                found.append((path, relative_path))
        return found

    def test_no_exception_string_reaches_a_durable_error_field(self):
        violations = []
        for path, relative_path in self._production_sources():
            if relative_path in self._ALLOWED:
                continue
            for line_number, line in enumerate(path.read_text().splitlines(), start=1):
                if self._EXCEPTION_TEXT.search(line) and self._DURABLE_FIELD.search(line):
                    violations.append(f"{relative_path}:{line_number}")

        self.assertEqual(
            violations,
            [],
            "Stored and rendered failure fields must carry caller-owned text from "
            "core.services.public_errors, not an exception string: " + ", ".join(violations),
        )

    def test_public_message_errors_never_interpolate_another_exception(self):
        """A `PublicMessageError` subclass claims that its own `str()` is safe.

        One raise site that interpolates `{exc}` makes that claim false for every
        message the class carries, because the marker classifies the type and not
        the string.
        """
        root = Path(settings.BASE_DIR)
        public_classes = self._public_error_class_names(root)
        self.assertIn("StorageActionError", public_classes, "The marker scan found no classes; it is not working.")

        violations = []
        for path, relative_path in self._production_sources():
            for line_number, line in enumerate(path.read_text().splitlines(), start=1):
                stripped = line.strip()
                if not stripped.startswith("raise "):
                    continue
                raised = stripped[len("raise ") :].split("(", 1)[0].strip()
                if raised in public_classes and self._EXCEPTION_TEXT.search(line):
                    violations.append(f"{relative_path}:{line_number}")

        self.assertEqual(
            violations,
            [],
            "A PublicMessageError's message must be composed by its raise site, never "
            "from another exception (use `raise ... from exc` and let the log carry it): " + ", ".join(violations),
        )

    def _public_error_class_names(self, root: Path) -> set[str]:
        """Every class that inherits the marker, directly or through a base."""
        names = {"PublicMessageError"}
        definitions: list[tuple[str, list[str]]] = []
        for path, _relative_path in self._production_sources():
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    bases = [base.id for base in node.bases if isinstance(base, ast.Name)]
                    definitions.append((node.name, bases))
        # Iterate to a fixed point so a subclass of a subclass is included too.
        changed = True
        while changed:
            changed = False
            for name, bases in definitions:
                if name not in names and any(base in names for base in bases):
                    names.add(name)
                    changed = True
        return names


class BackendSourceInvariantTests(SimpleTestCase):
    def test_production_audit_writes_use_the_shared_service(self):
        root = Path(settings.BASE_DIR)
        allowed_path = Path("core/services/audit_events.py")
        violations = []
        for package in ("core", "console_app", "pve_helper"):
            for path in sorted((root / package).rglob("*.py")):
                relative_path = path.relative_to(root)
                if (
                    relative_path == allowed_path
                    or "migrations" in relative_path.parts
                    or path.name.startswith("tests")
                ):
                    continue
                for line_number, line in enumerate(path.read_text().splitlines(), start=1):
                    if "AuditEvent.objects.create(" in line:
                        violations.append(f"{relative_path}:{line_number}")

        self.assertEqual(
            violations,
            [],
            "Production Audit events must use core.services.audit_events."
            f"record_audit_event(): {', '.join(violations)}",
        )

    def test_nginx_accepts_forwarded_scheme_only_from_the_trusted_peer(self):
        root = Path(settings.BASE_DIR)
        template = (root / "docker/nginx/templates/default.conf.template").read_text()

        self.assertIn("geo $realip_remote_addr $pve_helper_is_trusted_proxy_peer", template)
        self.assertIn(
            'map "$pve_helper_is_trusted_proxy_peer:$http_x_forwarded_proto"',
            template,
        )
        self.assertIn("default $scheme;", template)
        self.assertIn('"1:https" https;', template)

    def test_every_routed_view_is_login_wrapped(self):
        """Auth is per-view (`app_login_required`), so a forgotten decorator on any
        routed view silently exposes it unauthenticated. Enforce coverage at the
        source level: every `views.<name>` routed in core/urls.py whose function is
        defined in the view packages must carry `app_login_required`/`login_required`
        in its decorator block. Only the health probes are intentionally public."""
        root = Path(settings.BASE_DIR)
        public_allowlist = {"health_live", "health_ready"}

        urls_source = (root / "core/urls.py").read_text()
        routed = set(re.findall(r"views\.([A-Za-z_][A-Za-z0-9_]*)", urls_source))

        view_files = sorted((root / "core/views").rglob("*.py"))
        view_files.append(root / "core/template_clone_views.py")

        def_re = re.compile(r"^\s*def ([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*request\b")
        violations = []
        for path in view_files:
            lines = path.read_text().splitlines()
            for index, line in enumerate(lines):
                match = def_re.match(line)
                if not match:
                    continue
                name = match.group(1)
                if name not in routed or name in public_allowlist:
                    continue
                decorators = []
                cursor = index - 1
                while cursor >= 0:
                    stripped = lines[cursor].strip()
                    if stripped.startswith("@"):
                        decorators.append(stripped)
                        cursor -= 1
                        continue
                    if stripped == "" or stripped.startswith("#"):
                        cursor -= 1
                        continue
                    break
                blob = " ".join(decorators)
                if "app_login_required" not in blob and "login_required" not in blob:
                    violations.append(f"{path.relative_to(root)}:{index + 1} ({name})")

        self.assertEqual(
            violations,
            [],
            f"Routed views must be wrapped by app_login_required (only health_* are public): {', '.join(violations)}",
        )

    def test_production_tag_registry_writes_use_the_shared_service(self):
        root = Path(settings.BASE_DIR)
        allowed_path = Path("core/services/tag_registry.py")
        violations = []
        for path in sorted((root / "core").rglob("*.py")):
            relative_path = path.relative_to(root)
            if relative_path == allowed_path or "migrations" in relative_path.parts or path.name.startswith("tests"):
                continue
            for line_number, line in enumerate(path.read_text().splitlines(), start=1):
                if ".set_cluster_options(" in line:
                    violations.append(f"{relative_path}:{line_number}")

        self.assertEqual(
            violations,
            [],
            "Production tag registry writes must use "
            "core.services.tag_registry.mutate_registered_tags(): "
            f"{', '.join(violations)}",
        )


class TrashItemCreationContractTests(SimpleTestCase):
    """A trash row names its storage at creation, because its creator knows it.

    `TrashItem.save()` used to backfill `mount`/`storage_id` from `metadata` or
    from a `storage_id` lookup. No live writer had needed it since both creation
    paths in `services/storage_actions.py` started passing both fields, and the
    lookup gave up silently when one storage_id matched two mounts — so it read
    as a guarantee it could not make. Removing it moves the requirement here,
    where a third creation path that forgets fails loudly instead of producing a
    row that no datastore's trash view can attribute.
    """

    REQUIRED_KEYWORDS = {"mount", "storage_id"}

    def test_every_production_trash_item_names_its_mount_and_storage_id(self):
        root = Path(settings.BASE_DIR)
        found = 0
        violations = []
        for path in sorted((root / "core").rglob("*.py")):
            relative_path = path.relative_to(root)
            if "migrations" in relative_path.parts or path.name.startswith("tests"):
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr != "create" or ast.unparse(node.func) != "TrashItem.objects.create":
                    continue
                found += 1
                missing = sorted(self.REQUIRED_KEYWORDS - {keyword.arg for keyword in node.keywords})
                if missing:
                    violations.append(f"{relative_path}:{node.lineno} is missing {', '.join(missing)}")

        self.assertGreaterEqual(found, 2, "The TrashItem creation scan stopped finding the storage_actions paths.")
        self.assertEqual(violations, [], "Trash rows created without naming their storage.")


class MigrationStateInvariantTests(SimpleTestCase):
    """The migrations must say what the models say.

    Nothing checked this before: `0024` was hand-written because the container
    filesystem is read-only, and without this test a hand-written migration that
    got a field wrong would only surface as a runtime error against a schema
    nobody had rebuilt.
    """

    # Unlike its neighbours this one needs the database: the autodetector reads
    # `django_migrations` to check the applied history is consistent.
    databases = {"default"}

    def test_no_model_change_is_missing_a_migration(self):
        from io import StringIO

        from django.core.management import call_command

        output = StringIO()
        try:
            call_command("makemigrations", "--check", "--dry-run", stdout=output, stderr=output)
        except SystemExit:
            self.fail(f"Models have changed without a migration:\n{output.getvalue()}")


class InventoryIndexInvariantTests(SimpleTestCase):
    """Which indexes exist, and why — so the reasoning outlives the commit.

    Two kinds of claim live here, and they are settled by opposite evidence. The
    first three tests record a *measurement*: which indexes the database said it
    reads. The last one records a *structure*: which indexes cannot be needed
    whatever the database says. Conflating the two is what nearly removed the
    busiest index on `FileInventory`.

    A Round 9 finding called all four of `FileInventory`'s single-column indexes
    unusable and recommended removing them. Measured against the running
    database, `classification` and `content_category` were the two most-scanned
    indexes on the table; the finding's claim that `content_category` "is never
    filtered at all" is contradicted by `_storage_content_usage`, which filters
    it six times per call. Only the ones with zero scans *and* zero filters in
    the source were dropped.

    Asserting the exact set rather than "these two exist" so an index added
    without a measurement behind it also has to come through here.
    """

    def test_file_inventory_carries_exactly_the_indexes_that_were_measured(self):
        from core.models import FileInventory

        self.assertEqual(
            sorted(tuple(index.fields) for index in FileInventory._meta.indexes),
            [("classification",), ("content_category",), ("storage", "path")],
        )

    def test_the_file_inventory_storage_column_has_no_index_of_its_own(self):
        """It would be a strict prefix of `(storage, path)`, paid for per row on
        the largest table in the app."""
        from core.models import FileInventory

        self.assertFalse(FileInventory._meta.get_field("storage").db_index)

    def test_the_volume_observation_table_keeps_only_its_composite(self):
        """Its own Meta comment reorders a unique constraint to avoid a fourth
        index; two single-column ones had been added three lines below it."""
        from core.models import ClusterStorageVolumeObservation

        self.assertEqual(
            [tuple(index.fields) for index in ClusterStorageVolumeObservation._meta.indexes],
            [("cluster_storage", "observed_volume_generation")],
        )

    def test_no_indexed_column_is_a_strict_prefix_of_a_wider_index(self):
        """The rule the two tests above are single instances of.

        `FileInventory.storage` was found while fixing something else, and the
        sweep that followed found eighteen more across the module — every one a
        `ForeignKey` whose column also leads a composite, plus
        `ConsoleSession.status`. `0025` removed them.

        Stated model-wide rather than as nineteen assertions because the failure
        mode is additive: `ForeignKey` indexes by default, so a field added to a
        model whose composite already leads with that column silently costs a
        second btree on every write. Nothing about writing that line looks wrong.

        Note what this cannot be checked against. A prefix index is the *narrower*
        of two applicable indexes, so Postgres prefers it and its `idx_scan`
        climbs — the busiest one in the sweep had 115160 scans and was still
        pure overhead. Redundancy here is a structural property, not a measured
        one, which is exactly why it belongs in a source invariant.
        """
        from django.apps import apps

        def columns(model, index_fields):
            return tuple(model._meta.get_field(name.lstrip("-")).column for name in index_fields)

        def coverers(model):
            """Every index that can serve an arbitrary lookup on its leading columns.

            Partial indexes are excluded: a `condition` restricts the index to
            rows matching the predicate, so it answers only queries that carry the
            same restriction and cannot stand in for an unconditional one.
            """
            for index in model._meta.indexes:
                if getattr(index, "condition", None) is None and not getattr(index, "opclasses", None):
                    yield index.name, columns(model, index.fields)
            for constraint in model._meta.constraints:
                fields = getattr(constraint, "fields", None)
                if fields and getattr(constraint, "condition", None) is None:
                    yield constraint.name, columns(model, fields)

        redundant = []
        checked = 0
        for model in apps.get_app_config("core").get_models():
            wider = list(coverers(model))
            for field in model._meta.local_fields:
                if not (field.db_index and field.column):
                    continue
                checked += 1
                for name, cols in wider:
                    if len(cols) > 1 and cols[0] == field.column:
                        redundant.append(f"{model.__name__}.{field.name} is a prefix of {name} {cols}")
                        break

        self.assertGreater(checked, 0, "The sweep stopped finding indexed columns at all.")
        self.assertEqual(
            sorted(redundant),
            [],
            "Indexed columns that a wider index on the same model already leads with; "
            "pass db_index=False and add a migration.",
        )


MEMBERSHIP_OWNED_NODE_COLUMNS = frozenset(
    {
        "present",
        "online",
        "nodeid",
        "reported_ring_address",
        "membership_generation",
        "first_discovered_at",
        "last_discovered_at",
    }
)

NODE_RUNTIME_OWNER = "core/services/cluster_node_runtime.py"


class NodeRuntimeColumnOwnershipInvariantTests(SimpleTestCase):
    """5a1C writes runtime columns; 5a1B writes membership columns.

    The rule is enforced rather than asserted because the failure is silent and
    the mechanism is real: ``cluster_membership._publish_complete`` rewrites every
    node row with an argument-less ``save()`` from a snapshot taken earlier in its
    transaction. A bare ``save()`` on this side does the mirror image -- it would
    carry stale membership columns back over a fresher publication, and nothing in
    a diff would look wrong.
    """

    #: The one constructor of a coverage row in the publisher. A receiver assigned
    #: from it owns every one of its columns and may save in full.
    COVERAGE_FACTORY = "_coverage_for"

    def _owner_tree(self) -> ast.Module:
        return ast.parse((Path(settings.BASE_DIR) / NODE_RUNTIME_OWNER).read_text())

    def _assigns_coverage_row(self, value: ast.expr) -> bool:
        return (
            isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == self.COVERAGE_FACTORY
        )

    def test_the_owned_field_set_excludes_every_membership_column(self):
        """The check the ``update_fields`` arms all delegate to.

        Every write in the module passes ``update_fields=list(RUNTIME_FIELDS)``,
        so naming the keyword proves nothing on its own: widening that one tuple
        by a single membership column reopens the whole hole while every other
        arm stays green. This is the arm that closes it.
        """
        overlap = sorted(set(RUNTIME_FIELDS) & MEMBERSHIP_OWNED_NODE_COLUMNS)

        self.assertEqual(
            overlap,
            [],
            "RUNTIME_FIELDS names a column 5a1B owns, so every update_fields "
            "write in the node-runtime publisher now carries it: "
            f"{', '.join(overlap)}",
        )

    def test_every_shared_row_save_names_only_the_fields_it_owns(self):
        """Coverage rows belong wholly to this module and may save in full.

        ``ClusterNodeState`` does not: 5a1B owns half its columns, so every write
        to it here must name the runtime fields -- and name *only* those, which
        the companion test above enforces on the tuple they all pass.

        The exemption is keyed on the model the receiver was assigned from, not
        on the variable's name. Keying it on the name ``coverage`` meant binding
        a ``ClusterNodeState`` to that name silently bought the exemption.
        """
        tree = self._owner_tree()
        coverage_names = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name) and self._assigns_coverage_row(node.value)
        }

        bare = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "save"
            and not (isinstance(node.func.value, ast.Name) and node.func.value.id in coverage_names)
            and not any(keyword.arg == "update_fields" for keyword in node.keywords)
        ]

        self.assertEqual(
            bare,
            [],
            "An argument-less save() on a shared row in the node-runtime publisher "
            "writes every column, including the membership columns 5a1B owns, from "
            "whatever the in-memory row happened to hold. Pass update_fields "
            f"explicitly ({NODE_RUNTIME_OWNER} lines {bare}).",
        )

    def test_no_queryset_update_writes_a_membership_column(self):
        """The one form ``update_fields`` cannot protect against.

        ``ClusterNodeState.objects.filter(...).update(membership_generation=...)``
        is exactly what the membership publisher does, and it is neither a
        ``save()`` nor an attribute assignment -- so without this arm both other
        checks pass while the column is rewritten.
        """
        updates = [
            node
            for node in ast.walk(self._owner_tree())
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "update"
        ]

        offenders = sorted(
            {
                f"{keyword.arg}:{node.lineno}"
                for node in updates
                for keyword in node.keywords
                if keyword.arg in MEMBERSHIP_OWNED_NODE_COLUMNS
            }
        )

        # `**{"present": False}` has `keyword.arg is None`, so the named-keyword
        # scan above cannot see it. Reading the column names out of an arbitrary
        # expression is not decidable here, so the unpacking form is refused
        # outright: this module has no legitimate use for it.
        unpacked = sorted({str(node.lineno) for node in updates for keyword in node.keywords if keyword.arg is None})

        self.assertEqual(
            offenders,
            [],
            "A queryset update() in the node-runtime publisher writes a "
            "membership-owned column. Membership publication is 5a1B's: "
            f"{', '.join(offenders)}",
        )
        self.assertEqual(
            unpacked,
            [],
            "A queryset update() in the node-runtime publisher takes **kwargs, "
            "which hides the column names from this invariant. Name the columns "
            f"({NODE_RUNTIME_OWNER} lines {', '.join(unpacked)}).",
        )

    #: Writes that reach a column without a named keyword, an attribute target or a
    #: `save()` this module's other arms can read. Each one carried a green mutant
    #: that wrote a membership column: `setattr` hides the attribute name from the
    #: assignment arm, and `bulk_update` takes its field list positionally.
    OPAQUE_WRITE_CALLS = frozenset({"setattr", "bulk_update", "bulk_create"})

    def test_no_opaque_write_form_is_used_on_the_shared_row(self):
        """The arms above all read a *name*. These forms do not present one.

        Refused outright rather than analysed: deciding what
        `setattr(row, name, value)` writes means evaluating `name`, and this
        module has no legitimate use for any of them. `create`/`update_or_create`
        are absent for a different reason -- 5a1C never creates a
        ``ClusterNodeState`` row at all, which its own tests assert behaviourally.
        """
        offenders = sorted(
            {
                f"{node.func.id if isinstance(node.func, ast.Name) else node.func.attr}:{node.lineno}"
                for node in ast.walk(self._owner_tree())
                if isinstance(node, ast.Call)
                and (
                    (isinstance(node.func, ast.Name) and node.func.id in self.OPAQUE_WRITE_CALLS)
                    or (isinstance(node.func, ast.Attribute) and node.func.attr in self.OPAQUE_WRITE_CALLS)
                )
            }
        )

        self.assertEqual(
            offenders,
            [],
            "The node-runtime publisher uses a write form that hides the column "
            "name from every ownership check in this class. Assign the runtime "
            f"attribute directly and save with update_fields: {', '.join(offenders)}",
        )

    def test_membership_columns_are_never_assigned_by_the_runtime_publisher(self):
        offenders = sorted(
            {
                f"{node.attr}:{node.lineno}"
                for parent in ast.walk(self._owner_tree())
                if isinstance(parent, (ast.Assign, ast.AugAssign))
                for node in ast.walk(parent.targets[0] if isinstance(parent, ast.Assign) else parent.target)
                if isinstance(node, ast.Attribute) and node.attr in MEMBERSHIP_OWNED_NODE_COLUMNS
            }
        )

        self.assertEqual(
            offenders,
            [],
            "These membership-owned columns are assigned inside the node-runtime "
            "publisher. Membership publication is 5a1B's; a runtime refresh that "
            "moves them makes older membership evidence look freshly proven: "
            f"{', '.join(offenders)}",
        )

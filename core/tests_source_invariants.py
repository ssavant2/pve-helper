from __future__ import annotations

import ast
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

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
    STORAGE_WRITE_MODULES = (
        "core/services/storage_actions.py",
        "core/services/vm_register.py",
        "core/services/ovf_import.py",
        "core/views/storage.py",
    )

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
        "core/services/vm_register.py": set(),
        "core/services/ovf_import.py": set(),
        "core/views/storage.py": set(),
    }

    def _code_lines(self, relative_path: str) -> list[tuple[int, str]]:
        source = (Path(settings.BASE_DIR) / relative_path).read_text()
        return [
            (number, line)
            for number, line in enumerate(source.splitlines(), start=1)
            if not line.lstrip().startswith("#")
        ]

    def test_storage_writes_do_not_resolve_a_path_and_then_act_on_it(self):
        offenders: list[str] = []
        for relative_path in self.STORAGE_WRITE_MODULES:
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
        for relative_path in self.STORAGE_WRITE_MODULES:
            allowed = self.TRUSTED_ROOT_RESOLVERS[relative_path]
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


class BackendSourceInvariantTests(SimpleTestCase):
    def test_production_audit_writes_use_the_shared_service(self):
        root = Path(settings.BASE_DIR)
        allowed_path = Path("core/services/audit_events.py")
        violations = []
        for path in sorted((root / "core").rglob("*.py")):
            relative_path = path.relative_to(root)
            if relative_path == allowed_path or "migrations" in relative_path.parts or path.name.startswith("tests"):
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
    """The indexes `0024` kept, and why — so the measurement outlives the commit.

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

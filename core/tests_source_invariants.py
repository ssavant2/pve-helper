from __future__ import annotations

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


# The global `configured_clients()` fan-out is what Phase 1b replaces: it selects
# clients from settings with no cluster scope, so a caller can reach another
# cluster's guest by VMID. This list is a ratchet, not a permission: it records the
# surface that still has to migrate, and it may only ever shrink. Prune a module
# from it once its callers pass an explicit cluster; never add one.
LEGACY_CONFIGURED_CLIENTS_MODULES = frozenset(
    {
        "core/services/console_sessions.py",
        "core/services/guest_create.py",
        "core/services/proxmox.py",
        "core/services/tag_actions.py",
        "core/services/tag_registry.py",
        "core/services/vm_register.py",
        "core/tasks.py",
        "core/template_clone_views.py",
        "core/views/common.py",
        "core/views/guests/_core.py",
        "core/views/guests/actions.py",
        "core/views/guests/dialogs.py",
        "core/views/guests/hardware.py",
        "core/views/guests/mutations.py",
        "core/views/guests/read_model_support.py",
        "core/views/guests/replication.py",
        "core/views/guests/tabs.py",
        "core/views/scheduling.py",
        "core/views/storage.py",
    }
)

# The legacy scope adapter may only be called at an entry point that has no
# GuestRef/NodeRef/path scope yet, and Phase 4 deletes it before activation. An
# empty allowlist is the exit condition, so this list may also only shrink.
LEGACY_CLUSTER_SCOPE_ADAPTER_ALLOWLIST = frozenset(
    {
        "core/services/cluster_resolver.py",  # the definition itself
        # The single boundary where guest writes resolve scope. Phase 3 gives
        # `detail` a GuestRef and this call goes away with the adapter in Phase 4.
        "core/views/guests/operation_lifecycle.py",
    }
)

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

    def test_no_new_module_uses_the_global_client_fan_out(self):
        offenders = sorted(self._modules_containing("configured_clients") - LEGACY_CONFIGURED_CLIENTS_MODULES)

        self.assertEqual(
            offenders,
            [],
            "New code must resolve clients from an explicit cluster via "
            "core.services.cluster_resolver, not the global configured_clients() "
            f"fan-out: {', '.join(offenders)}",
        )

    def test_legacy_client_fan_out_list_has_no_stale_entries(self):
        actual = self._modules_containing("configured_clients")
        stale = sorted(LEGACY_CONFIGURED_CLIENTS_MODULES - actual)

        self.assertEqual(
            stale,
            [],
            "These modules no longer use configured_clients(); remove them from "
            f"LEGACY_CONFIGURED_CLIENTS_MODULES so the ratchet cannot loosen: {', '.join(stale)}",
        )

    def test_legacy_scope_adapter_stays_on_its_allowlist(self):
        offenders = sorted(self._modules_containing(LEGACY_ADAPTER_NAME) - LEGACY_CLUSTER_SCOPE_ADAPTER_ALLOWLIST)

        self.assertEqual(
            offenders,
            [],
            f"{LEGACY_ADAPTER_NAME}() may only be called from allowlisted entry points "
            f"and is deleted before activation: {', '.join(offenders)}",
        )


class FrontendSourceInvariantTests(SimpleTestCase):
    def _frontend_sources(self) -> list[Path]:
        root = Path(settings.BASE_DIR)
        return sorted((root / "static/js/app").glob("*.js")) + sorted(
            (root / "templates/core").rglob("*.html")
        )

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
            "window.prompt() is reserved for the console paste safeguard; use "
            "the shared application dialog elsewhere.",
        )


class BackendSourceInvariantTests(SimpleTestCase):
    def test_production_audit_writes_use_the_shared_service(self):
        root = Path(settings.BASE_DIR)
        allowed_path = Path("core/services/audit_events.py")
        violations = []
        for path in sorted((root / "core").rglob("*.py")):
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
            "Routed views must be wrapped by app_login_required (only health_* are "
            f"public): {', '.join(violations)}",
        )

    def test_production_tag_registry_writes_use_the_shared_service(self):
        root = Path(settings.BASE_DIR)
        allowed_path = Path("core/services/tag_registry.py")
        violations = []
        for path in sorted((root / "core").rglob("*.py")):
            relative_path = path.relative_to(root)
            if (
                relative_path == allowed_path
                or "migrations" in relative_path.parts
                or path.name.startswith("tests")
            ):
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

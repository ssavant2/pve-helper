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

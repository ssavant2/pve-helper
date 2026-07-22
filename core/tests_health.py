"""Readiness must fail when the image and the database schema disagree.

Review 10: production's web healthcheck called `/healthz/live`, which returns a
constant 200, and `/healthz/ready` proved only that Postgres accepts a
connection. Nginx therefore started against a web service that was healthy by
every available measure and returned 500 on every page, which is what the
v0.1.1->v0.1.2 bring-up produced. These tests pin both halves: what the probes
report, and that the compose files actually ask the right one.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import yaml
from django.conf import settings
from django.test import TestCase

from core.services.health import readiness_report, reset_schema_cache, schema_state

COMPOSE_FILES = ("docker-compose.yml", "docker-compose.example.yml", "docker-compose.production.yml")


class _FakeExecutor:
    """A migration executor whose plan is whatever the test says it is."""

    def __init__(self, *plans):
        self._plans = list(plans)
        self.constructions = 0

    def __call__(self, _connection):
        self.constructions += 1
        return self

    @property
    def loader(self):
        return self

    @property
    def graph(self):
        return self

    def leaf_nodes(self):
        return [("core", "9999_leaf")]

    def migration_plan(self, _targets):
        return self._plans.pop(0) if len(self._plans) > 1 else self._plans[0]


class SchemaReadinessTests(TestCase):
    def setUp(self):
        reset_schema_cache()
        self.addCleanup(reset_schema_cache)

    def test_liveness_answers_without_touching_the_database(self):
        # A probe that queries Postgres turns a brief database blip into a
        # restarted web container. Liveness stays dumb on purpose.
        with self.assertNumQueries(0):
            response = self.client.get("/healthz/live")
        self.assertEqual(response.status_code, 200)

    def test_readiness_is_ok_when_the_schema_carries_every_migration(self):
        response = self.client.get("/healthz/ready")

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["checks"], {"database": "ok", "migrations": "ok"})

    def test_a_pending_migration_makes_the_service_not_ready(self):
        executor = _FakeExecutor([("core", "0101_x"), ("core", "0102_y")])

        with patch("core.services.health.MigrationExecutor", executor):
            response = self.client.get("/healthz/ready")

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.content)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["checks"]["migrations"], "pending")
        self.assertEqual(payload["checks"]["pending_count"], 2)

    def test_the_body_counts_the_gap_without_naming_it(self):
        # `/healthz/ready` is unauthenticated, so it is on the wrong side of the
        # trust boundary to describe the schema it is missing.
        executor = _FakeExecutor([("core", "0101_add_secret_column")])

        with patch("core.services.health.MigrationExecutor", executor):
            response = self.client.get("/healthz/ready")

        self.assertNotIn("0101_add_secret_column", response.content.decode())
        self.assertNotIn("core", response.content.decode())

    def test_readiness_turns_green_when_migrate_runs_under_it(self):
        # Why the probe repeats rather than deciding once at startup: the
        # operator applies the migrations against the running stack, and web
        # must recover without being restarted just to clear a marker.
        executor = _FakeExecutor([("core", "0101_x")], [])

        with patch("core.services.health.MigrationExecutor", executor):
            self.assertFalse(schema_state().current)
            self.assertTrue(schema_state().current)

    def test_a_confirmed_schema_is_never_examined_again(self):
        # The image's migration set is frozen at build time, so one confirmation
        # holds for the life of the process and the 30s probe costs nothing.
        executor = _FakeExecutor([])

        with patch("core.services.health.MigrationExecutor", executor):
            for _ in range(5):
                self.assertTrue(schema_state().current)

        self.assertEqual(executor.constructions, 1)

    def test_a_failed_schema_check_is_not_read_as_readiness(self):
        with patch("core.services.health.MigrationExecutor", side_effect=RuntimeError("no such table")):
            payload, status = readiness_report("pve-helper")

        self.assertEqual(status, 503)
        self.assertEqual(payload["checks"]["migrations_error"], "RuntimeError")
        self.assertNotIn("no such table", json.dumps(payload))


class ConsoleReadinessTests(TestCase):
    """The gateway reads the same tables through the same ORM, so a stale schema
    breaks it too — and nginx waits on its health just as it waits on web's."""

    def test_the_gateway_serves_readiness_as_well_as_liveness(self):
        from console_app.main import app

        paths = {getattr(route, "path", "") for route in app.routes}
        self.assertIn("/healthz/ready", paths)
        self.assertIn("/healthz/live", paths)


class ComposeProbeContractTests(TestCase):
    """The endpoint existed before this review and nothing asked for it. The gap
    was the wiring, so the wiring is what this test holds down."""

    def _healthcheck(self, name: str, service: str) -> str:
        compose = yaml.safe_load(Path(settings.BASE_DIR, name).read_text(encoding="utf-8"))
        return " ".join(compose["services"][service]["healthcheck"]["test"])

    def test_web_and_console_are_gated_on_readiness_everywhere(self):
        for name in COMPOSE_FILES:
            for service in ("web", "console"):
                with self.subTest(compose=name, service=service):
                    test = self._healthcheck(name, service)
                    self.assertIn("/healthz/ready", test)
                    self.assertNotIn("/healthz/live", test)

    def test_nginx_waits_for_both_application_services(self):
        # Readiness is only a deployment gate if something depends on it.
        for name in COMPOSE_FILES:
            compose = yaml.safe_load(Path(settings.BASE_DIR, name).read_text(encoding="utf-8"))
            depends = compose["services"]["nginx"]["depends_on"]
            with self.subTest(compose=name):
                self.assertEqual(depends["web"]["condition"], "service_healthy")
                self.assertEqual(depends["console"]["condition"], "service_healthy")

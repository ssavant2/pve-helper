#!/usr/bin/env sh
set -eu

# The `migrate` service applies the schema once, as a Compose job that every
# application service waits on, so the stop/migrate/start dance this script used
# to perform is now the platform's job rather than the operator's.
#
# What is still deliberately manual: the one-way cutovers
# (`complete_trust_cutover`, `complete_credential_cutover`). They are separate
# management commands, not migrations, and an upgrade from a pre-multicluster
# release still needs them — see docs/deployment-runbook.md.
docker compose pull
docker compose up -d --remove-orphans --wait
docker compose ps

# pve-helper

Internal Proxmox helper toolbox.

Current skeleton:

- Django + Postgres
- Django Q2 worker
- Authentik OIDC configuration placeholders
- Proxmox API client shell
- Read-only storage scanner shell
- Health endpoints
- vSphere-inspired server-rendered UI shell
- Django admin models for audit, scans, storage, and inventory
- Local Lucide icon bundle for navigation icons

Start with `docs/deployment-runbook.md`.

The compose defaults are for local skeleton verification. Before real internal use, create `.env`, set real secrets, configure Authentik OIDC, and keep `APP_REQUIRE_LOGIN=true`.

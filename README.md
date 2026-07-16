# pve-helper

> [!WARNING]
> **Early alpha software — use at your own risk.** pve-helper is under active
> development and has not reached version 1.0. It administers infrastructure
> and can perform destructive operations; bugs, breaking configuration changes
> and incomplete recovery paths should be expected. Test it away from critical
> workloads, keep independent backups and verify every permission before
> enabling storage writes.

Self-hosted Proxmox administration toolbox.

`pve-helper` is intended to be a small internal web toolbox for Proxmox
environments, starting with storage browsing/inventory helpers and growing into
additional modules over time.

## Current Skeleton

- Django + Postgres
- Django Q2 worker
- Built-in OIDC login and group authorization
- Proxmox API client shell
- Read-only storage scanner with optional gated upload/trash/restore actions
- Health endpoints
- vSphere-inspired server-rendered UI shell
- Django admin models for audit, scans, storage, and inventory
- Local Lucide icon bundle for navigation icons

## Deployment Model

The app serves HTTP through its nginx front container. It can be used directly
on a trusted internal network or placed behind any external reverse proxy that
terminates TLS. pve-helper does not manage certificates and TLS is not required
for the services to start.

The optional HTTPS reference deployment is:

1. Reverse proxy / TLS endpoint, for example Nginx Proxy Manager.
2. `pve-helper` nginx front container.
3. `pve-helper` Django/Gunicorn web container.
4. Authentik OIDC login flow, initiated and enforced by the app with a required
   group claim.

Without an external proxy, the browser connects directly to the pve-helper nginx
port over HTTP. With one, HTTP traffic from that proxy reaches the same port and
the app redirects the browser to Authentik when login is required.
The nginx front container also serves authorized datastore downloads from
read-only storage mounts after Django has approved the request, so very large
downloads do not stream through the Django worker.

Other front-door auth patterns can be used, but the app has native OIDC support
and should still enforce its own login and group authorization with
`APP_REQUIRE_LOGIN=true`.

## Storage Access

The storage inventory features require direct read access to the shared storage
that Proxmox uses. How that access is provided is deliberately deployment
specific.

In the reference homelab deployment, the Docker host has an extra NIC in the
storage VLAN and mounts the TrueNAS NFS exports on the host. Other setups can
use a different network and mount design, as long as the container can read the
same storage paths that Proxmox references.

Upload, trash, and restore actions depend on the effective Docker bind-mount
mode for each storage. A datastore mounted read-only into the app remains
read-only even when another datastore is writable. See `docs/deployment-runbook.md`.

This project is meant for internal homelab / small ops use. It is not designed
or supported as an enterprise storage-management product.

## System requirements

For one production stack, the supported baseline is **2 vCPU and 2 GB RAM**;
**2 vCPU and 4 GB RAM** is recommended. Four vCPU and 8 GB RAM is a capacity
tier for overlapping bulk work, not the normal requirement. A host running both
production and a development stack should have 6–8 GB RAM. Storage mounted for
Proxmox content is additional and is not included in these figures.

Docker `mem_limit` values are safety ceilings, not reservations and not additive
host requirements. See the deployment runbook for workload assumptions and dev
tuning.

## Quick install

No source checkout is required. Download the two release files, inspect and
edit the environment file, then start the stack:

```bash
mkdir pve-helper && cd pve-helper
curl -fLo docker-compose.yml https://github.com/ssavant2/pve-helper/releases/latest/download/docker-compose.yml
curl -fLo .env https://github.com/ssavant2/pve-helper/releases/latest/download/example.env
mkdir -p certs && touch certs/ca-bundle.pem
# Edit .env and fill every required blank value before continuing.
docker compose config --quiet
docker compose up -d --wait db
docker compose run --rm --no-deps web python manage.py migrate --noinput
docker compose up -d --wait
```

See [INSTALL.md](INSTALL.md) for the complete first-install, storage and
authentication instructions. The app serves HTTP itself; HTTPS, if wanted, is
provided by an external reverse proxy.

## Development

Clone the repository and use `docker-compose.example.yml` as the tracked
development template. Development builds the Dockerfile's local `test` target;
only the lean `runtime` target is published to GHCR.

## Setup reference

Start with `docs/deployment-runbook.md`. For day-to-day administration, see
`docs/user-manual.md`. For Authentik, see
`docs/authentik-oidc-setup.md`. For Proxmox API credentials, see
`docs/proxmox-api-token.md`. For database role separation, see
`docs/postgres-hardening.md`.

The production Compose file refuses to start without the required secrets,
Proxmox connection and storage paths. Keep `APP_REQUIRE_LOGIN=true` unless the
installation is isolated on a trusted network. `APP_BASE_URL` is the canonical
browser-facing URL, using `http://` for direct access or `https://` when an
external proxy supplies TLS. Its scheme also controls Secure session/CSRF
cookies. The URL shown in the app header is taken from the current request.

## Documentation

- Deployment: `docs/deployment-runbook.md`
- End-user guide: `docs/user-manual.md`
- OIDC / Authentik setup: `docs/authentik-oidc-setup.md`

Build/test/lint and the Playwright E2E workflow for working on the source live in
a local `AGENTS.md` (kept out of git and out of published images — the deliverable
is a built image, not the source tree).

## License

pve-helper is licensed under the [GNU Affero General Public License v3.0](LICENSE)
(`AGPL-3.0-only`). You may use, modify and redistribute it, including
commercially. If you distribute a modified version or let users interact with
one over a network, the corresponding source must remain available under the
same license.

# pve-helper

Internal Proxmox helper toolbox.

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

Run the app behind a reverse proxy that terminates TLS, then enable real
authentication before using it against live Proxmox storage.

The reference deployment is:

1. Reverse proxy / TLS endpoint, for example Nginx Proxy Manager.
2. `pve-helper` nginx front container.
3. `pve-helper` Django/Gunicorn web container.
4. Authentik OIDC login flow, initiated and enforced by the app with a required
   group claim.

In other words, normal HTTP traffic goes through the reverse proxy to the app;
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

## Setup

Start with `docs/deployment-runbook.md`. For day-to-day administration, see
`docs/user-manual.md`. For Authentik, see
`docs/authentik-oidc-setup.md`. For Proxmox API credentials, see
`docs/proxmox-api-token.md`. For database role separation, see
`docs/postgres-hardening.md`.

The compose defaults are for local skeleton verification. Before real internal use, create `.env`, set real secrets, configure Authentik OIDC, and keep `APP_REQUIRE_LOGIN=true`. `APP_BASE_URL` should be the canonical URL that Authentik redirects back to; the URL shown in the app header is taken from the current request.

## Development Checks

The app image copies the source tree at build time. The running `web`/`worker`
containers do not bind-mount the checkout, so Python/template changes are not
visible inside `docker compose exec web ...` until the image is rebuilt and the
container is recreated.

After changing Python, templates, or bundled static assets, rebuild before
container-based tests:

```bash
docker compose build web worker
docker compose run --rm \
  -e DB_USER="$DB_ADMIN_USER" \
  -e DB_PASSWORD="$DB_ADMIN_PASSWORD" \
  web python manage.py test --settings=pve_helper.test_settings --keepdb
```

The default test settings block every unmocked Proxmox HTTP request. Run live
integration checks only as a separately reviewed, explicit command with the
normal settings and suitable non-production infrastructure.

Then restart the running app containers so the browser sees the same code that
was tested:

```bash
docker compose up -d web worker worker-bulk console nginx
```

JavaScript linting/formatting runs through Docker, so Node.js does not need to
be installed on the host:

```bash
docker compose -f docker-compose.tools.yml run --rm js-check
docker compose -f docker-compose.tools.yml run --rm js-format
```

The tools container writes `node_modules/` locally for speed; it is ignored by
git.

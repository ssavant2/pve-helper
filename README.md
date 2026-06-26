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
- Read-only storage scanner shell
- Health endpoints
- vSphere-inspired server-rendered UI shell
- Django admin models for audit, scans, storage, and inventory
- Local Lucide icon bundle for navigation icons

## Deployment Model

Run the app behind a reverse proxy that terminates TLS, then enable real
authentication before using it against live Proxmox storage.

The reference deployment is:

1. Reverse proxy / TLS endpoint, for example Nginx Proxy Manager.
2. `pve-helper` web container.
3. Authentik OIDC login flow, initiated and enforced by the app with a required
   group claim.

In other words, normal HTTP traffic goes through the reverse proxy to the app;
the app redirects the browser to Authentik when login is required.

Other front-door auth patterns can be used, but the app has native OIDC support
and should still enforce its own login and group authorization with
`APP_REQUIRE_LOGIN=true`.

## Storage Access

The storage inventory features require direct read access to the shared storage
that Proxmox uses. How that access is provided is deliberately deployment
specific.

In the reference homelab deployment, the Docker host has an extra NIC in the
storage VLAN and mounts the TrueNAS NFS exports read-only on the host. Other
setups can use a different network and mount design, as long as the container
can read the same storage paths that Proxmox references.

This project is meant for internal homelab / small ops use. It is not designed
or supported as an enterprise storage-management product.

## Setup

Start with `docs/deployment-runbook.md`. For Authentik, see
`docs/authentik-oidc-setup.md`. For Proxmox API credentials, see
`docs/proxmox-api-token.md`.

The compose defaults are for local skeleton verification. Before real internal use, create `.env`, set real secrets, configure Authentik OIDC, and keep `APP_REQUIRE_LOGIN=true`. `APP_BASE_URL` should be the canonical URL that Authentik redirects back to; the URL shown in the app header is taken from the current request.

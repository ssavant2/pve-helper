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

## Current capabilities

- Proxmox VM and container inventory, detail views and lifecycle operations
- Console, clone, migration, backup, restore and hardware workflows
- Storage inventory, orphan detection and optional upload/trash/restore actions
- Scheduled tasks, tag administration, Recent Tasks and a durable audit trail
- Built-in OIDC login and group authorization
- Django, Postgres and Django Q2 behind a vSphere-inspired server-rendered UI

## Deployment

pve-helper is distributed as prebuilt containers and serves HTTP through its
own nginx front container. HTTPS, when wanted, is terminated by an external
reverse proxy; pve-helper neither provisions nor requires a certificate.
Native OIDC login and group authorization can be enabled independently of the
chosen reverse proxy.

The supported platform baseline is **Proxmox VE 9.2+**, **Docker Engine 25+**
with Compose v2, and a **Linux 5.12+ kernel** on the Docker host. PVE 8.x and
older Docker/kernel combinations are unsupported; see the installation guide
for the feature and storage-safety reasons behind these minimums.

Storage and volume inventory comes from the Proxmox API. A host mount is optional
and adds file browsing only for file-tree backends. Application writes are enabled
by default; a datastore mounted read-only stays read-only regardless, and the
global setting can be turned off to freeze writes during maintenance. This
project targets internal homelab and small-operations use; it is not an enterprise
storage-management product.

## Installation

[Read the installation guide](INSTALL.md) for requirements, the two-file quick
install, storage permissions, authentication, upgrades and source-based local
development. `INSTALL.md` is the canonical installation reference.

## Documentation

- Deployment: `docs/deployment-runbook.md`
- End-user guide: `docs/user-manual.md`
- Authentication and generic OIDC setup: `docs/oidc-setup.md`
- Tested Authentik walkthrough (optional): `docs/authentik-oidc-setup.md`
- Tested TrueNAS ACL walkthrough (optional): `docs/truenas-acl-pve-helper.md`
- Security policy and vulnerability reporting: `SECURITY.md`

pve-helper does not require Authentik or TrueNAS. The vendor-specific guides are
more detailed, tested walkthroughs for operators who use those products; other
standards-compatible OIDC providers, reverse proxies and supported storage
backends can be configured from the generic documentation.

## License

pve-helper is licensed under the [GNU Affero General Public License v3.0](LICENSE)
(`AGPL-3.0-only`). You may use, modify and redistribute it, including
commercially. If you distribute a modified version or let users interact with
one over a network, the corresponding source must remain available under the
same license.

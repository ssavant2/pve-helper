# pve-helper deployment runbook

> Hostnames, groups, IPs and exports below are generic placeholders. Substitute your own
> environment's values in `.env` (which is gitignored) — do not commit real values.

## First local skeleton run

1. Copy `.env.example` to `.env`.
2. Replace at least `APP_SECRET_KEY` and `DB_PASSWORD`.
3. Start Postgres:

   ```bash
   docker compose up -d db
   ```

4. Run migrations:

   ```bash
   docker compose run --rm web python manage.py migrate
   ```

5. Create an admin user for local inspection:

   ```bash
   docker compose run --rm web python manage.py createsuperuser
   ```

6. Start the app:

   ```bash
   docker compose up -d web worker
   ```

7. Open `http://dockerhost:21080` directly or configure NPM for `https://pve-helper.example.com`.

## NFS mounts

For production, mount the NFS exports on the Docker host before starting the app.
Manual `mount` commands do not survive reboot, so use `/etc/fstab` or an equivalent
systemd mount setup.

Install the NFS client package on the Docker host:

```bash
sudo apt update
sudo apt install nfs-common
```

Create the mount points:

```bash
sudo mkdir -p /mnt/pve-helper/truenas-fs /mnt/pve-helper/truenas-vm
```

Recommended initial `/etc/fstab` entries:

```fstab
203.0.113.10:/export/proxmox-fs /mnt/pve-helper/truenas-fs nfs4 ro,vers=4.2,proto=tcp,nconnect=4,hard,timeo=600,retrans=2,noatime,_netdev,nofail,x-systemd.automount,x-systemd.idle-timeout=600,x-systemd.requires=network-online.target,x-systemd.after=network-online.target 0 0
203.0.113.10:/export/proxmox-vm /mnt/pve-helper/truenas-vm nfs4 ro,vers=4.2,proto=tcp,nconnect=4,hard,timeo=600,retrans=2,noatime,_netdev,nofail,x-systemd.automount,x-systemd.idle-timeout=600,x-systemd.requires=network-online.target,x-systemd.after=network-online.target 0 0
```

For this environment, substitute:

```fstab
203.0.113.20:/mnt/Pool-FS/FS/Proxmox /mnt/pve-helper/truenas-fs nfs4 ro,vers=4.2,proto=tcp,nconnect=4,hard,timeo=600,retrans=2,noatime,_netdev,nofail,x-systemd.automount,x-systemd.idle-timeout=600,x-systemd.requires=network-online.target,x-systemd.after=network-online.target 0 0
203.0.113.20:/mnt/Pool-VMs/VM/Proxmox /mnt/pve-helper/truenas-vm nfs4 ro,vers=4.2,proto=tcp,nconnect=4,hard,timeo=600,retrans=2,noatime,_netdev,nofail,x-systemd.automount,x-systemd.idle-timeout=600,x-systemd.requires=network-online.target,x-systemd.after=network-online.target 0 0
```

Apply and verify:

```bash
sudo systemctl daemon-reload
sudo mount /mnt/pve-helper/truenas-fs
sudo mount /mnt/pve-helper/truenas-vm
findmnt -T /mnt/pve-helper/truenas-fs
findmnt -T /mnt/pve-helper/truenas-vm
```

Then recreate the app containers so Docker binds the mounted NFS trees, not the empty
underlying directories:

```bash
docker compose up -d --force-recreate web worker
```

Keep both the host NFS mounts and the Docker bind mounts read-only until write/trash
support is deliberately enabled. When trash support is added later, the host mount and
compose bind mount for the affected storage must be changed to read-write.

`nconnect=4` is a good starting point on modern Ubuntu kernels. It lets one NFS mount
use multiple TCP connections, which can help throughput and parallel directory walks
without changing the app. Increase only if measurements show a benefit.

If the app can see the NFS export but cannot enter Proxmox VM directories such as
`images/500`, add a read/traverse ACL for the app identity. See
`docs/truenas-acl-pve-helper.md`.

## Authentik

Follow `docs/authentik-oidc-setup.md`.

Set:

```env
APP_REQUIRE_LOGIN=true
OIDC_ISSUER_URL=https://authentik.example.internal/application/o/pve-helper/
OIDC_CLIENT_ID=<from-authentik>
OIDC_CLIENT_SECRET=<from-authentik>
OIDC_REQUIRED_GROUP=pve-helper-admins
```

For an internal-only deployment, point `OIDC_ISSUER_URL` at the internal Authentik URL
that fully serves the flow UI. If that URL (or Proxmox) uses an internal/private CA, the
container must trust it for back-channel TLS — see the next section.

## Internal CA trust (back-channel TLS)

The app makes server-to-server HTTPS calls: OIDC token/JWKS to Authentik, and the Proxmox
API. If those hosts present internal-CA-signed certificates, the container must trust that
CA or the calls fail with `SSLError` (visible as a 500 right after the OIDC redirect).

1. Build a PEM bundle containing public roots plus your internal CA, e.g.:

   ```bash
   cat /etc/ssl/certs/ca-certificates.crt /path/to/internal-ca.pem > certs/ca-bundle.pem
   ```

2. Mount it read-only into `web` and `worker` (see `docker-compose.yml`) and set:

   ```env
   REQUESTS_CA_BUNDLE=/etc/ssl/pve-helper-ca-bundle.pem
   ```

`REQUESTS_CA_BUNDLE` covers the OIDC path (which uses `requests`). The Proxmox client uses
`httpx`; with publicly-trusted Proxmox certs it needs nothing extra, otherwise set
`PVE_CA_BUNDLE` to an internal-CA path.

The `certs/` directory is gitignored — CA material is environment-specific and must not be
committed.

## Proxmox

Create a dedicated read-only API token. The intended starting role is `PVEAuditor` on the
required paths.

For the full UI walkthrough, see `docs/proxmox-api-token.md`.

Set:

```env
PVE_ENDPOINTS=https://pve1.example.com:8006
PVE_VERIFY_TLS=true
PVE_API_TOKEN_ID=<token-id>
PVE_API_TOKEN_SECRET=<token-secret>
```

If Proxmox uses an internal CA, set `PVE_CA_BUNDLE` to a mounted internal-CA path.

## Storage consumer safety

Before any additional Proxmox node can mount either shared NFS export, add that node to the
matching storage's expected consumers. The orphan classifier must see every expected
consumer in the same scan-run, otherwise files are marked `unknown` / `classification_blocked`
instead of `likely_orphan`.

Initial single-node value:

```yaml
expected_consumers:
  - pve1
```

Future value before further nodes consume the storage:

```yaml
expected_consumers:
  - pve1
  - pve2
  - pve3
```

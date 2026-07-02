# pve-helper deployment runbook

> Hostnames, groups, IPs and exports below are generic placeholders. Substitute your own
> environment's values in `.env` (which is gitignored) — do not commit real values.

## First local skeleton run

1. Copy `.env.example` to `.env`.
2. Replace at least `APP_SECRET_KEY`, `DB_ADMIN_PASSWORD`, and `DB_PASSWORD`.
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
   docker compose up -d nginx web worker
   ```

7. Open `http://dockerhost:21080` directly or configure NPM for `https://pve-helper.example.com`.

The `nginx` service owns the public app port and proxies normal requests to the
internal Django/Gunicorn `web` service. Authorized datastore downloads are served
directly by nginx from read-only storage mounts after Django has performed auth,
path validation, and audit logging.

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

The examples use two storages:

- `nfs-fs` for general files such as ISOs, backups, templates, and other
  capacity-oriented content. In a home lab this might be backed by spinning
  disks.
- `nfs-vm` for VM disk images. In a home lab this might be backed by SSDs.

These names are only examples. Use storage IDs that match your Proxmox storage
configuration.

Recommended initial `/etc/fstab` entries:

```fstab
truenas.example.com:/mnt/tank/proxmox-fs /mnt/pve-helper/truenas-fs nfs4 ro,vers=4.2,proto=tcp,nconnect=4,hard,timeo=600,retrans=2,noatime,_netdev,nofail,x-systemd.automount,x-systemd.idle-timeout=600,x-systemd.requires=network-online.target,x-systemd.after=network-online.target 0 0
truenas.example.com:/mnt/tank/proxmox-vm /mnt/pve-helper/truenas-vm nfs4 ro,vers=4.2,proto=tcp,nconnect=4,hard,timeo=600,retrans=2,noatime,_netdev,nofail,x-systemd.automount,x-systemd.idle-timeout=600,x-systemd.requires=network-online.target,x-systemd.after=network-online.target 0 0
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
docker compose up -d --force-recreate nginx web worker
```

Keep both the host NFS mounts and the Docker bind mounts read-only until you are ready
to allow upload/trash/restore on a specific storage. When writes are allowed for that
storage, change both the host mount and the compose bind mount for the affected storage
to read-write.

`nconnect=4` is a good starting point on modern Ubuntu kernels. It lets one NFS mount
use multiple TCP connections, which can help throughput and parallel directory walks
without changing the app. Increase only if measurements show a benefit.

If the app can see the NFS export but cannot enter Proxmox VM directories such as
`images/<vmid>`, add a read/traverse ACL for the app identity. See
`docs/truenas-acl-pve-helper.md`.

## Storage write mode

File upload, move-to-trash, and restore actions are enabled in app configuration by
default, but they still require the effective storage mount to be writable from inside
the `web` container. This lets one datastore stay read-only while another is writable.

To allow writes for a storage:

1. Change the affected host NFS mount from `ro` to `rw`.

2. Change the affected Docker bind mount from `:ro` to `:rw`, then recreate `web` and
   `worker`. The `nginx` storage mounts should stay read-only; nginx only serves
   authorized downloads and proxies write requests to Django.

`STORAGE_WRITE_ENABLED=false` is still available as a global emergency brake. It hides
write controls and rejects write requests even if a storage is mounted read-write.
`STORAGE_UPLOAD_MAX_SIZE_MB` controls the upload limit; `0` means no app-level limit.
`FILE_UPLOAD_TEMP_DIR` controls where Django stores multipart upload chunks before the
view writes the final file to its target path. For large datastore uploads, do not leave
this on container `/tmp`: `/tmp` is a tmpfs in the hardened container and large uploads
can kill the Gunicorn worker with out-of-memory errors before the app code sees the
file. Put `FILE_UPLOAD_TEMP_DIR` on real storage with enough free space, for example:

```env
FILE_UPLOAD_TEMP_DIR=/storages/truenas-fs/.pve-helper-upload-tmp
```

Create that directory on the writable storage and make sure the app UID/GID can write
to it before testing large uploads.

By default, authorized datastore downloads use `X-Accel-Redirect`:

- Django validates the requested storage/path and writes the audit event.
- The pve-helper nginx sidecar serves the file bytes from a read-only mount.
- The visible download URL stays the same.

Set `STORAGE_DOWNLOAD_ACCEL_ENABLED=false` to fall back to Django/Gunicorn streaming.
When fallback streaming is used, keep `GUNICORN_TIMEOUT` high enough for the largest
expected file transfer. The container disables Gunicorn `sendfile` and sends
`X-Accel-Buffering: no` on downloads to avoid bursty NFS-to-proxy buffering for large
files.

For Nginx Proxy Manager, add equivalent settings in the proxy host's advanced
configuration when this app is used for large datastore uploads/downloads:

```nginx
proxy_buffering off;
proxy_request_buffering off;
proxy_max_temp_file_size 0;
proxy_read_timeout 86400s;
proxy_send_timeout 86400s;
send_timeout 86400s;
client_max_body_size 0;
```

`client_max_body_size 0` removes the NPM upload limit. Use a concrete size instead if
your deployment should enforce a proxy-level upload policy.

Browser note for large transfers:

- Firefox has behaved reliably for large qcow2 downloads in testing and shows progress
  immediately.
- Chrome has shown misleading or broken behavior with very large files: the UI can sit
  at `0 bytes`, create `Unconfirmed ... .crdownload` files, issue multiple Range
  requests, or appear to restart a download even while the server is already streaming
  data at full speed.
- Prefer Firefox for large datastore uploads/downloads until Chrome's behavior is
  better understood in your environment.

The app still keeps destructive behavior narrow: files are moved to `.trash/pve-helper`
on the same storage, not permanently deleted, and V1 only offers trash actions for files
classified as `likely_orphan`.

## PostgreSQL roles

The app should connect as `DB_USER`, not the Postgres bootstrap/admin role.
For new deployments this is handled by the init script mounted into
`/docker-entrypoint-initdb.d`. For existing deployments that were initialized
with `DB_USER` as `POSTGRES_USER`, follow `docs/postgres-hardening.md` once
before switching the DB service to `DB_ADMIN_USER`.

## Authentik

Follow `docs/authentik-oidc-setup.md`.

Set:

```env
APP_REQUIRE_LOGIN=true
OIDC_ISSUER_URL=https://auth.example.com/application/o/pve-helper/
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

Create a dedicated API token. The intended baseline role is `PVEAuditor` on `/`, which is
enough for inventory, storage visibility, and orphan classification.

If Scheduled Tasks power actions are enabled, add the custom `HelperPower` role with
`VM.Audit` and `VM.PowerMgmt` on `/vms` to both the user and the
privilege-separated token. `VM.Audit` is needed for the target picker to list VM/CT
guests, and `VM.PowerMgmt` is needed to start, stop, shutdown, or reboot them.
Proxmox reserves the `PVE*` role namespace, so do not name the custom role `PVE...`.

For the full UI walkthrough, see `docs/proxmox-api-token.md`.

Set:

```env
PVE_ENDPOINTS=https://pve1.example.com:8006
PVE_VERIFY_TLS=true
PVE_API_TOKEN_ID=<token-id>
PVE_API_TOKEN_SECRET=<token-secret>
SCHEDULED_ACTIONS_ENABLED=true
SCHEDULED_ACTION_TIMEOUT_SECONDS=1800
SCHEDULED_ACTION_POLL_INTERVAL_SECONDS=5
SCHEDULED_ACTION_RUN_RETENTION_DAYS=90
```

If Proxmox uses an internal CA, set `PVE_CA_BUNDLE` to a mounted internal-CA path.

Set `SCHEDULED_ACTIONS_ENABLED=false` if you want to disable scheduled VM/CT
power actions at runtime even when the pve-helper token has HelperPower
permissions.
When enabled, `SCHEDULED_ACTION_TIMEOUT_SECONDS` is the max time pve-helper will
wait for a submitted Proxmox task before marking it timed out.

## Storage consumer safety

Before any additional Proxmox node can mount either shared NFS export, add that node to the
matching storage's expected consumers. The orphan classifier must see every expected
consumer in the same scan-run, otherwise files are marked `unknown` / `classification_blocked`
instead of `likely_orphan`.

Initial single-node value:

```yaml
expected_consumers:
  - pve-node-1
```

Future value before further nodes consume the storage:

```yaml
expected_consumers:
  - pve-node-1
  - pve-node-2
  - pve-node-3
```

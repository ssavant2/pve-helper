# pve-helper deployment runbook

> Hostnames, groups, IPs and exports below are generic placeholders. Substitute your own
> environment's values in `.env` (which is gitignored) — do not commit real values.

This document is for deployment and operation of the platform. For day-to-day
use of the administration client, see the [user manual](user-manual.md).

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
   docker compose up -d nginx web worker worker-bulk console
   ```

7. Open `http://dockerhost:21080` directly or configure NPM for `https://pve-helper.example.com`.

Set `APP_BASE_URL` to the browser-facing URL. A direct deployment needs no
certificate:

```env
APP_BASE_URL=http://dockerhost:21080
```

If a separate reverse proxy provides TLS, use its external URL instead:

```env
APP_BASE_URL=https://pve-helper.example.com
```

The scheme controls session and CSRF Secure cookies. pve-helper itself always
serves HTTP and neither provisions nor requires a certificate. HTTPS is strongly
recommended whenever the administration network is not already trusted.

The `nginx` service owns the public app port and proxies normal requests to the
internal Django/Gunicorn `web` service. Authorized datastore downloads are served
directly by nginx from read-only storage mounts after Django has performed auth,
path validation, and audit logging.

### Worker topology and resources

Background work runs as two Django-Q clusters, deployed as separate services:

- **`worker`** — the *control plane*: schedules, retention, trash purge, space
  snapshots, and the stale-task reapers. It is the **only** cluster that runs the
  Django-Q scheduler, so `Schedule` rows fire exactly once.
- **`worker-bulk`** — the *data plane*: scans, inflate, backup/restore, migration,
  OVA/OVF import, and long (up to 6 h) Proxmox UPID polling. It runs with
  `Q_CLUSTER_NAME=bulk` (6 h timeout, scheduler **off**) and only drains jobs that
  the app explicitly routes to the `bulk` queue.

`Q_BULK_WORKERS` (default **2**) is the number of worker **processes** the bulk
cluster forks — not threads, and not a core requirement. Two lets a long
backup/restore poll and quick guest status polls proceed concurrently instead of
serializing. Bulk work is mostly **I/O-bound** — Proxmox performs the actual disk
work while the worker sleeps in `wait_for_task` — so **two bulk workers do not
require two dedicated cores**; a single core time-slices them fine. Extra vCPUs
only help when genuinely CPU-bound bulk jobs overlap (two scans at once, or OVA
SHA hashing during a scan). A 2-core host is comfortable for the whole set
(web + control worker + 2 bulk workers + console).

Always keep `Q_BULK_RETRY` **greater than** `Q_BULK_TIMEOUT`; otherwise Django-Q
retries — and thus double-runs — a job that is still legitimately running.

### Optional external reverse proxy

By default, audit events record the direct peer of pve-helper's nginx sidecar.
That is safe: client-supplied `X-Forwarded-For` values are ignored. If Nginx
Proxy Manager (NPM) is in front of the app and audit events should retain the
browser's IP, explicitly trust *only* NPM's fixed IP address or private subnet:

```env
# Example only — use NPM's actual address or subnet, never 0.0.0.0/0.
NGINX_TRUSTED_PROXY=192.0.2.20
```

In the NPM Proxy Host's Advanced configuration, preserve the normal proxy
headers (these are NPM's usual defaults, but make them explicit if customised):

```nginx
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
```

Recreate the pve-helper `nginx` service after changing `NGINX_TRUSTED_PROXY`
(`docker compose up -d nginx`) — a plain `restart` reuses the existing container
and will not pick up the new value. Setting `NGINX_TRUSTED_PROXY` is the required
step; the header block above is usually already NPM's default. The nginx
sidecar trusts `X-Forwarded-For` and `X-Forwarded-Proto` only when the original
TCP peer matches `NGINX_TRUSTED_PROXY`, then passes the validated client IP and
external scheme to the app. Direct HTTP access remains supported, but a direct
caller cannot turn that request into HTTPS by forging proxy headers. If the
external proxy supplies additional access-control policy, use a host firewall
or network ACL to prevent clients from deliberately bypassing it; that
restriction is deployment-specific rather than an application requirement.

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

Then recreate the web and worker containers so Docker binds the mounted NFS
trees, not the empty underlying directories. `worker-bulk` needs the same
mounts as `worker` because it performs scans and other storage-heavy work:

```bash
docker compose up -d --force-recreate nginx web worker worker-bulk
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

Create a dedicated Proxmox user and API token for pve-helper. The normal
deployment grants the built-in `Administrator` role on `/`, with propagation
enabled, to both:

- user: `pve-helper@pve`
- privilege-separated token: `pve-helper@pve!pve-helper`

The broader role is intentional. pve-helper is becoming an admin tool rather
than a read-only scanner: scheduled power actions, VM/CT creation/deletion,
snapshots, configuration edits, migration, and tag writes should not require
chasing individual Proxmox privileges one by one. Keep destructive app features
behind pve-helper confirmation/audit flows instead of trying to model them as
tiny Proxmox roles.

For a read-only scanner-only deployment, `PVEAuditor` on `/` is still enough for
inventory, storage visibility, and orphan classification. Older `HelperPower`
grants on `/vms` can be removed once the dedicated user and token have
`Administrator` on `/`.

For the full UI walkthrough, see `docs/proxmox-api-token.md`.

Set:

```env
PVE_ENDPOINTS=https://pve1.example.com:8006
PVE_VERIFY_TLS=true
PVE_API_TOKEN_ID=<token-id>
PVE_API_TOKEN_SECRET=<token-secret>
SCHEDULED_ACTIONS_ENABLED=true
SCHEDULED_ACTION_TIMEOUT_SECONDS=1800
BACKUP_TASK_TIMEOUT_SECONDS=21600
SCHEDULED_ACTION_POLL_INTERVAL_SECONDS=5
SCHEDULED_ACTION_RUN_RETENTION_DAYS=90
```

If Proxmox uses an internal CA, set `PVE_CA_BUNDLE` to a mounted internal-CA path.

Set `SCHEDULED_ACTIONS_ENABLED=false` if you want to disable scheduled VM/CT
power actions at runtime even when the pve-helper token has administrator
permissions.
When enabled, `SCHEDULED_ACTION_TIMEOUT_SECONDS` is the max time pve-helper will
wait for a submitted Proxmox task before marking it timed out.
`BACKUP_TASK_TIMEOUT_SECONDS` applies independently to long-running vzdump
backup and restore jobs; the default is six hours.

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

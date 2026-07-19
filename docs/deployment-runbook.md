# pve-helper deployment runbook

> Hostnames, groups, IPs and exports below are generic placeholders. Substitute your own
> environment's values in `.env` (which is gitignored) — do not commit real values.

This document is for deployment and operation of the platform. For day-to-day
use of the administration client, see the [user manual](user-manual.md).

## Pull-only production deployment

The development checkout builds `pve-helper-dev:local`. Production does not need a
checkout or a local build. Pushing a semantic version tag such as
`v0.1.0-alpha.1` starts `.github/workflows/publish-latest.yml`. It audits the
locked dependencies, runs Django, JavaScript and Playwright checks, then
publishes all three images as `latest`, with the version tag and with a
commit-addressed SHA tag. A manual workflow run publishes images but does not
create a GitHub release.

The release workflow attaches a standalone `docker-compose.yml` and
`example.env` to each GitHub release. The Compose file uses three published
runtime images: the app, a Postgres image containing the role-init script and
an nginx image containing the proxy template. The production directory contains
only:

- `docker-compose.yml`, packaged from `docker-compose.production.yml`;
- `.env`; and
- `certs/ca-bundle.pem` (empty when no private CA is needed).

The application version shown in the header is baked into the image by the
release workflow. Local development builds display `DEV`. Set
`PVE_HELPER_VERSION=latest` to follow the newest successful release, or use the
same semantic version/commit-addressed SHA tag for all three images when pinning a
deployment.

The first GHCR publication is private by default. Make the package public in its
GitHub Package settings before an unauthenticated production pull. Subsequent
fix-forward deployments run from the production directory:

```bash
docker compose pull
docker compose up -d --wait db
docker compose stop nginx web console worker worker-bulk
docker compose run --rm --no-deps web python manage.py migrate --noinput
docker compose up -d --remove-orphans --wait
```

The sequence pulls `latest`, starts/waits for Postgres, stops application
services, runs migrations with the newly pulled image and recreates the complete
stack. Pulling `latest` alone never updates an already-running container.

### Platform version requirements

| Component | Minimum | Why |
|---|---|---|
| Proxmox VE | 9.2 on every managed node | pve-helper targets the PVE 9.2 API/HA baseline. The Dynamic Load Balancer — the dynamic Cluster Resource Scheduler (CRS) mode used as the DRS-equivalent in the Hosts & Clusters module — was introduced in 9.2. PVE 8.x is unsupported. |
| Docker Engine | 25.0 | Docker 25 introduced recursive read-only bind-mount support. |
| Docker-host kernel | Linux 5.12 | Kernel 5.12 is the minimum for nested bind mounts to remain recursively read-only instead of exposing a writable submount beneath a read-only parent. |
| Docker Compose | v2 | The distributed deployment is a Compose v2 application. |

Check the baseline before installation or upgrade:

```bash
# On every Proxmox node
pveversion

# On the Docker host
uname -r
docker version --format '{{.Server.Version}}'
docker compose version
```

These are compatibility requirements rather than best-effort recommendations.
Do not onboard a PVE 8.x cluster because the currently implemented inventory
views happen to answer successfully, and do not rely on a pre-5.12 kernel for a
read-only storage boundary around nested or propagated mounts.

### CPU and memory requirements

These figures cover the complete stack — nginx, web, control worker, bulk worker,
console and Postgres — rather than one process inside the application image:

| Deployment | vCPU | RAM | Intended workload |
|---|---:|---:|---|
| Minimum single production stack | 2 | 2 GB | Small environment, normal scans and no overlapping heavy bulk jobs |
| Recommended single production stack | 2 | 4 GB | Normal operation with headroom for scans, backups and inventory growth |
| Concurrent-heavy production | 4 | 8 GB | Several overlapping bulk jobs, image processing or unusually large inventories |
| Production and development on one host | 2–4 | 6–8 GB | Two databases/stacks plus builds, browser tests and development tooling |

The Compose `mem_limit` values are **upper safety bounds**, not reserved memory.
Adding them together does not give the VM's minimum RAM requirement. A normal
small production stack is expected to use roughly 1–1.5 GB in steady operation;
workload, process count and filesystem cache make this variable.

For a development stack running beside production, start with
`GUNICORN_WORKERS=1` and `Q_BULK_WORKERS=1`. Increase them only for a targeted
concurrency test. Production defaults remain two Gunicorn and two bulk workers;
the control worker already defaults to one.

Large uploads do not justify more application RAM: place
`FILE_UPLOAD_TEMP_DIR` on real mounted storage as described under *Storage write
mode*. Container tmpfs is deliberately small and must not buffer datastore-sized
files.

Use `docker stats`, the host's available memory/swap and cgroup
`memory.events`/`memory.peak` to size from observed load. An old kernel OOM line
alone is not a current capacity measurement; correlate it with the lifetime and
cgroup counters of the running container.

## First production run

1. Download `example.env` from the release as `.env`.
2. Fill every required blank value: application and database secrets, external
   URL/host/origin values, the token-encryption keyring and both storage host
   paths. Configure the OIDC placeholders as well when login is enabled. A new
   install leaves the legacy `PVE_*` endpoint/token fields empty.
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

7. Open `http://dockerhost:21080` directly or configure NPM for
   `https://pve-helper.example.com`. Choose **Clusters → Connections → Add
   cluster** and complete the verified onboarding flow. No cluster record or
   credential is saved until transport, permissions and CA identity pass.

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
internal Django/Gunicorn `web` service. Authorized datastore downloads use nginx
only when the mount was captured in nginx's read-only startup manifest; Django
performs auth, live-mount/path validation and audit first, and transparently streams
the file itself when a newly propagated mount is not in that manifest yet.

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

Recommended `/etc/fstab` entries (use `ro` instead of `rw` when the host itself
must enforce read-only access):

```fstab
truenas.example.com:/mnt/tank/proxmox-fs /mnt/pve-helper/truenas-fs nfs4 rw,vers=4.2,proto=tcp,nconnect=4,hard,timeo=600,retrans=2,noatime,_netdev,nofail 0 0
truenas.example.com:/mnt/tank/proxmox-vm /mnt/pve-helper/truenas-vm nfs4 rw,vers=4.2,proto=tcp,nconnect=4,hard,timeo=600,retrans=2,noatime,_netdev,nofail 0 0
```

Apply and verify:

```bash
sudo systemctl daemon-reload
sudo mount /mnt/pve-helper/truenas-fs
sudo mount /mnt/pve-helper/truenas-vm
findmnt -T /mnt/pve-helper/truenas-fs
findmnt -T /mnt/pve-helper/truenas-vm
```

Compose binds the single host root from `PVE_HELPER_STORAGE_ROOT` to `/storages`.
Web, worker and worker-bulk use `rslave`, so a submount added or remounted after
container start is immediately visible without recreating containers. nginx uses
a private recursively read-only snapshot; `STORAGE_WRITE_ENABLED` remains the
operation-level policy gate for the application processes.

The app discovers Proxmox storage definitions from its API catalog independently
of these mounts. After a file-tree submount exists, register it from **Datastores →
Register mount** and select the cluster storage plus shared or node-local scope.
The association is explicit so equal storage IDs in different clusters cannot
silently collide. The legacy `TRUENAS_*` variables are retained only for one-time
upgrade import; leave their storage IDs blank on a new installation.

Definitions/node state and volume content have separate periodic costs. The public
defaults are one and five minutes respectively via
`STORAGE_METADATA_REFRESH_INTERVAL_MINUTES` and
`STORAGE_VOLUME_REFRESH_INTERVAL_MINUTES`. Successful storage-affecting operations
queue an immediate catalog refresh, and a destructive preflight performs a fresh
scoped read, so operation correctness is not delayed by the slower interval.

After the first deployment, verify propagation by mounting a harmless temporary
filesystem beneath the host root. It must appear in web/worker without recreation,
must not enter nginx's private namespace, and nginx's underlying root must remain
read-only. Remove the temporary mount afterwards. Failure means the deployment
must not enable storage writes.

For an existing deployment that changed from individual binds to the generic root,
recreate the services once:

```bash
docker compose up -d --force-recreate nginx web worker worker-bulk
```

The app checks `/proc/self/mountinfo` for NFS/CIFS/CephFS/GlusterFS registrations.
If such a submount disappears, the still-present backing directory is rejected as
`mount unavailable`; the browser and writes never fall through into it. A deliberate
`dir` registration has a separate profile and does not require a submount.

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

2. Set `STORAGE_WRITE_ENABLED=true`. The Compose contract already keeps nginx
   read-only and gives web/workers the access needed for an authorized operation.

`STORAGE_WRITE_ENABLED=false` is the global emergency brake. It hides
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

By default, authorized datastore downloads use `X-Accel-Redirect` when the mount is
listed in nginx's read-only startup manifest:

- Django validates the requested storage/path and writes the audit event.
- The pve-helper nginx sidecar serves the file bytes from a read-only mount.
- The visible download URL stays the same.

A mount added after nginx started uses Django streaming immediately and becomes
accelerated after the next ordinary nginx restart/deployment. This fallback is a
security boundary: Linux/Docker do not force a later `rslave` submount read-only in
an already-running namespace, even with recursive read-only requested.

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

## Secret encryption keyring

Proxmox API tokens are stored per cluster in the database, encrypted at rest. The
key that decrypts them lives only in the environment, in
`PVE_HELPER_ENCRYPTION_KEYS`.

**This is the most load-bearing secret in the deployment.** `.env` is reduced to
the app secret, the database and this keyring, and every cluster credential is
sealed under it. Lose the active key and no cluster credential can be read: the
app refuses to start rather than pretending the clusters are merely unreachable,
and the only way back is re-entering every token by hand.

It is deliberately not `SECRET_KEY`. That key gets rotated for session and signing
reasons, and doing so must never make every cluster credential unreadable.

### Format

```
PVE_HELPER_ENCRYPTION_KEYS=<key-id>:<base64-32-byte-key>[,<key-id>:<base64-32-byte-key>...]
PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID=<key-id>
```

Key ids are lowercase (`a-z`, `0-9`, `-`, `_`) and are stored inside every sealed
value, which is how a read knows which key to use. `PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID`
names the key that seals *new* secrets; it may be omitted only when the keyring
holds exactly one key. Keep old keys in the keyring until nothing is sealed under
them any more.

Generate a key:

```bash
docker compose exec -T web python -c \
  "import base64, os; print(base64.b64encode(os.urandom(32)).decode())"
```

### Custody

- **Back up every key id that any stored ciphertext still names**, not just the
  active one. `rotate_encryption_keys` (below) is what makes an old key
  droppable; until it has run, that key is still required to read data.
- Store the backup somewhere that survives the loss of this host and of `.env`,
  and that is not the same place as a database backup — a backup holding both the
  ciphertext and its key protects nothing.
- Treat a key id as permanent. Reusing an id for different key material makes
  every value sealed under the old one undecryptable *and* unidentifiable.
- A database backup taken today is only restorable while the keys referenced by
  the credentials **in that backup** still exist. Retire keys on the same schedule
  as the backups that depend on them.

### Rotation

Rotation decrypts each credential with the key that sealed it and re-seals it
under the active key. The Proxmox token itself does not change. This is what makes
a compromised key recoverable.

```bash
# 1. Add the new key alongside the old one and make it active, then restart.
#    PVE_HELPER_ENCRYPTION_KEYS=old1:<...>,new1:<...>
#    PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID=new1

# 2. See what would change. This is the default; nothing is written.
docker compose exec -T web python manage.py rotate_encryption_keys

# 3. Apply.
docker compose exec -T web python manage.py rotate_encryption_keys --apply

# 4. Only once this reports nothing pending may the old key be dropped from the
#    keyring — and only after any database backup that still needs it has aged out.
docker compose exec -T web python manage.py rotate_encryption_keys
```

Audit records which cluster was rotated and between which key ids. It never
records secret material.

### Recovering from a missing key

Startup fails with `pve_helper.E010`, naming the key ids that are missing:

```
Stored cluster credentials reference encryption keys that are not in the keyring: <key-id>.
```

This is not a Proxmox outage and must not be treated as one. Two ways back:

1. **Restore the named key** into `PVE_HELPER_ENCRYPTION_KEYS` from backup/escrow
   and restart. Nothing else is needed; the credentials were never damaged.
2. **Re-enter the tokens.** If the key is genuinely gone, the sealed secrets are
   unrecoverable and each cluster's token must be set again:

   ```bash
   docker compose exec -T web sh -c \
     'PVE_HELPER_TOKEN_SECRET="<token secret>" \
      python manage.py set_cluster_credential <cluster-key> "<token-id>"'
   ```

   `set_cluster_credential` and `rotate_encryption_keys` deliberately skip the
   startup checks, so they run in exactly the broken state they repair. Without
   that, the check reporting the problem would block the command fixing it.

The token secret is read from `PVE_HELPER_TOKEN_SECRET` or stdin and never from an
argument: arguments are visible in the process list and in shell history.

### Credential cutover

Existing installations may start with the legacy global `PVE_API_TOKEN_ID` /
`PVE_API_TOKEN_SECRET`. Moving to per-cluster credentials is one explicit step,
because it changes where every provider call gets its identity:

```bash
docker compose exec -T web python manage.py complete_credential_cutover
```

It seals the legacy token into the bootstrap cluster and records a durable marker;
from then on the legacy settings are never read. They are *ignored, not deleted* —
rolling the code back resumes reading them, and re-import is idempotent. **Do not
remove the legacy token from the environment until the identity contract version 1
boundary has succeeded.**

## Authentik

Follow `docs/authentik-oidc-setup.md`.

For a new installation, leave the legacy `PVE_ENDPOINTS`,
`PVE_API_TOKEN_ID`, `PVE_API_TOKEN_SECRET` and `PVE_CA_BUNDLE` fields empty.
Start the application and add each independent cluster through **Clusters →
Connections**. The wizard stores transport trust and an encrypted, write-only
credential per cluster and verifies effective `Administrator` permissions at
`/` before persisting anything.

The following environment block is retained only for a one-time import by an
older/single-cluster deployment:

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

`REQUESTS_CA_BUNDLE` covers the OIDC path (which uses `requests`). Proxmox trust
is separate and cluster-owned: approve public trust or paste that cluster's CA
PEM in **Clusters → Connections**. `PVE_CA_BUNDLE` remains only for the legacy
single-cluster bootstrap/rollback path.

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

For UI onboarding, paste the internal CA PEM in that cluster's trust step. Do not
put a new cluster's CA in the legacy global `PVE_CA_BUNDLE`.

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

An offline expected consumer therefore reports **inventory incomplete**; it does
not make the shared datastore unavailable. File browsing and guest operations on
healthy cluster members continue normally. Only orphan conclusions and risky
file mutations remain conservative until every expected consumer is observed.

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

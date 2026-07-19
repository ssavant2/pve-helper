# pve-helper installation guide

> [!WARNING]
> pve-helper is early alpha software. It has not reached version 1.0 and is not
> production-mature. Use it only where you can tolerate bugs and breaking
> changes, with independent backups and after testing its permissions and
> destructive operations against non-critical infrastructure.

pve-helper is distributed as three prebuilt runtime images plus a standalone
Compose file and environment template. A production installation does not need
Git, Python, Node.js or a source checkout. No development image is published.

## Requirements

- Proxmox VE 9.2 or later on every managed node. Proxmox VE 8.x is not
  supported; pve-helper targets the 9.2 API and HA/Cluster Resource Scheduler
  baseline, including the Dynamic Load Balancer introduced in 9.2.
- A Linux Docker host with kernel 5.12 or later. This is the minimum kernel for
  recursive read-only protection of nested storage bind mounts.
- Docker Engine 25.0 or later with Docker Compose v2. Docker 25 added the
  recursive read-only bind-mount support used with the kernel requirement above.
- 2 vCPU and 2 GB RAM minimum; 2 vCPU and 4 GB RAM recommended
- Network access from the containers to the Proxmox API
- Host mounts for any Proxmox storage that pve-helper should browse
- A base64-encoded 32-byte encryption key, backed up separately from the database
- A Proxmox API token with the permissions described in
  `docs/proxmox-api-token.md`

Treat these as compatibility requirements, not merely recommendations. An older
PVE release may appear to work with today's simpler views while lacking APIs and
CRS behavior required by later modules; an older Docker host may fail to preserve
read-only semantics across nested or propagated storage mounts.

## Install

```bash
mkdir pve-helper
cd pve-helper
curl -fLo docker-compose.yml https://github.com/ssavant2/pve-helper/releases/latest/download/docker-compose.yml
curl -fLo .env https://github.com/ssavant2/pve-helper/releases/latest/download/example.env
mkdir -p certs
touch certs/ca-bundle.pem
```

Edit `.env` before starting. Fill every required blank value, replace the OIDC
placeholders when login is enabled, and configure:

- the public or internal `APP_BASE_URL`, allowed hosts and CSRF origins;
- unique application and database secrets;
- `PVE_HELPER_ENCRYPTION_KEYS` and its active key id;
- the single `PVE_HELPER_STORAGE_ROOT` containing any optional file-tree mounts;
- OIDC values when `APP_REQUIRE_LOGIN=true`.

`PVE_HELPER_NETWORK` and Docker's Compose project name must be unique when more
than one production-style stack runs on the same Docker host.

Generate the token-encryption key with a local cryptographic random source, for
example:

```bash
printf 'PVE_HELPER_ENCRYPTION_KEYS=primary:'
head -c 32 /dev/urandom | base64
```

Put the resulting value and `PVE_HELPER_ENCRYPTION_ACTIVE_KEY_ID=primary` in
`.env`, and keep an independent copy of the key. The production template fails
before container creation when required application/database/encryption secrets
or storage paths are missing. It starts with `DEBUG=false`, login required and
storage writes disabled.

If the Proxmox or OIDC endpoints use a private CA, place a PEM bundle at
`certs/ca-bundle.pem` and set `REQUESTS_CA_BUNDLE` to
`/etc/ssl/pve-helper-ca-bundle.pem`. Otherwise leave the created file empty and
`REQUESTS_CA_BUNDLE` blank.

Validate the rendered deployment, then start it:

```bash
docker compose config --quiet
docker compose up -d --wait db
docker compose run --rm --no-deps web python manage.py migrate --noinput
docker compose up -d --wait
```

Open the configured `APP_BASE_URL`, then choose **Clusters → Connections → Add
cluster**. The wizard inspects the endpoint certificate before sending a token,
verifies effective Administrator permissions and the Proxmox CA identity, and
only then stores the cluster and its encrypted write-only credential.

Proxmox storage definitions are discovered automatically. A host mount is not
required for volume inventory. To add file browsing for an eligible datastore,
mount it as a subdirectory of `PVE_HELPER_STORAGE_ROOT`, then open **Datastores →
Register mount** and explicitly associate that directory with its cluster storage
and node scope. The app never mounts storage or creates the Proxmox definition as
part of registration.

`STORAGE_METADATA_REFRESH_INTERVAL_MINUTES` controls the cheap definitions/node
state cadence (default 1 minute), while
`STORAGE_VOLUME_REFRESH_INTERVAL_MINUTES` controls the more expensive content
inventory (default 5 minutes). Accepted operations refresh affected state directly;
destructive preflight does not rely on the periodic age alone.

pve-helper listens over HTTP. A certificate
is neither provisioned nor required; an external reverse proxy may terminate
HTTPS if your environment needs it.

## Enabling storage writes

The default application policy is read-only (`STORAGE_WRITE_ENABLED=false`). nginx
receives a private, recursively read-only `/storages` snapshot; web and workers
receive the dynamically propagated root read-write so the application can authorize
an operation after checking the specific live mount. A newly added mount uses the
safe streaming download fallback until nginx next restarts. Verify the host mount
and ACLs first. To allow upload, trash and restore operations, create a temporary
upload directory on real writable storage, then set:

```env
STORAGE_WRITE_ENABLED=true
FILE_UPLOAD_TEMP_DIR=/storages/truenas-fs/.pve-helper-upload-tmp
```

See `docs/deployment-runbook.md` for NFS mount guidance, upload temporary-space
requirements, Authentik, reverse proxies and database role separation.

## Update

The following sequence preserves `.env`, certificates and Docker volumes. It
pulls the current images, runs database migrations using the new app image and
replaces the application containers:

```bash
docker compose pull
docker compose up -d --wait db
docker compose stop nginx web console worker worker-bulk
docker compose run --rm --no-deps web python manage.py migrate --noinput
docker compose up -d --remove-orphans --wait
```

When release notes mention Compose changes, download the new
`docker-compose.yml` to a temporary path, compare it with the installed file,
then replace that file before running the update. Never overwrite `.env`,
`certs/` or Docker volumes during an update.

## Development install

Development is source-based and is not published as a separate image:

```bash
git clone https://github.com/ssavant2/pve-helper.git pve-helper-dev
cd pve-helper-dev
cp .env.example .env
# Edit .env for the development environment.
docker compose -f docker-compose.example.yml build
docker compose -f docker-compose.example.yml up -d
```

The development compose project, image, network and volume use the
`pve-helper-dev` prefix and do not share state with production.

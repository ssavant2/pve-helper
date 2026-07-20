# Security policy

pve-helper is early alpha software that administers infrastructure and can
perform destructive operations. Do not expose it directly to the public
internet, use a least-privilege Proxmox token, keep storage mounts read-only
until writes are deliberately enabled, and maintain independent backups.

## Supported versions

Security fixes are provided only in the newest published release and the
`latest` image. Older alpha builds are not maintained. Every release also has a
version tag and a commit-addressed `sha-...` image tag for verification and
incident analysis; record the registry digest when cryptographic immutability is
required.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's private
vulnerability reporting flow:

https://github.com/ssavant2/pve-helper/security/advisories/new

Include the affected version or image digest, deployment topology, impact,
reproduction steps and any relevant logs with credentials and internal
addresses removed. Reports are handled on a best-effort basis; this hobby
project has no response-time or support SLA.

## Development verification

GitHub runs the standard CodeQL suites for Python and JavaScript/TypeScript on
every push and pull request to `main`, and on a weekly schedule. Contributors
can run the same suites before pushing:

```bash
./scripts/codeql_scan.sh
```

The script downloads GitHub's checksum-pinned official CodeQL bundle on its
first run and caches it outside the repository. Analysis runs in the pinned
Playwright development image so the host needs Docker, `curl`, `flock`,
`sha256sum`, and `tar`, but does not need Node.js or CodeQL installed. Set
`CODEQL_CACHE_DIR`, `CODEQL_THREADS`, or `CODEQL_RAM_MB` to override the cache
location or resource limits.

## Scope

The report should concern pve-helper code, its published images or its release
configuration. Vulnerabilities in Proxmox, your OIDC provider, Docker, an external
reverse proxy, a storage appliance or the underlying operating system should be
reported to the corresponding project or vendor.

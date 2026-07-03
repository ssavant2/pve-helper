# Proxmox API token setup

`pve-helper` started as a read-mostly inventory/storage helper, but the planned
VM module needs normal administrator operations: snapshots, VM/CT creation,
configuration changes, deletion, migration, guest-agent reads, and scheduled
power actions.

Use a dedicated Proxmox user and API token with the built-in `Administrator`
role on `/`. Keep token privilege separation enabled and grant the same role to
both the user and the token.

## Values

Suggested values:

| Field | Value |
| --- | --- |
| User | `pve-helper@pve` |
| Token ID | `pve-helper` |
| Full token id | `pve-helper@pve!pve-helper` |
| Role | `Administrator` |
| Path | `/` |
| Propagate | enabled |

For a single-node install, create this on that node. If the nodes are later
clustered, the user/token and permissions live in the cluster configuration. If
the nodes are separate, repeat the setup on every node that `pve-helper` will
inventory.

## Steps

1. Log in to the Proxmox web UI as an administrator.

2. Create a dedicated user:

   - Go to `Datacenter` -> `Permissions` -> `Users`.
   - Click `Add`.
   - User name: `pve-helper`
   - Realm: `Proxmox VE authentication server`
   - Enabled: yes.
   - Expire: never, unless you intentionally want a rotation date.
   - Set a long random password if Proxmox asks for one. The app will not use
     the password.

3. Grant the user administrator permissions:

   - Go to `Datacenter` -> `Permissions`.
   - Click `Add` -> `User Permission`.
   - Path: `/`
   - User: `pve-helper@pve`
   - Role: `Administrator`
   - Propagate: enabled.

4. Create the API token:

   - Go to `Datacenter` -> `Permissions` -> `API Tokens`.
   - Click `Add`.
   - User: `pve-helper@pve`
   - Token ID: `pve-helper`
   - Expire: never, unless you intentionally want a rotation date.
   - Privilege Separation: enabled.
   - Click `Add`.
   - Copy the token secret immediately. Proxmox only shows it once.

5. Grant the token administrator permissions:

   With privilege separation enabled, the token needs its own ACL entry. The
   token cannot exceed the owning user's permissions, so grant both the user and
   the token the same role.

   - Go to `Datacenter` -> `Permissions`.
   - Click `Add` -> `API Token Permission`.
   - Path: `/`
   - API Token: `pve-helper@pve!pve-helper`
   - Role: `Administrator`
   - Propagate: enabled.

6. Verify effective permissions:

   ```bash
   pveum user permissions pve-helper@pve --path /
   pveum user token permissions pve-helper@pve pve-helper --path /
   ```

7. Add the values to `.env`:

   ```env
   PVE_API_TOKEN_ID=pve-helper@pve!pve-helper
   PVE_API_TOKEN_SECRET=<secret-shown-once-by-proxmox>
   PVE_ENDPOINTS=https://pve-node-1.example.com:8006
   PVE_VERIFY_TLS=true
   ```

8. Restart the app containers:

   ```bash
   docker compose up -d
   ```

9. Test from the Docker host:

   ```bash
   curl -fsS \
     -H "Authorization: PVEAPIToken=pve-helper@pve!pve-helper=<secret>" \
     https://pve-node-1.example.com:8006/api2/json/nodes
   ```

   If Proxmox uses an internal CA that the host does not trust yet, fix CA trust
   instead of setting `PVE_VERIFY_TLS=false` for normal use.

## Required Access

The current read-only scanner uses these API areas:

- `/api2/json/nodes`
- `/api2/json/nodes/{node}/qemu`
- `/api2/json/nodes/{node}/qemu/{vmid}/config`
- `/api2/json/nodes/{node}/lxc`
- `/api2/json/nodes/{node}/lxc/{vmid}/config`
- `/api2/json/nodes/{node}/storage`

For a deliberately read-only scanner deployment, `PVEAuditor` on `/` is enough
because it includes the audit-style read permissions needed for VM, node, and
datastore inventory.

The normal pve-helper deployment should use `Administrator` on `/` for both the
dedicated user and the privilege-separated token. This avoids chasing individual
permissions as the app adds admin workflows such as snapshots, VM creation,
VM/CT deletion, configuration edits, migration, and tag writes.

Older deployments may still have a custom `HelperPower` role on `/vms` plus
`PVEAuditor` on `/`. That was sufficient for scheduled power actions only. Once
`Administrator` is assigned on `/`, those piecemeal grants can be removed to
keep the ACL list readable.

## References

- Proxmox API token docs: https://pve.proxmox.com/pve-docs/pveum-plain.html
- Proxmox API overview: https://pve.proxmox.com/wiki/Proxmox_VE_API
- Proxmox API viewer: https://pve.proxmox.com/pve-docs/api-viewer/

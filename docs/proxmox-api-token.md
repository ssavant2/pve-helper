# Proxmox API token setup

`pve-helper` needs read access for inventory and storage/orphan classification.
Use a dedicated API token with the built-in `PVEAuditor` role as the baseline.

Scheduled VM/CT power actions are optional and require an extra role with
`VM.PowerMgmt` on `/vms`. Keep privilege separation enabled and grant the same
extra role to both the user and the token.

## Values

Suggested values:

| Field | Value |
| --- | --- |
| User | `pve-helper@pve` |
| Token ID | `pve-helper` |
| Full token id | `pve-helper@pve!pve-helper` |
| Baseline role | `PVEAuditor` |
| Baseline path | `/` |
| Optional power role | `HelperPower` |
| Optional power path | `/vms` |

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

3. Grant the user read-only permissions:

   - Go to `Datacenter` -> `Permissions`.
   - Click `Add` -> `User Permission`.
   - Path: `/`
   - User: `pve-helper@pve`
   - Role: `PVEAuditor`
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

5. Grant the token read-only permissions:

   With privilege separation enabled, the token needs its own ACL entry. The
   token cannot exceed the owning user's permissions, so grant both the user and
   the token the same read-only role.

   - Go to `Datacenter` -> `Permissions`.
   - Click `Add` -> `API Token Permission`.
   - Path: `/`
   - API Token: `pve-helper@pve!pve-helper`
   - Role: `PVEAuditor`
   - Propagate: enabled.

6. Optional: grant VM/CT power permissions for Scheduled Tasks:

   Proxmox reserves the `PVE*` role namespace for built-in roles, so use a
   project role name such as `HelperPower`.

   CLI:

   ```bash
   pveum role add HelperPower --privs "VM.PowerMgmt"
   pveum acl modify /vms -user pve-helper@pve -role HelperPower -propagate 1
   pveum acl modify /vms -token 'pve-helper@pve!pve-helper' -role HelperPower -propagate 1
   ```

   GUI:

   - Go to `Datacenter` -> `Permissions` -> `Roles`.
   - Click `Add`.
   - Role name: `HelperPower`.
   - Privileges: `VM.PowerMgmt`.
   - Go to `Datacenter` -> `Permissions`.
   - Add a `User Permission` on `/vms` for `pve-helper@pve` with role
     `HelperPower`, with propagation enabled.
   - Add an `API Token Permission` on `/vms` for
     `pve-helper@pve!pve-helper` with role `HelperPower`, with propagation
     enabled.

   Verify:

   ```bash
   pveum user permissions pve-helper@pve | grep -E 'VM.PowerMgmt|/vms'
   pveum user token permissions pve-helper@pve pve-helper | grep -E 'VM.PowerMgmt|/vms'
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

`PVEAuditor` is the intended starting role because it includes the audit-style
read permissions needed for VM, node, and datastore inventory.

The optional Scheduled Tasks power actions require `VM.PowerMgmt` on `/vms` for
the same user and token. That is intentionally narrower than `PVEVMAdmin`.

## References

- Proxmox API token docs: https://pve.proxmox.com/pve-docs/pveum-plain.html
- Proxmox API overview: https://pve.proxmox.com/wiki/Proxmox_VE_API
- Proxmox API viewer: https://pve.proxmox.com/pve-docs/api-viewer/

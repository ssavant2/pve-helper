# Proxmox API token setup

Every pve-helper connection authenticates to Proxmox as a dedicated user's
privilege-separated API token. This page is the provider-side half: what to
create on the Proxmox host, and what the app checks before it will store
anything.

The pve-helper side is **Hosts & Clusters → Connections → Add host/cluster** in the
running app. Nothing here goes into `.env`: the wizard stores transport trust and
an encrypted, write-only credential per connection. (An older single-cluster
deployment may still carry `PVE_API_TOKEN_ID`/`PVE_API_TOKEN_SECRET` in its
environment — see *Credential cutover* in the deployment runbook. New
installations leave those empty.)

## Values

| Field | Value |
| --- | --- |
| User | `pve-helper@pve` |
| Token ID | `pve-helper` |
| Full token id | `pve-helper@pve!pve-helper` |
| Role | `Administrator` |
| Path | `/` |
| Propagate | enabled |
| Privilege separation | enabled |

In a cluster, users, tokens and ACLs live in the replicated cluster
configuration, so this is created **once** for the whole cluster, on any node.
Standalone hosts each need their own.

## Create it

From a shell on the node:

```bash
pveum user add pve-helper@pve --comment "pve-helper integration"
pveum acl modify / --users pve-helper@pve --roles Administrator --propagate 1
pveum user token add pve-helper@pve pve-helper --privsep 1
pveum acl modify / --tokens 'pve-helper@pve!pve-helper' --roles Administrator --propagate 1
```

`pveum user token add` prints the secret **once**. If it is not captured, delete
the token and create it again; there is no way to read it back.

Three details decide whether this works:

- **Privilege separation means the token needs its own ACL entry.** That is the
  fourth command. Without it the token inherits nothing, and the failure appears
  as a permissions error during verification even though the *user* is an
  administrator.
- **Propagate on `/`.** The app reads `/nodes/...`, `/storage/...` and more; a
  non-propagating grant on the root gives it the root and nothing beneath it.
- **A token can never exceed its owning user's permissions.** That is why both
  ACL commands grant the same role — raising only the token achieves nothing.

The same thing through the web UI: `Datacenter → Permissions → Users → Add`, then
`Datacenter → Permissions → Add → User Permission` (path `/`, role
`Administrator`, propagate on), then `Datacenter → Permissions → API Tokens →
Add` (privilege separation on, copy the secret immediately), then `Datacenter →
Permissions → Add → API Token Permission` with the same path, role and
propagation.

Confirm before leaving the node:

```bash
pveum user permissions pve-helper@pve --path /
pveum user token permissions pve-helper@pve pve-helper --path /
```

## What pve-helper verifies

Onboarding persists nothing until all of the following pass, so a failure message
points at exactly one of them:

| Read | Requirement |
| --- | --- |
| `version` | **Proxmox VE 9.2 or later.** Older releases are refused outright. |
| `nodes` | At least one visible node. |
| `access/permissions` and `access/roles/Administrator` | Effective privileges at `/` must cover *every* privilege the `Administrator` role holds. |
| `nodes/{node}/certificates/info` | The root CA's subject must contain a UUID. |
| `cluster/status` | Read for the cluster name. Empty for a standalone host, which is fine. |

Two of these surprise people:

- **The permission check compares privilege sets, not role names.** A custom role
  containing everything `Administrator` contains passes; a role that merely looks
  administrative does not, and the error names the missing privileges.
- **The CA UUID is a hard requirement.** A Proxmox-generated cluster CA always
  carries it in the subject's `OU=`. If the root CA has been replaced with a
  corporate one that has no UUID, onboarding fails even when everything else is
  correct — that UUID is the identity pve-helper pins the connection to, and
  changing it later requires an explicit operator re-approval.

Certificate inspection happens **before** credentials are sent: the wizard shows
the endpoint certificate for approval, and only the following step transmits the
token. Trust is per connection — approve public trust or paste that cluster's CA
PEM. Do not put a new cluster's CA in the legacy global `PVE_CA_BUNDLE`.

**Public trust only works if `pveproxy` presents a publicly signed certificate.**
A default Proxmox install signs it with the cluster's own CA, so the verification
step fails with `unable to get local issuer certificate` — a transport failure,
raised before the token is ever checked, so it is not a permissions problem no
matter how much it looks like one. Paste the CA instead. On any node of that
cluster:

```bash
cat /etc/pve/pve-root-ca.pem
```

Copy the whole block including the `-----BEGIN CERTIFICATE-----` and
`-----END CERTIFICATE-----` lines into the wizard's CA field. A standalone host
has its own CA and needs its own paste; it is not the same certificate as any
cluster already added.

## After it is added

The connection collects its first inventory immediately, visible in Recent Tasks
as **Add host/cluster to inventory**. Datastores, tags and guests appear when it
finishes.

Add the cluster's remaining nodes afterwards, one at a time, with **Add endpoint**
on the connection page. They are redundant transports to the same control plane
and share the one credential; each is verified against the pinned identity before
it joins failover.

## Rotation and revocation

Rotate by entering a complete replacement token on the connection detail page; it
is verified before the stored credential is replaced. To withdraw pve-helper's
access, disable the cluster and then remove the stored credential — deleting the
token in Proxmox is a separate provider-side action and is what actually revokes
it.

Retiring or deleting a connection does not touch Proxmox either. The token
outlives it and must be revoked on the Proxmox side.

## Why `Administrator`

pve-helper is an administration tool rather than a read-only scanner: scheduled
power actions, VM/CT creation and deletion, snapshots, configuration edits,
migration, backup/restore and tag writes are all normal use. Modelling those as a
handful of narrow Proxmox roles means revisiting the ACL every time a feature
lands, so the destructive operations are gated by pve-helper's own confirmation
and audit flows instead.

A deliberately read-only deployment can use `PVEAuditor` on `/` — enough for
inventory, storage visibility and orphan classification, and nothing else. Older
installations may still carry a custom `HelperPower` role on `/vms` alongside
`PVEAuditor`; once `Administrator` is granted on `/`, those piecemeal grants can
be removed.

## References

- Proxmox user management: https://pve.proxmox.com/pve-docs/pveum-plain.html
- Proxmox API overview: https://pve.proxmox.com/wiki/Proxmox_VE_API
- Proxmox API viewer: https://pve.proxmox.com/pve-docs/api-viewer/

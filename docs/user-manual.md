# pve-helper user manual

`pve-helper` is a daily-use Proxmox administration client for a small
infrastructure team or homelab. It is designed for operators who already know
vSphere-style administration, the Proxmox VE object model, and basic Linux
storage/networking concepts. This is not a beginner's guide to Proxmox, NFS,
virtual machines, or containers.

## Product scope and ambition

pve-helper aims to cover roughly 90–95% of normal daily Proxmox administration:
guest lifecycle and configuration, storage work, backups/restores, migration,
console access, scheduled power operations, and operational auditability. Its
intended audience is administrators who have moved from vSphere to Proxmox and
want a coherent administration experience without relying on the native Proxmox
GUI for every routine task.

It does not aim for complete feature parity with either vSphere or Proxmox VE.
The priority is the commonly used operational surface, implemented with clear
preflight checks, confirmations, background task tracking, and audit history.
The 90–95% target is an ambition, not a claim of current feature completeness.

Use the native Proxmox GUI for advanced platform administration, rare workflows,
and highly specific features that pve-helper does not expose. That is an
intentional boundary, not a workaround.

All authenticated users are infrastructure administrators. Use the app with the
same care as the Proxmox VE UI: actions can power off guests, alter their
configuration, move data, create backups, and change storage definitions.

For installation, identity-provider setup, proxy configuration, storage mounts,
and service operation, use the [deployment runbook](deployment-runbook.md).
For the Proxmox API identity and privileges, use
[Proxmox API token setup](proxmox-api-token.md).

## What pve-helper owns

Proxmox remains the source of truth for infrastructure state: guests, nodes,
storage definitions, running tasks, and permissions. pve-helper keeps its own
Postgres-backed operational data: storage scans, classifications, scheduled task
runs, audit events, task history, and a small number of enriched read models.

This distinction explains several UI behaviours:

- **Live** status is queried from Proxmox and can be temporarily unavailable.
- **Guest and tag inventory** use a current-state projection refreshed from
  Proxmox. Partial endpoint failures preserve previously known objects and are
  treated as degraded coverage rather than proof that an object disappeared.
- **Storage inventory** and file classifications come from retained completed
  scans; check the displayed scan timestamp before acting on a file result.
- A long-running write is submitted to a background worker. Its progress and
  final state appear in **Recent Tasks** rather than being held open in the
  browser request.
- Mounted file-based storage and API-only storage have different capabilities.
  Do not expect a file browser on block-backed or unmounted storage.

## Start here

The sidebar is the primary navigation. Its five working areas are:

| Area | Use it for |
| --- | --- |
| **VMs/CTs** | Guest inventory, power, console, configuration, migration, backup/restore, and related operations. |
| **Storage** | Mounted shared datastores, API-only local/block storage, scans, file operations, and orphan review. |
| **Tags** | Create and color tags, inspect membership, assign or remove tags, and rename or delete them across guests. |
| **Scheduled Tasks** | One-time and recurring guest power schedules, their runs, and history. |
| **Audit** | Authentication and administration history, filters, search, and export. |

**Clusters** and **Network** remain reserved for later modules. They are not
active administration surfaces in the current release.

The top bar provides global search, theme selection, VM/CT ID visibility, and
IPv4/IPv6 display preferences. Preferences are browser-local. The task bar at
the bottom of every page is **Recent Tasks**; leave it visible while performing
writes.

## Recent Tasks and audit trail

Every submitted guest operation, storage action, scan, import, scheduled run,
and relevant failure is recorded. Use the two views for different questions:

- **Recent Tasks** answers “what is happening now?” It shows queueing,
  progress, completion, failures, selected task cancellation, and force-stop
  follow-up where a graceful shutdown timed out.
- **Audit** answers “what happened and who did it?” It is the durable event log
  for logins, changes, scans, and file actions.

Do not treat a browser redirect or a queued banner as completion. For any
background operation, wait for its terminal Recent Tasks row and inspect a
failure before retrying. Retrying a still-running backup, import, migration, or
inflate can create conflicting work.

Audit supports module and text filters, a date range, and optional technical
fields. CSV and JSON exports stream all matching rows. Excel exports are limited
to 5,000 events; narrow the filters or use CSV/JSON for larger exports.

## Working with VMs and containers

### Find a guest

Use either VMs/CTs surface:

- **Overview** is the broad table for status, filtering, sorting, selection, and
  bulk operations.
- **Inventory** is the persistent guest list and detail workspace. Select a
  guest to work through its tabs.

Guest identity is node-qualified. When names or VMIDs are ambiguous, verify the
node shown in the guest label before taking an action. Linked-clone ancestry,
locks, and live status are also shown where available.

### Use the guest workspace

The guest tabs expose the normal daily administration surface. Exact tabs depend
on VM versus container and on what Proxmox returns.

- **Summary**: power actions, current configuration, resource overview, tags,
  notes, and quick navigation.
- **Console**: browser-integrated graphical VM console or container terminal.
  Start the guest first if the page says it is not running. Use the console for
  guest interaction, not as a replacement for reviewing the task/audit result of
  a power action.
- **Configure** and **Hardware**: identity, boot/options, CPU/memory, disks,
  NICs, and device-related edits. The app refreshes the relevant guest data
  after a successful write; still verify complex changes in Proxmox when they
  affect production workloads.
- **Datastores**, **Networks**, **Monitor**, **Permissions**, and **Agent**:
  inspect the guest's storage references, network mapping, read models, access,
  and guest-agent information.
- **Snapshots**, **Backup**, **Replication**, **Firewall**, and **Cloud-Init**:
  manage the corresponding Proxmox feature when it applies to the guest.

Power, configuration, snapshot, clone, migration, backup, restore, import, and
destructive actions use preflight checks and confirmations. Read the confirmation
text: it identifies the target and, where relevant, the operational consequence.
If a preflight blocks an action, resolve the reported lock, storage, CPU,
network, state, or permission condition rather than bypassing it through a
second attempt.

### Create, clone, migrate, import, and restore

The VMs/CTs workspace provides actions for new VMs/containers, clone/template
flows, migration, registering existing disks, OVA/OVF import, and restoring
backups. These are deliberately separate workflows because their source data and
preflight requirements differ.

Before a data-moving operation:

1. Confirm the destination node and storage have the intended capacity and
   access.
2. Check whether the source is a linked clone, uses local storage, has a lock,
   or has passthrough/device constraints.
3. For imports, inspect the selected archive/descriptor and target VMID before
   confirming.
4. Track the operation in Recent Tasks until it reaches a terminal state.

Long operations run on the bulk worker. A browser disconnect does not cancel a
successfully queued operation. Use task cancellation only where the task bar
explicitly offers it; otherwise let the underlying Proxmox task settle and
inspect the resulting failure or completion.

### Backups, restores, and consoles

Backup and restore operations can legitimately take hours. Do not submit the
same operation again just because the UI has not refreshed. The configured
timeout is six hours for the long-running bulk queue, with a later reconciliation
step for work whose worker was interrupted.

The console session is short-lived and one-time. pve-helper does not expose the
Proxmox API token to the browser. If console connection fails, first check guest
state, Proxmox console availability, and Recent Tasks/audit entries; then use the
native Proxmox UI for platform-level console diagnosis if needed.

## Working with storage

### Know the storage type first

pve-helper distinguishes two storage access models:

| Storage type | What pve-helper can do |
| --- | --- |
| **Mounted shared datastore** | Scan files, browse folders, classify entries, download/upload, create folders, rename, move, use the app-managed trash, inspect permissions, and work with disk images where supported. |
| **API-only datastore** | Read Proxmox-provided summary, monitor, volumes, guest references, configuration, and content types. It has no host-mounted filesystem, so file browsing and file operations are not applicable. |

Mounted storage is only as writable as its effective Docker bind mount. A green
app-level write setting does not override a read-only NFS mount. Conversely,
`STORAGE_WRITE_ENABLED=false` is a global operational brake that hides/rejects
storage writes even when a mount is writable.

### Scan before judging files

Storage classification is conservative. Start a scan from a datastore page or
the Storage overview whenever the inventory is stale or after a relevant Proxmox
change. A scan compares storage files with the Proxmox configuration it can see.

Interpret classifications carefully:

- **In use / referenced**: a scanned configuration references the entry.
- **Likely orphan**: no scanned expected consumer references the entry; this is
  the only classification eligible for the app's trash workflow.
- **Unknown / blocked**: the scan cannot safely determine ownership. Common
  causes are an unavailable expected consumer, incomplete inventory, or an
  unsupported reference. Do not delete or trash the entry based on this result.

Before adding a Proxmox node that can consume shared storage, ensure it is listed
as an expected consumer in the deployment configuration and run a fresh scan.
Otherwise pve-helper intentionally blocks orphan classification instead of
guessing.

### Files and destructive file actions

Use the **Files** tab only on mounted datastores. It is a server-side, paginated
browser; search is limited to the current folder. Download authorization and
auditing happen in Django, while large file bytes are normally served by the
internal nginx sidecar.

For file changes:

- Verify the datastore, folder, filename, and classification before confirming.
- Use **Move to trash** instead of permanent deletion when available. The app
  moves eligible files to `.trash/pve-helper` on the same storage, allowing a
  controlled restore path.
- Restore, rename, move, upload, and inflate can have downstream Proxmox
  consequences. Refresh the scan after a material change.
- Do not use pve-helper to manipulate files that are merely “unknown”; resolve
  their ownership through Proxmox or the storage platform first.

The **Content Types** tab controls the Proxmox storage definition's allowed
content types. It does not list the files stored on the datastore. Actual
mounted objects are on **Files**; API-only volumes are on **Volumes**.

### Monitor, permissions, nodes, and guest references

The remaining datastore tabs answer different operational questions:

- **Summary**: capacity, access, scan state, and high-level inventory.
- **Monitor**: historical space snapshots and recent scan/file activity.
- **Configuration**: the storage definition visible to pve-helper.
- **Permissions**: filesystem ACL/permission information for mounted storage.
- **Nodes** and **VMs/CTs**: expected consumers and current guest references.

Use these tabs before changing a storage definition or treating an apparently
unused file as safe to remove.

## Orphan Finder

**Orphan Finder** is a cross-datastore review queue for entries currently
classified as likely orphan. It is not a deletion queue and never makes a
classification certain by itself.

For each candidate, confirm the latest scan is current, inspect the storage/path
and any available image metadata, then verify in Proxmox that no intended guest,
template, backup, or external process owns it. If any expected consumer was
unavailable, treat the result as incomplete and scan again later.

## Scheduled Tasks

Scheduled Tasks currently supports single-guest power actions: start, graceful
shutdown, hard stop, and reboot. Definitions can be one-time or recurring,
including monthly date and weekday patterns.

When creating or editing a schedule:

1. Confirm the node-qualified guest target and the action's consequence.
2. Check the timezone shown by the app/deployment and the next run preview.
3. Decide explicitly whether a missed occurrence may catch up.
4. Use **Run now** only when an immediate queued execution is intended.

The control worker is the only scheduler, so a schedule fires once even while
bulk backup/import/scan work is running. Run history is retained independently
of the definition. Deleting a definition is a soft delete and is refused while
one of its runs is in flight.

## Tags

**Tags** is the central registry and membership view. It appears below Network
in the main navigation. Create lowercase tags with an optional color before
assigning them, open any tag to see its VMs/CTs/templates, and use the existing
guest or overview controls to assign tags.

Rename and **Delete tag** run through the bulk worker because they may touch
many guests. The confirmation shows the affected count; Recent Tasks and Audit
show partial failures. A safely retryable row says **Failed — right-click for
options**; inspect its details, then right-click it and choose **Retry Task...**.
Already completed objects are not changed again.

**Refresh tag inventory** queues a read-only background reconciliation of both
the Proxmox tag registry/colors and guest membership. Follow it in Recent Tasks;
**Completed with warnings** means usable data was refreshed while one component
or endpoint was unavailable. Data from unavailable endpoints is preserved, and
the membership **As of** time advances only when membership was actually read.

An optional read-only `/api/v1` integration can expose tag membership to a
backup reconciliation script. It is disabled by default and requires a
dedicated bearer token issued with `issue_integration_token`; see the deployment
runbook before enabling it.

## Operational guardrails

- Confirmations, audits, preflight checks, and Recent Tasks are guardrails; they
  do not replace change control or backup policy.
- Treat live Proxmox data as authoritative over an older scan/read model.
- When an external reverse proxy is used, configure its trusted peer address to
  preserve source-IP and HTTPS integrity; direct HTTP deployment remains
  supported. See the deployment runbook before changing proxy headers.
- Use the native Proxmox UI for rare platform settings or features not exposed
  here. pve-helper intentionally targets daily administration, not full Proxmox
  feature parity.
- If Proxmox, the database, a worker, or a storage mount is unavailable, stop
  retrying destructive actions and establish which dependency failed first.

## Useful troubleshooting sequence

1. Read the latest **Recent Tasks** row and its details.
2. Check **Audit** for the corresponding request and initiator.
3. For storage work, confirm mount access and run a fresh scan.
4. For guest work, confirm live guest/node state in pve-helper or Proxmox.
5. If the issue is service-level, use the deployment runbook and the health
   endpoints (`/healthz/live` and `/healthz/ready`) rather than modifying the
   database directly.

Do not manually edit pve-helper audit, scan, schedule, or task rows to make the
UI look healthy. Correct the underlying Proxmox/storage condition, then let the
normal refresh or reconciliation workflow update the application state.

## Trademarks and affiliation

`VMware` and `vSphere` are used only to identify a third-party platform and its
administration model. They are trademarks of their respective owners.
`pve-helper` is an independent project and is not affiliated with, endorsed by,
or sponsored by Broadcom or VMware.

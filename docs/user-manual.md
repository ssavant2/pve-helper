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

- **Guest runtime and tag inventory** use a current-state projection refreshed
  from Proxmox by the worker. Passive pages do not wait for a broad Proxmox
  status request. Partial endpoint failures preserve previously known objects
  and are treated as degraded coverage rather than proof that an object
  disappeared.
- A successful power, configuration, hardware, or tag operation refreshes its
  affected guest immediately when the provider operation completes. It does not
  wait for the next periodic cluster refresh. The UI labels missing or stale
  runtime inventory rather than presenting it as fresh.
- **Storage inventory** and file classifications come from retained completed
  scans; check the displayed scan timestamp before acting on a file result.
- A long-running write is submitted to a background worker. Its progress and
  final state appear in **Recent Tasks** rather than being held open in the
  browser request.
- Mounted file-based storage and API-only storage have different capabilities.
  Do not expect a file browser on block-backed or unmounted storage.

## Start here

The sidebar is the primary navigation. Its working areas are:

| Area | Use it for |
| --- | --- |
| **Clusters → Connections** | Add verified clusters/endpoints and manage per-cluster credentials and enabled state. |
| **VMs/CTs** | Guest inventory, power, console, configuration, migration, backup/restore, and related operations. |
| **Storage** | The Proxmox storage catalog per cluster — one page per datastore, with its nodes, volumes, guests, and (where pve-helper has a mount) its files, scans and file operations. |
| **Tags** | Create and color tags, inspect membership, assign or remove tags, and rename or delete them across guests. |
| **Scheduled Tasks** | One-time and recurring guest power schedules, their runs, and history. |
| **Audit** | Authentication and administration history, filters, search, export, and a shortcut to log forwarding. |
| **PVE-helper Settings** | Application-specific integration settings, including log forwarding and host-mounted storage access. |

**Network** remains reserved for a later module. Cluster **Connections** is the
configuration surface; the broader host/cluster operations workspace arrives in
a later module.

The top bar provides global search, theme selection, VM/CT ID visibility, and
IPv4/IPv6 display preferences. Preferences are browser-local. The task bar at
the bottom of every page is **Recent Tasks**; leave it visible while performing
writes.

When more than one Proxmox cluster is configured, aggregated pages such as VM
Overview, Search, Audit and Recent Tasks show or filter by cluster. Tags is
cluster-specific: its selector navigates to that cluster's Tags URL. Cluster
selection is never a hidden browser/session setting, so confirm the cluster
shown by the object or page before submitting a write.

## Cluster connections

A standalone Proxmox node and a multi-node Proxmox cluster are both represented
as one pve-helper cluster. A cluster has one permanent lowercase key and one or
more replaceable API endpoints. The key is durable identity used in URLs, tasks
and Audit; it cannot be renamed. The display name can be changed.

To add a standalone host or a cluster, open **Clusters → Connections →
Add host/cluster**. A multi-node cluster is registered through one of its nodes;
its remaining nodes are added afterwards, one at a time, as extra endpoints of
the same connection. The steps are:

1. Enter its display name, permanent key and first HTTPS Proxmox endpoint.
2. Review the certificate shown before entering credentials. Choose public trust
   or paste the internal CA PEM used to verify this cluster.
3. Enter an `Administrator` API token and explicitly bind the chosen key to the
   Proxmox CA UUID/fingerprint returned by the verified endpoint.

Adding the connection starts its first inventory immediately, as an **Add
host/cluster to inventory** row in Recent Tasks. It reads the cluster's guests,
storage catalog and tag registry in that order, so the new connection's
datastores, tags and guests appear shortly after the connection itself rather
than at the next periodic refresh. It finishes as *Completed with warnings* if a
node did not answer, and the row names that node; the periodic refreshes and the
per-datastore **Refresh** button pick up whatever it missed.

The token secret is write-only: it is encrypted in the database and is never
shown again or written to Audit. Rotate it by entering a complete replacement on
the connection detail page. To revoke it from pve-helper, disable the cluster
first and then remove the stored credential; revoke/delete the token in Proxmox
as a separate provider-side action.

Add another node of the same Proxmox cluster with **Add endpoint**. Certificate,
credential and pinned CA identity are verified before it joins failover. A
disabled endpoint is re-verified before it can be enabled again.

Disabling a cluster blocks new refreshes, schedules, consoles and writes while
retaining its last-known inventory, schedules and Audit history. It is refused
while provider work is active — including a running scan, because a scan reads
every enabled cluster. Re-enabling verifies the stored trust, credential and
cluster identity first. A CA-identity mismatch quarantines ingestion until an
operator has independently verified the intended cluster and explicitly
re-approved the new identity.

An added cluster's Proxmox storage definitions and node state appear in
the **Storage catalog** section under **Storage → Overview** after catalog refresh.
This API inventory requires no host mount.
If a file-tree datastore should also be browsable, mount it beneath the deployment's
`/storages` host root and use **PVE-helper Settings → Storage access** to bind that
existing directory to the correct cluster storage and, for node-local storage,
node. Registration does not create or edit a Proxmox storage definition.

### Retiring or deleting a connection

Removing a cluster from pve-helper is local: it never deletes Proxmox guests,
storage, tokens or the Proxmox cluster itself. The connection detail page offers
three removal outcomes in its danger zone, and which ones appear depends on the
cluster's state.

- **Retire cluster** (verified) is the normal removal. Disable the cluster first,
  then retire it: pve-helper verifies one selected endpoint's pinned Proxmox CA
  identity, removes the stored credential, trust record and endpoints, stops future
  schedules and clears current inventory — while preserving the permanent key, all
  Audit history, scan evidence and past run history. The retired connection moves to
  the **Retired hosts & clusters** archive with a `Verified` badge and a read-only detail
  page linking to its filtered Audit history.
- **Force retire** is for a cluster that is permanently gone and will never answer
  again. It makes no provider call, disables and retires in one step even when an
  ordinary disable is refused by stuck provider work, and requires you to type the
  exact permanent key and a reason asserting the site is permanently unavailable.
  It carries a `Forced` badge. Because there is no provider confirmation, its Audit
  wording records abandoned work as "abandoned without confirmation", never
  "cancelled".
- **Delete unused connection** appears **only** for a connection that has never
  acquired any operational or inventory history — a fresh onboarding mistake. It is
  the only action that physically deletes the cluster row and releases its permanent
  key and CA identity for reuse. It requires typing the exact key and confirms twice.
  Any scan, guest, storage, console or scheduled-action history hides this control;
  retire the connection instead to preserve that history.

Retirement releases the pinned Proxmox CA UUID in both retirement modes, so the
same physical cluster can be re-onboarded later under a **new** permanent key. A
retired cluster's original key stays permanently reserved so its history keeps its
meaning; a replacement may reuse the display name but not the key. Only a
deleted-unused connection frees its exact key for re-entry. In all cases the
Proxmox API token still exists on the Proxmox side and should be revoked there
separately when the site is reachable.

## Recent Tasks and audit trail

Every submitted guest operation, storage action, scan, import, scheduled run,
and relevant failure is recorded. Use the two views for different questions:

- **Recent Tasks** answers “what is happening now?” It shows queueing,
  progress, completion, failures, selected task cancellation, and force-stop
  follow-up where a graceful shutdown timed out. In a multi-cluster installation
  its cluster selector filters the five task rows without changing operation
  scope elsewhere. Cluster-neutral operations, such as a global storage scan,
  remain visible in every cluster filter because they apply to every cluster.
  Drag a column heading to keep a browser-local column order, just as in VM/CT
  Overview.
- **Audit** answers “what happened and who did it?” It is the durable event log
  for logins, changes, scans, and file actions, with an optional cluster filter.

### When part of a multi-file action succeeds

Selecting several files and trashing or moving them is a fan-out, not one
atomic operation. If some files succeed and others are refused, PVE-helper does
not roll the successful ones back and does not report the whole action as a
single failure. Instead:

- every file that succeeded is audited individually, as it happens;
- one **Partly completed** row owns the operation and names how many of how many
  succeeded, plus each failure and its reason;
- that row asks a question — *“2 of 3 moved to trash — click to answer”* — and
  keeps pulsing and pinned to the top of Recent Tasks until you answer it. It is
  deliberately exempt from the normal one-hour Recent Tasks retention: an
  unanswered question is not history yet.
- Answering it opens a dialog offering to **Retry N file(s)** or to **Accept this
  outcome**. Both are decisions and both close the question for good, so the
  buttons say which decision they are rather than “Cancel”.
- Closing that dialog with **×** or **Esc** is not a third answer. It defers: the
  question stays pinned and you can reopen it from the same row. This holds for
  every question in Recent Tasks, including the force-stop follow-up, whose two
  answers are **Force stop** and **Leave it running** — nothing you can click by
  accident throws an unanswered question away.

If Recent Tasks is collapsed, an **Attention needed (N)** indicator appears in
its header and expands the list when clicked, so an open question cannot sit
unseen behind a hidden panel.

Do not treat a browser redirect or a queued banner as completion. For any
background operation, wait for its terminal Recent Tasks row and inspect a
failure before retrying. Retrying a still-running backup, import, migration, or
inflate can create conflicting work.

Audit supports module and text filters, a date range, and optional technical
fields. CSV and JSON exports stream all matching rows. Excel exports are limited
to 5,000 events; narrow the filters or use CSV/JSON for larger exports.

### Forward Audit events to syslog or a SIEM

Open **Audit → Log forwarder**, or **PVE-helper Settings → Log forwarder**, to
send new Audit events to one installation-wide RFC 5424 destination. Configure
the receiver's host and port, choose **TCP with TLS** or plain **TCP**, enable
forwarding, and select **Save configuration**. TLS delivery then waits for you to
approve the certificate the destination serves; nothing is sent over TLS until
you have.

#### Approving the destination's certificate

The system trust store contains public CAs, so a collector using an internal or
self-signed certificate — the normal case in a home lab or small firm — cannot be
verified by it. Rather than forcing a choice between plaintext TCP and no TLS at
all, the forwarder asks you once, the way SSH asks about a new host key.

Select **Inspect and approve** in the **Destination certificate** panel. The app
connects far enough to read the certificate and shows its subject, issuer,
validity and SHA-256 fingerprint, and states whether it already verifies against
the trust store. **That first read is unverified** — compare the fingerprint
against the collector itself before accepting it. Then choose one of:

- **Always trust certificates from this issuer** — trusts the issuing CA and
  nothing else. Renewals from the same CA are accepted silently; a certificate
  from any other issuer is not. Recommended, and unavailable if the destination
  sends no issuer certificate.
- **Trust only this exact certificate** — the strictest option. Every renewal is
  reported as a change you must approve again.
- **Accept any certificate (no verification)** — encrypted but unauthenticated.
  Anything answering on that address is accepted, and you get no change or expiry
  warnings.

The decision is recorded in Audit with who made it. It applies to the host and
port it was made for: re-pointing the forwarder clears it and asks again.

#### Certificate changes and expiry

Once a day the app re-reads the destination's certificate. It raises an
answerable **Recent Tasks** question — the same pulsing "click to answer" badge
used elsewhere — when the certificate stops verifying under your approval, when a
pinned certificate is replaced, or when the served certificate is within **seven
days** of expiring. Answering opens the same approval dialog; approving the new
certificate closes the question. An expiring certificate has nothing to approve
yet, so it can also be acknowledged, which is recorded in Audit. The same finding
is raised once, not once per day, and a check that finds everything healthy
closes any question it had raised.

Forwarding starts with Audit events created while the forwarder is enabled; it
does not export the existing Audit history. Delivery is durable and ordered.
The worker normally checks for queued messages once per minute, retries a failed
connection with increasing delays, and resumes an abandoned in-progress
delivery. This is at-least-once delivery, so a receiver may see a duplicate
after an uncertain connection failure. Receivers must accept RFC 5424 messages
over RFC 6587 octet-counted TCP framing.

The **Delivery status** panel refreshes automatically:

- **Pending** counts messages still waiting or currently being sent. It returns
  to zero after the receiver accepts them; when forwarding is disabled, queued
  messages remain as **paused** until it is enabled again.
- **Last delivery** is the most recent successful send, not merely the most
  recent attempt.
- **Last error** names which kind of failure it was — the collector was
  unreachable, its certificate failed verification, the TLS handshake failed, or
  its certificate has not been approved — with the time. These need opposite
  responses from you, so they are not collapsed into one message. Detailed socket
  or TLS exceptions remain in protected application logs.

After saving an enabled destination, select **Send test event**. This creates a
real `log_forwarder.test_requested` Audit event and sends it through the same
durable path as ordinary events. “Queued” means accepted for delivery; confirm
success by checking that **Pending** reaches zero, **Last delivery** advances,
and the event appears at the receiver.

Each syslog message contains normalized scalar Audit fields such as timestamp,
action, outcome, module, username, source IP, cluster and object identity. Where
applicable it also includes the normalized storage ID, path and target
preallocation. Arbitrary Audit details, raw provider errors, credentials,
certificate material and diagnostic exception text are deliberately excluded.
Changing the configuration and requesting a test are themselves visible in
Audit. Saving configuration has no separate confirmation popup.

### Serve the UI over HTTPS, and manage trusted authorities

Open **PVE-helper Settings → Certificates**. The tab owns three decisions: which
certificate this installation presents over HTTPS, which certificate authorities it
trusts when it connects out, and how far in advance any certificate's expiry is
warned about.

#### Uploading a certificate

The upload accepts what a CA is likely to hand you rather than one canonical shape:
a PEM certificate, a DER-encoded `.crt`/`.cer`, a `fullchain.pem`, or a PKCS#12
`.pfx`/`.p12` export. The private key may be a separate file or inside the PKCS#12.
Encrypted keys and password-protected PKCS#12 files both use the **Password** field.
Certificates in a bundle need not be in any particular order — the leaf is identified
by which certificate issued nothing else in the upload, and the chain is ordered from
it.

A server certificate is refused at upload if no private key was supplied or if the
key does not belong to the certificate. That check happens here rather than at reload
time, where the only symptom would be an nginx error in a container log. The private
key is encrypted at rest under the same keyring that seals cluster API tokens, so it
is not recoverable from a database dump alone.

A trusted authority is the certificate only. An upload that also contains a private
key is refused: that is almost always a misfired export of the CA's own signing key,
and this feature has no use for one.

#### Turning HTTPS on

Select a certificate, tick **Enable HTTPS**, and apply. The certificate and its chain
are written to a volume nginx reads, and nginx picks them up within about a minute —
no restart and no shell access to the Docker host.

The port does not change. The address you already use starts answering TLS instead of
plain HTTP, and a browser that arrives over `http://` is redirected to the same
address over `https://`, so bookmarks keep working.

If an external reverse proxy sits in front of this installation, it is included in
that switch: point its upstream at `https://` when you enable HTTPS, or it will keep
speaking plain HTTP to a port that no longer answers it and be redirected back at
itself.

One setting in `.env` overrides all of this:

- `APP_FORCE_HTTP=true` — serve plain HTTP, whatever is stored. **This is the way
  back in.** A certificate that turns out to be wrong would otherwise lock you out of
  the page that would replace it, so the recovery switch deliberately lives outside
  the application: set it, recreate the containers, fix the certificate, remove it
  again.

The certificate currently serving HTTPS cannot be deleted. Its Remove button is
rendered disabled and says why, rather than disappearing. Select a different
certificate or disable HTTPS first.

#### Trusted certificate authorities

An authority added here is appended to the trust bundle used for every outbound TLS
connection this installation makes — Proxmox endpoints included. It **extends** the
deployment's own bundle rather than replacing it, so adding one internal CA does not
stop public certificates from verifying.

This is separate from a cluster connection's own transport trust, which pins one
cluster to one chain, and separate again from the log forwarder's destination
approval. An authority here widens what verifies; those two decide what a specific
endpoint is allowed to present.

#### Expiry warnings

One threshold covers every certificate this installation knows about: the HTTPS
certificate, each trusted authority, and the log forwarder's destination. Set whether
warnings are on and how many days ahead they start, between 1 and 99.

The check runs once a day. A certificate inside the window raises a question in
Recent Tasks with the same pulsing "click to answer" badge used elsewhere, because
the Certificates page is not a page anyone visits and an expiring HTTPS certificate
takes the UI down with it. The question names the certificate and how long is left,
and offers two answers: open the Certificates tab, or acknowledge that you have seen
it. Acknowledging stops the badge; it does not pretend anything was renewed.

Questions do not repeat. The same finding about the same certificate is filed once
and not refiled daily. A certificate that crosses from expiring to already expired
does raise the second, more serious question. Replacing or removing the certificate
closes its question automatically at the next daily check, and so does turning
warnings off.


## Working with VMs and containers

### Find a guest

Use either VMs/CTs surface:

- **Overview** is the broad table for status, filtering, sorting, selection, and
  bulk operations.
- **Inventory** is the persistent guest list and detail workspace. Select a
  guest to work through its tabs.

Guest identity is `(cluster, VM/CT type, VMID)`; the node is its current location
and may change after migration. Overview labels the cluster and Inventory groups
the guest tree by cluster when several are configured. When names or VMIDs
overlap, verify both cluster and node before taking an action. Linked-clone
ancestry, locks, and the latest projected runtime status are also shown where
available. Old bookmarks without a cluster redirect only when identity is
unique; an ambiguous bookmark asks you to choose instead of guessing.

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

pve-helper separates its API inventory from optional filesystem access:

| Storage type | What pve-helper can do |
| --- | --- |
| **Proxmox API inventory (Layer 1)** | Read definitions, node state, capacity, volumes and guest references. Block/API backends such as LVM-thin, RBD and PBS remain useful here but never pretend to have a file browser. |
| **Registered file-tree mount (Layer 2)** | Adds scanning, folder browsing and supported file actions for eligible backends such as `dir`, NFS, CIFS and CephFS. The mount is an explicit deployment association, not inferred from a matching name. |

Both layers are visible on **Storage → Overview**, labelled as such: *Storage
catalog* is Layer 1, *Storage gate* is Layer 2, and mount associations live under
**PVE-helper Settings → Storage access** (also Layer 2).

The sidebar shows Layer 1 only. Under **Storage**, each cluster lists its
published datastores — **Shared** first, since a shared datastore is one
cluster-wide object however many nodes see it, then one group per node for that
node's local storages (Proxmox names them all `local`/`local-lvm`, so the node
group is what tells them apart). One datastore, one page, whatever pve-helper's
own access to it happens to be.

Layer 2 shows up on that page rather than beside it. A registered mount is
pve-helper's *access* to a datastore, so it appears as the **Files** tab and as
the scan-driven panels — and where the capability is missing, the tab says so in
one line (`No file browser: lvmthin is not a browsable file-tree backend`)
instead of disappearing, so the page looks the same for every datastore.
Registering a mount stays in **PVE-helper Settings → Storage access**; it is a
deployment association and never part of how the clusters see their disks.

The **Nodes** tab answers "where is this datastore attached", including nodes
that did not answer the last refresh — those keep their entry, marked *Unknown*,
because a node taken down for patching has not lost its storage.

Scheduling is not part of a storage page. The **VMs/CTs** tab only answers which
guests consume the datastore; open the guest to schedule anything.

#### Two checks guard a destructive file action

They are not redundant, and neither implies the other:

| Check | What it proves | Evidence |
| --- | --- | --- |
| **Storage gate** (Layer 2) | PVE-helper's own scan saw the whole picture — every expected Proxmox consumer of this shared mount answered while the scan ran. | Scan-time consumer coverage |
| **Volume coverage** (Layer 1, per datastore in Storage catalog) | Proxmox's API listing is complete for that exact storage scope, bound to the generation it was observed under. | Generation-bound API coverage |

A destructive action must pass both. If one refuses, the refusal names which
one and why; an Unknown or Blocked verdict is never a deletion candidate.

#### Referenced disks: one hard block, one answerable warning

Trash, rename, move and transfer make the file leave its volid, so a disk image
that some guest configuration still points at is never treated as loose. What
that costs you depends on what is actually known about the guest:

| Verdict | What triggers it | What you can do |
| --- | --- | --- |
| **Blocked** | A node that answered reports a guest running on the file, or the file sits in the `images/<vmid>` directory of such a guest. | Stop the guest in Proxmox. Nothing in pve-helper overrides this. |
| **Danger, acknowledgeable** | A guest configuration references the file but no reachable node reports it running; or a node did not report its storage inventory, so a guest there could be using the file unseen; or the guest was last seen running on a node that cannot be reached now. | Answer the confirmation, which names every fact that applies at once. The answer is recorded in Audit whether the action then succeeds or fails. |

The line between them is where the refusal sends you. A running guest on a
reachable node is live breakage and you have somewhere to go — stop it. An
unreachable node is not: it can die for good and be replaced by a differently
named one, and its guests' configurations die with it. "Detach it in Proxmox
first" then becomes an instruction nobody can carry out, and the file would stay
unreachable through this app forever. Those cases escalate to a question instead
of a refusal.

Detaching first is still the better workflow wherever it is available. To
replace a disk (a restore, for instance), detach it from the guest in Proxmox,
attach the replacement, and then act on the loose file — the reference is gone
and no risk confirmation is needed. The acknowledgement exists for when that
path is closed, not as a shortcut past it.

**Inflate is the exception, deliberately.** It rewrites the image in place under
the same volid, for the guest that owns it, so a reference is expected. Its gate
is the one that fits: the guest must be stopped, verified live at the moment of
the action rather than from the last scan.

Mounted storage is only as writable as its effective Docker bind mount. Writes are
enabled application-wide by default, but that does not override a read-only NFS
mount. Conversely, an administrator can set `STORAGE_WRITE_ENABLED=false` to freeze
storage writes across the whole application during maintenance; that hides and
rejects storage writes even where a mount is writable.

### Scan before judging files

Storage classification is conservative. The catalog refreshes definition/node
metadata frequently and volume observations on a slower interval; accepted guest
or storage operations trigger a targeted refresh, while destructive preflight does
its own fresh read. Start a file scan after a relevant mounted-storage change. A
scan enriches the API-owned volume view with files visible through a registered
mount.

Interpret classifications carefully:

- **In use / referenced**: a scanned configuration references the entry.
- **Likely orphan**: no scanned expected consumer references the entry; this is
  the only classification eligible for the app's trash workflow.
- **Unknown / blocked**: the scan cannot safely determine ownership. Common
  causes are an unavailable expected consumer, incomplete inventory, or an
  unsupported reference. Do not delete or trash the entry based on this result.

Before treating a shared-storage object as unused, confirm the catalog has complete
coverage for its permitted active nodes and that its mount association identifies
the intended backend. pve-helper intentionally blocks orphan classification when
coverage or cross-cluster identity is incomplete instead of guessing.

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
- **Permanent deletion from the Recycle Bin cannot be undone**, and is the only
  file operation in the app that cannot. It therefore asks twice. The first
  dialog states what is about to be destroyed: the original path, the size, how
  long it has been recoverable, and — importantly — whether a guest
  configuration still points at that disk, in which case restoring it is the
  only way to make that guest whole again. The second dialog repeats those facts
  with its buttons swapped, so it cannot be cleared by muscle memory. Deleting a
  file that a *running* guest uses is not possible at all: the action is refused
  before any dialog appears.
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
- **Monitor**: historical space snapshots plus recent file activity and scans
  explicitly started for this datastore. Full-cluster scans are shown once in
  Recent Tasks and Audit instead of being duplicated under every datastore.
- **Configuration**: the storage definition visible to pve-helper.
- **Permissions**: filesystem ACL/permission information for mounted storage.
- **Nodes** and **VMs/CTs**: expected consumers and current guest references.

Use these tabs before changing a storage definition or treating an apparently
unused file as safe to remove.

An explicitly offline Proxmox node is excluded from the active candidates used for
shared-volume agreement. You can still browse through the registered mount and use
healthy members during maintenance. A node that should be online but cannot be
queried, disagreement between active shared-storage consumers, or stale/partial
coverage is shown as **unknown/incomplete** and blocks orphan conclusions and
destructive actions.

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

1. Confirm the cluster-qualified guest target, current node and the action's consequence.
2. Check the timezone shown by the app/deployment and the next run preview.
3. Decide explicitly whether a missed occurrence may catch up.
4. Use **Run now** only when an immediate queued execution is intended.

The control worker is the only scheduler, so a schedule fires once even while
bulk backup/import/scan work is running. Run history is retained independently
of the definition. Deleting a definition is a soft delete and is refused while
one of its runs is in flight.

## Tags

**Tags** is the central registry and membership view. It appears below Network
in the main navigation. In a multi-cluster installation, first select the
cluster whose independent tag registry you want to administer. Create lowercase tags with an optional color before
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

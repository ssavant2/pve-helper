# TrueNAS ACL for pve-helper

Goal: let `pve-helper` read and traverse the Proxmox NFS exports, and optionally
grant write access to a specific storage when upload/trash/restore should be
enabled.

This guide is written for TrueNAS SCALE `25.10.4 - Goldeye`.

The app container runs as:

```text
uid=10001(app) gid=10001(app)
```

TrueNAS must therefore grant access to GID `10001`. If you create a matching
TrueNAS user for naming and inspection, its UID may differ from the container
UID. Target the `pve-helper` group, not the `pve-helper` user.

## Target Paths

Use variables for your local paths before running the shell snippets:

```bash
export FS_PROXMOX_ROOT=/mnt/<pool>/<file-storage-export>/Proxmox
export VM_PROXMOX_ROOT=/mnt/<pool>/<vm-storage-export>/Proxmox
export FS_TEST_DIR="$FS_PROXMOX_ROOT/template/iso"
export VM_TEST_DIR="$VM_PROXMOX_ROOT/images/<vmid>"
```

The examples use `FS` for general file/ISO/backup storage and `VM` for VM disk
image storage. A common lab layout is capacity/spinning disks for the file
storage and SSDs for VM disks, but pve-helper only requires that the paths match
your Proxmox storage layout.

Do **not** recursively change permissions on the parent dataset unless the whole
dataset is dedicated to this Proxmox storage. Parent datasets can contain
non-Proxmox data and should keep their existing ACLs.

The clean long-term layout is to make each Proxmox storage root a child dataset.
That gives it independent ACLs, snapshots, quotas, and rollback. For the current
layout, apply ACLs only to the `Proxmox` directories.

## 1. Create the TrueNAS identity

1. Open TrueNAS.
2. Go to `Credentials` -> `Groups`.
3. Click `Add`.
4. Set:

   ```text
   Name: pve-helper
   GID: 10001
   ```

5. Save.
6. A matching user is optional for ACL naming and inspection. If you create one,
   it does not have to use UID `10001`; the important part is that its primary
   group is `pve-helper` / GID `10001`.
7. If creating the user, go to `Credentials` -> `Users`, click `Add`, and set:

   ```text
   Username: pve-helper
   Full Name: pve-helper
   Create New Primary Group: off
   Primary Group: pve-helper
   Home Directory: /var/empty
   SMB Access: off
   TrueNAS Access: off
   Disable Password: on
   ```

8. Save.

If TrueNAS says GID `10001` already exists, stop and check what already uses it.
Do not reuse another service account by accident.

## 2. Take a snapshot first

Before applying recursive ACL changes, take a manual snapshot of each affected
dataset:

```text
<pool>/<file-storage-export>
<pool>/<vm-storage-export>
```

ACL mistakes are much less exciting when rollback exists.

## 3. Add read ACL to Proxmox directories

Because `Proxmox` is a directory and not a dataset, avoid the dataset ACL editor
for the parent dataset. Use the TrueNAS shell and target the exact directory.

Proxmox creates some storage paths, such as `images/<vmid>`, as `root:root` with
restrictive modes like `750`. That ownership/mode is created by Proxmox, not by
TrueNAS. The ACL below does not change Proxmox ownership or write permissions;
it only adds an extra read/traverse rule for the `pve-helper` group so the
scanner can inspect the files.

Before using shell ACL commands, confirm that the parent datasets show
`Unix Permissions` in the TrueNAS `Datasets` -> `Permissions` widget. If they
show NFSv4 ACL entries instead, stop and convert this guide first.

Open `System` -> `Shell` in TrueNAS, or SSH to TrueNAS if SSH is enabled only
for the maintenance window.

First verify the identity. The important match is `gid=10001`:

```bash
id pve-helper
```

Example:

```text
uid=<truenas-uid>(pve-helper) gid=10001(pve-helper) groups=10001(pve-helper)
```

Then grant read/traverse on existing files and directories:

```bash
setfacl -R -m g:pve-helper:rX "$FS_PROXMOX_ROOT"
setfacl -R -m g:pve-helper:rX "$VM_PROXMOX_ROOT"
```

Then grant default inheritance on directories so new `images/<vmid>` directories
created later also inherit read/traverse:

```bash
find "$FS_PROXMOX_ROOT" -type d -exec setfacl -m d:g:pve-helper:rX {} +
find "$VM_PROXMOX_ROOT" -type d -exec setfacl -m d:g:pve-helper:rX {} +
```

Check the current failing directory:

```bash
getfacl "$VM_TEST_DIR"
```

You should see an entry similar to:

```text
group:pve-helper:r-x
```

and, on directories, a default entry similar to:

```text
default:group:pve-helper:r-x
```

The uppercase `X` is intentional. It adds execute/traverse to directories while
not turning ordinary files into executable files.

If the dataset uses NFSv4 ACLs instead of POSIX ACLs, stop here and convert this
guide before applying shell ACL commands. The current NFS/Generic layout is
expected to use POSIX/Unix ACLs.

## 4. Add write ACL to a writable Proxmox storage

Only do this for a storage that should allow upload/trash/restore/inflate from
`pve-helper`.

Grant write access on existing files and directories:

```bash
setfacl -R -m g:pve-helper:rwX "$FS_PROXMOX_ROOT"
```

Grant default inheritance on directories:

```bash
find "$FS_PROXMOX_ROOT" -type d -exec setfacl -m d:g:pve-helper:rwX {} +
```

When enabling writes on the VM storage, use the same pattern:

```bash
setfacl -R -m g:pve-helper:rwX "$VM_PROXMOX_ROOT"
find "$VM_PROXMOX_ROOT" -type d -exec setfacl -m d:g:pve-helper:rwX {} +
```

Verify a writable directory. For VM storage, test the directory that contains
the qcow2 file to inflate:

```bash
getfacl "$FS_TEST_DIR"
getfacl "$VM_TEST_DIR"
```

Expected write entries:

```text
group:pve-helper:rwx
default:group:pve-helper:rwx
```

Avoid user ACL entries such as `user:pve-helper` or `user:10001` for this
deployment. `user:pve-helper` applies to the TrueNAS user's UID, not the
container's UID, and a numeric `user:10001` entry is less clear than the group
rule.

If a numeric user ACL was added earlier, remove it before relying on the group
ACL. POSIX ACL evaluation will match `user:10001` for the container user before
it considers `group:pve-helper`, so a `user:10001:r-x` entry can block writes
even when the group has `rwx`:

```bash
setfacl -R -x u:10001 "$VM_PROXMOX_ROOT"
find "$VM_PROXMOX_ROOT" -type d -exec setfacl -x d:u:10001 {} +
```

## 5. Refresh NFS if needed

ACL changes are often immediate. User/group identity changes can take a few
minutes to affect NFS clients.

If the Docker host still gets `Permission denied`, restart or reload the NFS
service in TrueNAS:

```text
System -> Services -> NFS -> Restart
```

Then recreate the app containers:

```bash
cd /docker-apps/pve-helper
docker compose up -d --force-recreate web worker
```

If it still fails, remount the exports on the Docker host:

```bash
cd /docker-apps/pve-helper
docker compose stop web worker
sudo umount /mnt/pve-helper/nas-files
sudo umount /mnt/pve-helper/nas-vms
sudo mount /mnt/pve-helper/nas-files
sudo mount /mnt/pve-helper/nas-vms
docker compose up -d web worker
```

## 6. Verify from the Docker host

The app user must be able to list the VM directory:

```bash
docker compose exec -T web sh -c 'id; ls -la /storages/nas-vms/images/<vmid>'
```

Expected:

```text
uid=10001(app) gid=10001(app)
...
vm-<vmid>-disk-0.qcow2
```

Then run a scan:

```bash
docker compose exec -T web python manage.py shell -c "from core.models import ScanRun; from core.tasks import run_scan; s=ScanRun.objects.create(progress_message='ACL verification scan'); run_scan(s.id); s.refresh_from_db(); print(s.summary_counts); print(s.error_details)"
```

Expected result:

```text
'referenced': 1
{}
```

or at least no `PermissionError` for `images/<vmid>`.

To verify write access for a writable storage:

```bash
docker compose exec -T web sh -c 'printf test > /storages/nas-files/template/iso/.pve-helper-write-test && rm -f /storages/nas-files/template/iso/.pve-helper-write-test'
docker compose exec -T web sh -c 'printf test > /storages/nas-vms/images/<vmid>/.pve-helper-write-test && rm -f /storages/nas-vms/images/<vmid>/.pve-helper-write-test'
docker compose exec -T worker sh -c 'printf test > /storages/nas-vms/images/<vmid>/.pve-helper-worker-write-test && rm -f /storages/nas-vms/images/<vmid>/.pve-helper-worker-write-test'
```

## Notes

- Both the host NFS mount and Docker bind mount must be read-write for a storage
  before upload/trash/restore can work.
- Inflate rewrites qcow2 images through the background worker. To preserve
  Proxmox ownership on files such as `root:root` VM disks, run the worker with
  UID/GID `0:0`, supplementary group `10001`, and only `CAP_CHOWN`; keep the web
  container as the unprivileged app user.
- Do not use NFS `Mapall` for this. The same exports are used by Proxmox, so
  changing identity mapping at the share level is more invasive than needed.
- The ACL grants additional group access to `pve-helper`; it should not change
  Proxmox ownership.

## References

- TrueNAS 25.10 ACL permissions: https://www.truenas.com/docs/scale/25.10/scaletutorials/datasets/permissionsscale/
- TrueNAS 25.10 permissions UI reference: https://www.truenas.com/docs/scale/25.10/scaleuireference/datasets/editaclscreens/
- TrueNAS 25.10 NFS shares: https://www.truenas.com/docs/scale/25.10/scaletutorials/shares/addingnfsshares/

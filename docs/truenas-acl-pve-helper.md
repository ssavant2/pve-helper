# TrueNAS ACL for pve-helper

Goal: let `pve-helper` read and traverse the Proxmox NFS exports without giving
it write access.

This guide is written for TrueNAS SCALE `25.10.4 - Goldeye`.

The app container runs as:

```text
uid=10001(app) gid=10001(app)
```

TrueNAS must therefore know about UID/GID `10001` and the Proxmox storage paths
must grant that identity read/traverse access.

## Target Paths

In this environment the NFS exports are existing vSphere-oriented datasets, and
`Proxmox` is a plain directory inside each export, not its own dataset:

```text
/mnt/Pool-FS/FS/Proxmox
/mnt/Pool-VMs/VM/Proxmox
```

Do **not** recursively change permissions on the parent datasets:

```text
/mnt/Pool-FS/FS
/mnt/Pool-VMs/VM
```

Those parent datasets can contain non-Proxmox/vSphere data and should keep their
existing ACLs.

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
6. Go to `Credentials` -> `Users`.
7. Click `Add`.
8. Set:

   ```text
   Username: pve-helper
   Full Name: pve-helper
   UID: 10001
   Create New Primary Group: off
   Primary Group: pve-helper
   Home Directory: /var/empty
   SMB Access: off
   TrueNAS Access: off
   Disable Password: on
   ```

9. Save.

If TrueNAS says UID or GID `10001` already exists, stop and check what already
uses it. Do not reuse another service account by accident.

## 2. Take a snapshot first

Before applying recursive ACL changes, take a manual snapshot of each parent
dataset:

```text
Pool-FS/FS
Pool-VMs/VM
```

ACL mistakes are much less exciting when rollback exists.

## 3. Add read ACL to Proxmox directories

Because `Proxmox` is a directory and not a dataset, avoid the dataset ACL editor
for the parent dataset. Use the TrueNAS shell and target the exact directory.

Before using shell ACL commands, confirm that the parent datasets show
`Unix Permissions` in the TrueNAS `Datasets` -> `Permissions` widget. If they
show NFSv4 ACL entries instead, stop and convert this guide first.

Open `System` -> `Shell` in TrueNAS, or SSH to TrueNAS if SSH is enabled only
for the maintenance window.

First verify the user exists:

```bash
id pve-helper
```

Expected:

```text
uid=10001(pve-helper) gid=10001(pve-helper)
```

Then grant read/traverse on existing files and directories:

```bash
setfacl -R -m u:pve-helper:rX /mnt/Pool-FS/FS/Proxmox
setfacl -R -m u:pve-helper:rX /mnt/Pool-VMs/VM/Proxmox
```

If name lookup fails, use the numeric UID instead:

```bash
setfacl -R -m u:10001:rX /mnt/Pool-FS/FS/Proxmox
setfacl -R -m u:10001:rX /mnt/Pool-VMs/VM/Proxmox
```

Then grant default inheritance on directories so new `images/<vmid>` directories
created later also inherit read/traverse:

```bash
find /mnt/Pool-FS/FS/Proxmox -type d -exec setfacl -m d:u:pve-helper:rX {} +
find /mnt/Pool-VMs/VM/Proxmox -type d -exec setfacl -m d:u:pve-helper:rX {} +
```

Numeric UID fallback:

```bash
find /mnt/Pool-FS/FS/Proxmox -type d -exec setfacl -m d:u:10001:rX {} +
find /mnt/Pool-VMs/VM/Proxmox -type d -exec setfacl -m d:u:10001:rX {} +
```

Check the current failing directory:

```bash
getfacl /mnt/Pool-VMs/VM/Proxmox/images/500
```

You should see an entry similar to:

```text
user:pve-helper:r-x
```

and, on directories, a default entry similar to:

```text
default:user:pve-helper:r-x
```

The uppercase `X` is intentional. It adds execute/traverse to directories while
not turning ordinary files into executable files.

If the dataset uses NFSv4 ACLs instead of POSIX ACLs, stop here and convert this
guide before applying shell ACL commands. The current NFS/Generic layout is
expected to use POSIX/Unix ACLs.

## 4. Refresh NFS if needed

ACL changes are often immediate. User/group identity changes can take a few
minutes to affect NFS clients.

If docker3 still gets `Permission denied`, restart or reload the NFS service in
TrueNAS:

```text
System -> Services -> NFS -> Restart
```

Then recreate the app containers:

```bash
cd /docker-apps/pve-helper
docker compose up -d --force-recreate web worker
```

If it still fails, remount the exports on docker3:

```bash
cd /docker-apps/pve-helper
docker compose stop web worker
sudo umount /mnt/pve-helper/truenas-fs
sudo umount /mnt/pve-helper/truenas-vm
sudo mount /mnt/pve-helper/truenas-fs
sudo mount /mnt/pve-helper/truenas-vm
docker compose up -d web worker
```

## 5. Verify from docker3

The app user must be able to list the VM directory:

```bash
docker compose exec -T web sh -c 'id; ls -la /storages/truenas-vm/images/500'
```

Expected:

```text
uid=10001(app) gid=10001(app)
...
vm-500-disk-0.qcow2
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

or at least no `PermissionError` for `images/500`.

## Notes

- Keep the NFS mount read-only on docker3 for now.
- Keep the Docker bind mount read-only for now.
- Do not use NFS `Mapall` for this. The same exports are used by Proxmox, so
  changing identity mapping at the share level is more invasive than needed.
- This only grants read/traverse to `pve-helper`; it should not change Proxmox
  ownership or write behavior.

## References

- TrueNAS 25.10 ACL permissions: https://www.truenas.com/docs/scale/25.10/scaletutorials/datasets/permissionsscale/
- TrueNAS 25.10 permissions UI reference: https://www.truenas.com/docs/scale/25.10/scaleuireference/datasets/editaclscreens/
- TrueNAS 25.10 NFS shares: https://www.truenas.com/docs/scale/25.10/scaletutorials/shares/addingnfsshares/

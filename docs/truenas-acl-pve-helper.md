# TrueNAS ACL for pve-helper

Goal: let `pve-helper` read and traverse the Proxmox NFS exports without giving
it write access.

This guide is written for TrueNAS SCALE `25.10.4 - Goldeye`.

The app container runs as:

```text
uid=10001(app) gid=10001(app)
```

TrueNAS must therefore know about UID/GID `10001` and the datasets must grant
that identity read/traverse access.

## Datasets

Apply this to both exported datasets:

```text
/mnt/Pool-FS/FS/Proxmox
/mnt/Pool-VMs/VM/Proxmox
```

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

Before applying recursive ACL changes, take a manual snapshot of each dataset:

```text
Pool-FS/FS/Proxmox
Pool-VMs/VM/Proxmox
```

ACL mistakes are much less exciting when rollback exists.

## 3. Add read ACL on each dataset

Do this once for `Pool-FS/FS/Proxmox` and once for `Pool-VMs/VM/Proxmox`.

1. Go to `Datasets`.
2. Select the dataset, for example `Pool-VMs` -> `VM` -> `Proxmox`.
3. Find the `Permissions` widget.
4. Look at what the widget says:
   - `Unix Permissions` means POSIX/Unix permissions.
   - NFSv4 entries such as `owner@`, `group@`, or `everyone@` mean NFSv4 ACL.
5. Click `Edit`.
6. Do **not** change owner or owner group.
7. Do **not** use `Strip ACL`.
8. Do **not** replace the ACL with a preset.
9. Click `Add Item`.

If the screen is an **NFSv4 ACL** editor, set the new item to:

```text
Who: User
User: pve-helper
ACL Type: Allow
Permissions: Basic -> Read
Flags: Basic -> Inherit
```

Then enable:

```text
Apply permissions recursively
```

Click `Save Access Control List`.

If the screen is a **POSIX/Unix Permissions** editor instead:

1. Click `Add ACL` if the screen first shows only the basic Unix permissions.
2. Choose `Create a custom ACL` if TrueNAS asks whether to use a preset.
3. Add an ACL entry:

```text
Who/Type: User
User: pve-helper
Permissions: Read + Execute
Default/Inherit: enabled if shown
```

Then enable recursive apply and save.

If TrueNAS shows both `Read` and `Execute` separately, keep both selected. `Execute`
on directories is what lets the scanner enter `images/500`; `Read` lets it list
the directory and read file metadata.

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

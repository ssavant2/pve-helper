import logging
from pathlib import Path

from django.conf import settings
from django.db import migrations

from core.services.confined_filesystem import ConfinedFilesystemError, rename_directory_noreplace

logger = logging.getLogger(__name__)

LEGACY_SEGMENT = ".pve-helper-trash"
CANONICAL_SEGMENT = ".trash/pve-helper"


def _canonical(relative_path: str) -> str:
    return relative_path[: -len(LEGACY_SEGMENT)] + CANONICAL_SEGMENT


def _adopt_canonical_trash_directory(apps, schema_editor):
    """Move mounts registered with `.pve-helper-trash` onto `.trash/pve-helper`.

    The registration view briefly wrote its own trash spelling while bootstrap,
    retention, the docs and every test assumed `.trash/pve-helper`; files trashed
    on such a mount were labelled Infrastructure rather than Trash.

    The row and the directory must agree, so this rewrites both or neither. If
    the storage is not mounted in the container running the migration we cannot
    tell an empty trash from an invisible one, so the row is left as it is and a
    warning names it: a stale row keeps working, a rewritten row whose files
    stayed behind would strand them and break every restore and purge.
    """
    StorageMount = apps.get_model("core", "StorageMount")
    TrashItem = apps.get_model("core", "TrashItem")
    root = Path(settings.PVE_HELPER_STORAGE_CONTAINER_ROOT)

    for mount in StorageMount.objects.filter(trash_relative_path__endswith=LEGACY_SEGMENT):
        legacy_relative = mount.trash_relative_path.strip("/")
        canonical_relative = _canonical(legacy_relative)
        legacy_dir = root / legacy_relative

        mount_root = root / (mount.relative_path or "").strip("/")
        if not mount_root.is_dir():
            logger.warning(
                "Storage mount %s is not visible at %s; its trash directory still uses the legacy "
                "%s convention and must be migrated where the storage is mounted.",
                mount.storage_id,
                mount_root,
                LEGACY_SEGMENT,
            )
            continue

        if legacy_dir.is_dir():
            try:
                rename_directory_noreplace(root, legacy_relative, canonical_relative)
            except ConfinedFilesystemError:
                logger.warning(
                    "Storage mount %s could not move %s to %s; leaving the mount on the legacy "
                    "convention for an operator to resolve rather than stranding its trashed files.",
                    mount.storage_id,
                    legacy_relative,
                    canonical_relative,
                    exc_info=True,
                )
                continue

        mount.trash_relative_path = canonical_relative
        mount.trash_path = f"{root / canonical_relative}"
        mount.save(update_fields=["trash_relative_path", "trash_path"])

        # Each item records the absolute path of its own trashed copy; restore
        # and purge open exactly that path. Matching on the directory that just
        # moved rather than on the mount catches items whose mount link is null.
        legacy_prefix = f"{legacy_dir}/"
        canonical_prefix = f"{root / canonical_relative}/"
        for item in TrashItem.objects.filter(trash_path__startswith=legacy_prefix):
            item.trash_path = canonical_prefix + item.trash_path[len(legacy_prefix) :]
            item.save(update_fields=["trash_path"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0021_volume_observation_diff_model"),
    ]

    operations = [
        migrations.RunPython(_adopt_canonical_trash_directory, migrations.RunPython.noop),
    ]

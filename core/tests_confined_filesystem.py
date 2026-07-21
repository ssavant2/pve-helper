from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from core.services import confined_filesystem
from core.services.confined_filesystem import (
    ConfinedFilesystemError,
    ConfinedPathExistsError,
    confined_directory,
    copy_regular_file_noreplace,
    create_directory_noreplace,
    create_regular_file_exclusive,
    hardlink_open_file_noreplace,
    hardlink_open_file_to_new_directory,
    normalized_relative_path,
    open_regular_file,
    remove_confined_directory,
    remove_confined_tree,
    rename_directory_noreplace,
    rename_entry_noreplace,
    rename_regular_file_noreplace,
    set_confined_owner_and_mode,
)
from core.services.confined_names import ConfinedNameError, confined_path_component


class ConfinedFilesystemTests(SimpleTestCase):
    def test_normalization_rejects_absolute_and_parent_paths(self):
        for candidate in ("/etc/passwd", "../outside", "inside/../../outside", "inside\\file"):
            with self.subTest(candidate=candidate):
                with self.assertRaises(ConfinedFilesystemError):
                    normalized_relative_path(candidate)

    def test_open_rejects_symlinked_parent_and_file(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            outside_root = Path(outside)
            (outside_root / "secret").write_text("secret", encoding="utf-8")
            (root / "linked-parent").symlink_to(outside_root, target_is_directory=True)
            (root / "linked-file").symlink_to(outside_root / "secret")

            for relative_path in ("linked-parent/secret", "linked-file"):
                with self.subTest(relative_path=relative_path):
                    with self.assertRaises(ConfinedFilesystemError):
                        with open_regular_file(root, relative_path):
                            pass

    def test_open_reads_regular_file_beneath_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "nested").mkdir()
            (root / "nested" / "file.txt").write_bytes(b"safe")

            with open_regular_file(root, "nested/file.txt") as handle:
                self.assertEqual(handle.read(), b"safe")

    def test_rename_is_no_replace_and_rejects_symlink_source(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            directory = root / "nested"
            directory.mkdir()
            (directory / "source.txt").write_bytes(b"source")
            (directory / "existing.txt").write_bytes(b"existing")

            with self.assertRaises(ConfinedPathExistsError):
                rename_regular_file_noreplace(root, "nested/source.txt", "existing.txt")
            self.assertEqual((directory / "source.txt").read_bytes(), b"source")
            self.assertEqual((directory / "existing.txt").read_bytes(), b"existing")

            renamed = rename_regular_file_noreplace(root, "nested/source.txt", "renamed.txt")
            self.assertEqual(renamed, "nested/renamed.txt")
            self.assertEqual((directory / "renamed.txt").read_bytes(), b"source")

            outside_file = Path(outside) / "outside.txt"
            outside_file.write_bytes(b"outside")
            (directory / "linked.txt").symlink_to(outside_file)
            with self.assertRaises(ConfinedFilesystemError):
                rename_regular_file_noreplace(root, "nested/linked.txt", "escaped.txt")
            self.assertEqual(outside_file.read_bytes(), b"outside")

    def test_directory_rename_creates_parents_refuses_target_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            legacy = root / "vol" / ".pve-helper-trash"
            (legacy / "20260101T000000Z").mkdir(parents=True)
            (legacy / "20260101T000000Z" / "old.qcow2").write_bytes(b"trashed")

            # The `.trash` parent does not exist yet; it is created on the way.
            rename_directory_noreplace(root, "vol/.pve-helper-trash", "vol/.trash/pve-helper")
            moved = root / "vol" / ".trash" / "pve-helper" / "20260101T000000Z" / "old.qcow2"
            self.assertEqual(moved.read_bytes(), b"trashed")
            self.assertFalse(legacy.exists())

            # A second mount's move into an occupied target must not merge.
            (root / "vol" / ".pve-helper-trash").mkdir()
            with self.assertRaises(ConfinedPathExistsError):
                rename_directory_noreplace(root, "vol/.pve-helper-trash", "vol/.trash/pve-helper")
            self.assertEqual(moved.read_bytes(), b"trashed")

            outside_root = Path(outside)
            (outside_root / "captive").mkdir()
            (root / "linked").symlink_to(outside_root, target_is_directory=True)
            with self.assertRaises(ConfinedFilesystemError):
                rename_directory_noreplace(root, "linked/captive", "vol/.trash/stolen")
            self.assertTrue((outside_root / "captive").is_dir())

            # A regular file is not a directory move, even with a valid path.
            (root / "vol" / "plain.txt").write_bytes(b"plain")
            with self.assertRaises(ConfinedFilesystemError):
                rename_directory_noreplace(root, "vol/plain.txt", "vol/.trash/plain.txt")

    def test_hardlink_staging_uses_confined_open_file_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source.img").write_bytes(b"disk image")

            with open_regular_file(root, "source.img") as source:
                staged = hardlink_open_file_to_new_directory(
                    root,
                    source,
                    parent_relative_path="images",
                    directory_name="import-123",
                    file_name="source.img",
                )

            self.assertEqual(staged, "images/import-123/source.img")
            self.assertEqual((root / staged).read_bytes(), b"disk image")
            self.assertEqual((root / staged).stat().st_ino, (root / "source.img").stat().st_ino)

            remove_confined_directory(root, "images/import-123")
            self.assertFalse((root / "images" / "import-123").exists())
            self.assertEqual((root / "source.img").read_bytes(), b"disk image")

    def test_entry_rename_crosses_parents_and_roots_without_replacing(self):
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as dest_tmp:
            source_root = Path(source_tmp)
            dest_root = Path(dest_tmp)
            (source_root / "images" / "501").mkdir(parents=True)
            (source_root / "images" / "501" / "disk.qcow2").write_bytes(b"payload")

            # Within one root, into a parent that does not exist yet.
            rename_entry_noreplace(
                source_root,
                "images/501/disk.qcow2",
                ".trash/20260101/disk.qcow2",
                expected="file",
            )
            trashed = source_root / ".trash" / "20260101" / "disk.qcow2"
            self.assertEqual(trashed.read_bytes(), b"payload")

            # Across roots, and an occupied target keeps its own contents.
            (dest_root / "backups").mkdir()
            (dest_root / "backups" / "disk.qcow2").write_bytes(b"someone else")
            with self.assertRaises(ConfinedPathExistsError):
                rename_entry_noreplace(
                    source_root,
                    ".trash/20260101/disk.qcow2",
                    "backups/disk.qcow2",
                    target_root=dest_root,
                    expected="file",
                )
            self.assertEqual((dest_root / "backups" / "disk.qcow2").read_bytes(), b"someone else")
            self.assertEqual(trashed.read_bytes(), b"payload")

            rename_entry_noreplace(
                source_root,
                ".trash/20260101/disk.qcow2",
                "backups/moved.qcow2",
                target_root=dest_root,
                expected="file",
            )
            self.assertEqual((dest_root / "backups" / "moved.qcow2").read_bytes(), b"payload")
            self.assertFalse(trashed.exists())

    def test_copy_refuses_an_existing_target_instead_of_truncating_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source.qcow2").write_bytes(b"source payload")
            (root / "occupied.qcow2").write_bytes(b"do not truncate me")
            os.chmod(root / "source.qcow2", 0o640)

            # shutil.copy2 opens the destination "wb" and would empty it here.
            with self.assertRaises(ConfinedPathExistsError):
                copy_regular_file_noreplace(root, "source.qcow2", "occupied.qcow2")
            self.assertEqual((root / "occupied.qcow2").read_bytes(), b"do not truncate me")

            copy_regular_file_noreplace(root, "source.qcow2", "nested/copy.qcow2")
            copied = root / "nested" / "copy.qcow2"
            self.assertEqual(copied.read_bytes(), b"source payload")
            self.assertEqual(stat.S_IMODE(copied.stat().st_mode), 0o640)
            self.assertEqual(copied.stat().st_mtime_ns, (root / "source.qcow2").stat().st_mtime_ns)

    def test_copy_refuses_to_follow_a_symlinked_source_out_of_the_root(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            (Path(outside) / "secret").write_bytes(b"secret")
            (root / "linked").symlink_to(Path(outside) / "secret")

            with self.assertRaises(ConfinedFilesystemError):
                copy_regular_file_noreplace(root, "linked", "stolen")
            self.assertFalse((root / "stolen").exists())

    def test_exclusive_create_and_directory_create_refuse_existing_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with create_regular_file_exclusive(root, "new.part") as handle:
                handle.write(b"partial")
            self.assertEqual((root / "new.part").read_bytes(), b"partial")

            with self.assertRaises(ConfinedPathExistsError):
                create_regular_file_exclusive(root, "new.part")
            self.assertEqual((root / "new.part").read_bytes(), b"partial")

            create_directory_noreplace(root, "folder")
            with self.assertRaises(ConfinedPathExistsError):
                create_directory_noreplace(root, "folder")

    def test_tree_removal_does_not_follow_a_symlink_out_of_the_root(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            outside_root = Path(outside)
            (outside_root / "keep.qcow2").write_bytes(b"not yours to delete")

            trashed = root / ".trash" / "item"
            (trashed / "nested").mkdir(parents=True)
            (trashed / "nested" / "file.txt").write_bytes(b"trashed")
            (trashed / "escape").symlink_to(outside_root, target_is_directory=True)

            remove_confined_tree(root, ".trash/item")

            self.assertFalse(trashed.exists())
            # The link was unlinked as a link; what it pointed at is untouched.
            self.assertTrue(outside_root.is_dir())
            self.assertEqual((outside_root / "keep.qcow2").read_bytes(), b"not yours to delete")

    def test_tree_removal_refuses_a_symlinked_parent(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            outside_root = Path(outside)
            (outside_root / "victim").mkdir()
            (outside_root / "victim" / "file.txt").write_bytes(b"safe")
            (root / "linked").symlink_to(outside_root, target_is_directory=True)

            remove_confined_tree(root, "linked/victim")

            self.assertTrue((outside_root / "victim" / "file.txt").exists())

    def test_ownership_and_mode_are_not_applied_through_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            target = Path(outside) / "other.qcow2"
            target.write_bytes(b"someone else's file")
            os.chmod(target, 0o600)
            (root / "linked.qcow2").symlink_to(target)

            with self.assertRaises(ConfinedFilesystemError):
                set_confined_owner_and_mode(
                    root,
                    "linked.qcow2",
                    uid=os.geteuid(),
                    gid=os.getegid(),
                    mode=0o666,
                    expected="file",
                )
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_hardlink_noreplace_refuses_an_occupied_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source.ova").write_bytes(b"package")
            (root / "import").mkdir()
            (root / "import" / "taken.ova").write_bytes(b"existing")

            with open_regular_file(root, "source.ova") as source:
                with self.assertRaises(ConfinedPathExistsError):
                    hardlink_open_file_noreplace(root, source, "import/taken.ova")
                hardlink_open_file_noreplace(root, source, "import/staged.ova")

            self.assertEqual((root / "import" / "taken.ova").read_bytes(), b"existing")
            self.assertEqual((root / "import" / "staged.ova").stat().st_ino, (root / "source.ova").stat().st_ino)

    def test_confined_directory_child_paths_reach_the_pinned_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "images" / "501").mkdir(parents=True)
            (root / "images" / "501" / "disk.qcow2").write_bytes(b"pinned")

            with confined_directory(root, "images/501") as image_dir:
                child = image_dir.child_path("disk.qcow2")
                self.assertEqual(Path(child).read_bytes(), b"pinned")
                self.assertEqual(image_dir.pass_fds, (image_dir.fd,))
                with self.assertRaises(ConfinedFilesystemError):
                    image_dir.child_path("nested/disk.qcow2")

    def test_no_replace_rename_still_holds_where_renameat2_is_unsupported(self):
        """NFS answers EINVAL for RENAME_NOREPLACE, and NFS is what this app runs on.

        The container's own temporary directory does support the flag, so without
        forcing the fallback this suite would only ever prove the guarantee on the
        filesystem nobody stores disk images on.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "images").mkdir()
            (root / "images" / "disk.qcow2").write_bytes(b"payload")
            (root / "images" / "occupied.qcow2").write_bytes(b"do not replace me")
            (root / "guest").mkdir()
            (root / "guest" / "inner.txt").write_bytes(b"inside")
            (root / "taken").mkdir()

            with patch.object(confined_filesystem, "_RENAMEAT2", None):
                with self.assertRaises(ConfinedPathExistsError):
                    rename_entry_noreplace(root, "images/disk.qcow2", "images/occupied.qcow2", expected="file")
                self.assertEqual((root / "images" / "occupied.qcow2").read_bytes(), b"do not replace me")

                rename_entry_noreplace(root, "images/disk.qcow2", ".trash/stamp/disk.qcow2", expected="file")
                self.assertEqual((root / ".trash" / "stamp" / "disk.qcow2").read_bytes(), b"payload")
                self.assertFalse((root / "images" / "disk.qcow2").exists())

                with self.assertRaises(ConfinedPathExistsError):
                    rename_entry_noreplace(root, "guest", "taken", expected="directory")
                self.assertTrue((root / "guest" / "inner.txt").exists())

                rename_entry_noreplace(root, "guest", ".trash/stamp/guest", expected="directory")
                self.assertEqual((root / ".trash" / "stamp" / "guest" / "inner.txt").read_bytes(), b"inside")

    def test_the_rename_fallback_does_not_depend_on_hardlink_permission(self):
        """`linkat` is the tempting fallback and is unusable here.

        `fs.protected_hardlinks` refuses a link to a file the process neither owns
        nor can write, and every real disk image on these exports is root-owned
        while the app runs unprivileged. A fallback that calls linkat therefore
        fails EPERM on exactly the files it exists for, and passes in any test
        whose fixtures the test process created itself.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "disk.qcow2").write_bytes(b"payload")

            def refuse_hardlink(*args, **kwargs):
                raise AssertionError("The rename fallback must not use linkat.")

            with (
                patch.object(confined_filesystem, "_RENAMEAT2", None),
                patch.object(confined_filesystem, "_LINKAT", refuse_hardlink),
            ):
                rename_entry_noreplace(root, "disk.qcow2", "renamed.qcow2", expected="file")

            self.assertEqual((root / "renamed.qcow2").read_bytes(), b"payload")

    def test_directory_rename_fallback_refuses_a_reserved_name_that_is_not_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source").mkdir()
            (root / "source" / "file.txt").write_bytes(b"source")
            occupied = root / "occupied"
            occupied.mkdir()
            (occupied / "someone-elses.txt").write_bytes(b"keep me")

            with patch.object(confined_filesystem, "_RENAMEAT2", None):
                with self.assertRaises(ConfinedPathExistsError):
                    rename_entry_noreplace(root, "source", "occupied", expected="directory")

            self.assertEqual((occupied / "someone-elses.txt").read_bytes(), b"keep me")
            self.assertEqual((root / "source" / "file.txt").read_bytes(), b"source")

    def test_hardlink_staging_rejects_symlinked_parent(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            outside_root = Path(outside)
            (root / "source.img").write_bytes(b"disk image")
            (root / "images").symlink_to(outside_root, target_is_directory=True)

            with open_regular_file(root, "source.img") as source:
                with self.assertRaises(ConfinedFilesystemError):
                    hardlink_open_file_to_new_directory(
                        root,
                        source,
                        parent_relative_path="images",
                        directory_name="import-123",
                        file_name="source.img",
                    )

            self.assertFalse((outside_root / "import-123").exists())


class ConfinedNameTests(SimpleTestCase):
    def test_only_a_single_ordinary_component_is_accepted(self):
        self.assertEqual(confined_path_component("disk.qcow2"), "disk.qcow2")
        for candidate in ("", ".", "..", "a/b", "a\\b", "a\x00b"):
            with self.subTest(candidate=candidate):
                with self.assertRaises(ConfinedNameError):
                    confined_path_component(candidate)

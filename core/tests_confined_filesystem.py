from __future__ import annotations

import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from core.services.confined_filesystem import (
    ConfinedFilesystemError,
    ConfinedPathExistsError,
    hardlink_open_file_to_new_directory,
    normalized_relative_path,
    open_regular_file,
    remove_confined_directory,
    rename_regular_file_noreplace,
)


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

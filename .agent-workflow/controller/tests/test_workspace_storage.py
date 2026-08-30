"""Tests for descriptor-anchored workspace storage."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from td_controller.workspace_storage import WorkspaceStorage, WorkspaceStorageError


class WorkspaceStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(dir="/var/tmp")
        self.parent = Path(self.temporary_directory.name)
        self.root = self.parent / "workspaces"
        self.generation_number = 0

    def generation(self) -> str:
        self.generation_number += 1
        return f"generation-{self.generation_number:04d}"

    def storage(self, root: Path | None = None) -> WorkspaceStorage:
        return WorkspaceStorage(
            root or self.root, generation_factory=self.generation
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_reserve_open_duplicate_and_release_use_exact_anchor(self) -> None:
        with self.storage() as storage:
            anchor = storage.reserve("DOC-001", attempt=1)
            descriptor = storage.open_anchor(anchor)
            duplicate = storage.duplicate_root()

            self.assertEqual(os.listdir(descriptor), [])
            self.assertEqual(os.fstat(descriptor).st_ino, anchor.inode)
            self.assertEqual(os.fstat(duplicate).st_ino, os.stat(self.root).st_ino)
            os.close(descriptor)
            os.close(duplicate)
            storage.release_empty(anchor)
            self.assertFalse((self.root / anchor.name).exists())
            replacement = storage.reserve("DOC-001", attempt=1)
            self.assertNotEqual(replacement.generation, anchor.generation)
            with self.assertRaisesRegex(WorkspaceStorageError, "unavailable"):
                storage.open_anchor(anchor)

    def test_duplicate_reservation_is_rejected_without_reuse(self) -> None:
        with self.storage() as storage:
            first = storage.reserve("DOC-001", attempt=1)

            with self.assertRaisesRegex(WorkspaceStorageError, "already reserved"):
                storage.reserve("DOC-001", attempt=1)

            descriptor = storage.open_anchor(first)
            os.close(descriptor)

    def test_path_replacement_does_not_redirect_descriptor_operations(self) -> None:
        storage = self.storage()
        displaced = self.parent / "displaced"
        forged = self.parent / "forged"
        self.root.rename(displaced)
        forged.mkdir(mode=0o700)
        self.root.symlink_to(forged, target_is_directory=True)

        anchor = storage.reserve("DOC-001", attempt=1)

        self.assertTrue((displaced / anchor.name).is_dir())
        self.assertEqual(tuple(forged.iterdir()), ())
        descriptor = storage.open_anchor(anchor)
        os.close(descriptor)
        storage.release_empty(anchor)
        storage.close()
        self.root.unlink()
        displaced.rename(self.root)

    def test_replaced_anchor_and_symlink_are_rejected_without_escape(self) -> None:
        with self.storage() as storage:
            anchor = storage.reserve("DOC-001", attempt=1)
            original = self.root / "original"
            target = self.root / anchor.name
            target.rename(original)
            external = self.parent / "external"
            external.mkdir()
            target.symlink_to(external, target_is_directory=True)

            with self.assertRaisesRegex(WorkspaceStorageError, "unavailable"):
                storage.open_anchor(anchor)

            self.assertEqual(tuple(external.iterdir()), ())
            target.unlink()
            original.rename(target)

    def test_nonempty_anchor_is_quarantined_instead_of_deleted(self) -> None:
        with self.storage() as storage:
            anchor = storage.reserve("DOC-001", attempt=1)
            (self.root / anchor.name / "evidence").write_text("retain")

            with self.assertRaisesRegex(WorkspaceStorageError, "requires an empty"):
                storage.release_empty(anchor)
            quarantine = self.root / f".release-{anchor.generation}"
            self.assertTrue((quarantine / "evidence").is_file())
            with self.assertRaisesRegex(WorkspaceStorageError, "unavailable"):
                storage.open_anchor(anchor)

    def test_invalid_task_attempt_root_and_permissions_are_rejected(self) -> None:
        with self.storage() as storage:
            for task_id, attempt in (("bad/task", 1), ("DOC-001", 0), ("DOC-001", True)):
                with self.subTest(task_id=task_id, attempt=attempt):
                    with self.assertRaises(WorkspaceStorageError):
                        storage.reserve(task_id, attempt=attempt)
        unsafe = self.parent / "unsafe"
        unsafe.mkdir(mode=0o755)
        with self.assertRaisesRegex(WorkspaceStorageError, "permissions"):
            self.storage(unsafe)
        with self.assertRaisesRegex(WorkspaceStorageError, "root name"):
            self.storage(self.parent / "bad name")

    def test_partial_descriptor_open_failure_retains_reservation_quarantine(self) -> None:
        with self.storage() as storage:
            real_open = os.open

            def fail_child(path, flags, *args, **kwargs):
                if isinstance(path, str) and path.startswith("doc-001-attempt-1-"):
                    raise OSError("injected failure")
                return real_open(path, flags, *args, **kwargs)

            with patch("td_controller.workspace_storage.os.open", side_effect=fail_child):
                with self.assertRaisesRegex(WorkspaceStorageError, "reservation failed"):
                    storage.reserve("DOC-001", attempt=1)

            names = {path.name for path in self.root.iterdir()}
            self.assertEqual(
                names,
                {".doc-001-attempt-1.lock", "doc-001-attempt-1-generation-0001"},
            )

    def test_closed_storage_rejects_operations_idempotently(self) -> None:
        storage = self.storage()
        storage.close()
        storage.close()

        with self.assertRaisesRegex(WorkspaceStorageError, "closed"):
            storage.reserve("DOC-001", attempt=1)


if __name__ == "__main__":
    unittest.main()

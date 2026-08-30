from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from td_controller.workspace_storage import WorkspaceStorage, WorkspaceStorageError


class Generations:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"{self.value:032x}"


class WorkspaceStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "storage"
        self.root.mkdir(mode=0o700)
        self.root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.generations = Generations()

    def tearDown(self) -> None:
        os.close(self.root_fd)
        self.temporary.cleanup()

    def storage(self) -> WorkspaceStorage:
        return WorkspaceStorage(
            self.root_fd, generation_factory=self.generations
        )

    def test_borrow_uses_pinned_inode_after_path_replacement(self) -> None:
        with self.storage() as storage:
            anchor = storage.reserve("DOC-001", attempt=1)
            original = self.root / anchor.name
            original.rename(self.root / "held")
            original.mkdir(mode=0o700)
            with storage.borrow(anchor) as descriptor:
                self.assertEqual(
                    os.fstat(descriptor).st_ino, (self.root / "held").stat().st_ino
                )
                self.assertNotEqual(os.fstat(descriptor).st_ino, original.stat().st_ino)
            storage.retire(anchor)

    def test_symlink_replacement_cannot_redirect_borrow(self) -> None:
        with self.storage() as storage:
            anchor = storage.reserve("DOC-001", attempt=1)
            original = self.root / anchor.name
            original.rename(self.root / "held")
            original.symlink_to(self.root)
            with storage.borrow(anchor) as descriptor:
                self.assertEqual(
                    os.fstat(descriptor).st_ino, (self.root / "held").stat().st_ino
                )
            storage.retire(anchor)

    def test_outstanding_borrow_blocks_retirement(self) -> None:
        with self.storage() as storage:
            anchor = storage.reserve("DOC-001", attempt=1)
            with storage.borrow(anchor):
                with self.assertRaisesRegex(WorkspaceStorageError, "still borrowed"):
                    storage.retire(anchor)
            storage.retire(anchor)
            with self.assertRaisesRegex(WorkspaceStorageError, "unavailable"):
                with storage.borrow(anchor):
                    self.fail("retired capability was borrowed")

    def test_retirement_never_deletes_workspace_bytes(self) -> None:
        with self.storage() as storage:
            anchor = storage.reserve("DOC-001", attempt=1)
            evidence = self.root / anchor.name / "evidence"
            evidence.write_text("retain")
            storage.retire(anchor)
            self.assertEqual(evidence.read_text(), "retain")

    def test_duplicate_active_logical_workspace_is_rejected(self) -> None:
        with self.storage() as storage:
            anchor = storage.reserve("DOC-001", attempt=1)
            with self.assertRaisesRegex(WorkspaceStorageError, "already active"):
                storage.reserve("DOC-001", attempt=1)
            storage.retire(anchor)

    def test_permission_drift_fails_closed(self) -> None:
        with self.storage() as storage:
            anchor = storage.reserve("DOC-001", attempt=1)
            (self.root / anchor.name).chmod(0o777)
            with self.assertRaisesRegex(WorkspaceStorageError, "permissions"):
                with storage.borrow(anchor):
                    self.fail("unsafe capability was borrowed")
            (self.root / anchor.name).chmod(0o700)
            self.root.chmod(0o777)
            with self.assertRaisesRegex(WorkspaceStorageError, "permissions"):
                storage.reserve("DOC-002", attempt=1)
            self.root.chmod(0o700)
            storage.retire(anchor)

    def test_root_path_replacement_does_not_redirect_reservation(self) -> None:
        with self.storage() as storage:
            moved = self.root.with_name("moved")
            self.root.rename(moved)
            self.root.mkdir(mode=0o700)
            anchor = storage.reserve("DOC-001", attempt=1)
            self.assertTrue((moved / anchor.name).is_dir())
            self.assertFalse((self.root / anchor.name).exists())
            storage.retire(anchor)

    def test_post_creation_failure_retains_bytes_and_allows_retry(self) -> None:
        with self.storage() as storage:
            with patch(
                "td_controller.workspace_storage.os.listdir",
                side_effect=OSError("injected"),
            ):
                with self.assertRaisesRegex(WorkspaceStorageError, "reservation failed"):
                    storage.reserve("DOC-001", attempt=1)
            failed = self.root / f"doc-001-attempt-1-{1:032x}"
            self.assertTrue(failed.is_dir())
            replacement = storage.reserve("DOC-001", attempt=1)
            storage.retire(replacement)

    def test_forged_anchor_and_changed_descriptor_are_rejected(self) -> None:
        with self.storage() as storage:
            anchor = storage.reserve("DOC-001", attempt=1)
            forged = replace(anchor, inode=anchor.inode + 1)
            with self.assertRaisesRegex(WorkspaceStorageError, "unavailable"):
                with storage.borrow(forged):
                    self.fail("forged capability was borrowed")
            storage.retire(anchor)

    def test_invalid_inputs_active_close_and_closed_use_fail(self) -> None:
        with self.assertRaisesRegex(WorkspaceStorageError, "descriptor"):
            WorkspaceStorage(True)
        with self.storage() as storage:
            for task_id, attempt in (("bad", 1), ("DOC-001", True), ("DOC-001", 100)):
                with self.assertRaises(WorkspaceStorageError):
                    storage.reserve(task_id, attempt=attempt)
            anchor = storage.reserve("DOC-001", attempt=1)
            with self.assertRaisesRegex(WorkspaceStorageError, "active"):
                storage.close()
            storage.retire(anchor)
        with self.assertRaisesRegex(WorkspaceStorageError, "closed"):
            storage.reserve("DOC-001", attempt=1)

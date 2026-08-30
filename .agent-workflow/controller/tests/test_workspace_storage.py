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

    def test_inspection_remains_bound_after_path_replacement(self) -> None:
        with self.storage() as storage:
            anchor = storage.reserve("DOC-001", attempt=1)
            original = self.root / anchor.name
            original.rename(self.root / "held")
            original.mkdir(mode=0o700)
            snapshot = storage.inspect(anchor)
            self.assertEqual(snapshot.inode, (self.root / "held").stat().st_ino)
            self.assertNotEqual(snapshot.inode, original.stat().st_ino)
            storage.retire(anchor)

    def test_symlink_replacement_cannot_redirect_inspection(self) -> None:
        with self.storage() as storage:
            anchor = storage.reserve("DOC-001", attempt=1)
            original = self.root / anchor.name
            original.rename(self.root / "held")
            original.symlink_to(self.root)
            snapshot = storage.inspect(anchor)
            self.assertEqual(snapshot.inode, (self.root / "held").stat().st_ino)
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

    def test_retirement_never_mutates_retained_workspace(self) -> None:
        with self.storage() as storage:
            anchor = storage.reserve("DOC-001", attempt=1)
            evidence = self.root / anchor.name / "evidence"
            evidence.write_text("retain")
            self.assertEqual(storage.inspect(anchor).entry_count, 1)
            storage.retire(anchor)
            self.assertEqual(evidence.read_text(), "retain")
            with self.assertRaisesRegex(WorkspaceStorageError, "unavailable"):
                storage.inspect(anchor)

    def test_duplicate_and_forged_active_anchors_are_rejected(self) -> None:
        with self.storage() as storage:
            anchor = storage.reserve("DOC-001", attempt=1)
            with self.assertRaisesRegex(WorkspaceStorageError, "already active"):
                storage.reserve("DOC-001", attempt=1)
            forged = replace(anchor, inode=anchor.inode + 1)
            with self.assertRaisesRegex(WorkspaceStorageError, "unavailable"):
                storage.inspect(forged)
            storage.retire(anchor)

    def test_permission_drift_fails_closed(self) -> None:
        with self.storage() as storage:
            anchor = storage.reserve("DOC-001", attempt=1)
            (self.root / anchor.name).chmod(0o777)
            with self.assertRaisesRegex(WorkspaceStorageError, "permissions"):
                storage.inspect(anchor)
            (self.root / anchor.name).chmod(0o700)
            self.root.chmod(0o777)
            with self.assertRaisesRegex(WorkspaceStorageError, "permissions"):
                storage.reserve("DOC-002", attempt=1)
            self.root.chmod(0o700)
            storage.retire(anchor)

    def test_post_creation_failure_retains_directory_and_allows_retry(self) -> None:
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

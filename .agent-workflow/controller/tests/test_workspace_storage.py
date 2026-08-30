from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from td_controller.workspace_storage import (
    WorkspaceAnchor,
    WorkspaceStorage,
    WorkspaceStorageError,
)
class Generations:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"{self.value:032x}"
class WorkspaceStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.parent = Path(self.temporary.name)
        self.generations = Generations()
    def tearDown(self) -> None:
        self.temporary.cleanup()
    def storage(self) -> WorkspaceStorage:
        return WorkspaceStorage(
            self.parent, "task-storage", generation_factory=self.generations
        )

    def test_anchor_descriptor_survives_path_replacement(self) -> None:
        with self.storage() as storage:
            anchor = storage.reserve("DOC-001", attempt=1)
            root = self.parent / "task-storage"
            displaced = root / "displaced"
            (root / anchor.name).rename(displaced)
            (root / anchor.name).mkdir(mode=0o700)
            descriptor = storage.open_anchor(anchor)
            self.assertEqual(os.fstat(descriptor).st_ino, displaced.stat().st_ino)
            self.assertNotEqual(os.fstat(descriptor).st_ino, (root / anchor.name).stat().st_ino)
            os.close(descriptor)
            (root / anchor.name).rmdir()
            displaced.rename(root / anchor.name)
            storage.release_empty(anchor)
    def test_symlink_replacement_cannot_redirect_capability_or_release(self) -> None:
        with self.storage() as storage:
            anchor = storage.reserve("DOC-001", attempt=1)
            root = self.parent / "task-storage"
            original = root / anchor.name
            original.rename(root / "held")
            (root / anchor.name).symlink_to(self.parent)
            descriptor = storage.open_anchor(anchor)
            self.assertEqual(os.fstat(descriptor).st_ino, (root / "held").stat().st_ino)
            os.close(descriptor)
            with self.assertRaisesRegex(WorkspaceStorageError, "unavailable"):
                storage.release_empty(anchor)
            (root / anchor.name).unlink()
            (root / "held").rename(original)
            storage.release_empty(anchor)
    def test_release_quarantines_without_deleting_and_allows_retry(self) -> None:
        with self.storage() as storage:
            anchor = storage.reserve("DOC-001", attempt=1)
            root = self.parent / "task-storage"
            storage.release_empty(anchor)
            self.assertTrue((root / f".released-{anchor.name}").is_dir())
            self.assertTrue((root / anchor.lock_name).is_file())
            with self.assertRaisesRegex(WorkspaceStorageError, "unavailable"):
                storage.open_anchor(anchor)
            replacement = storage.reserve("DOC-001", attempt=1)
            self.assertNotEqual(replacement.generation, anchor.generation)
            storage.release_empty(replacement)
    def test_nonempty_workspace_is_not_released(self) -> None:
        with self.storage() as storage:
            anchor = storage.reserve("DOC-001", attempt=1)
            root = self.parent / "task-storage"
            evidence = root / anchor.name / "evidence"
            evidence.write_text("retain")
            with self.assertRaisesRegex(WorkspaceStorageError, "requires an empty"):
                storage.release_empty(anchor)
            evidence.unlink()
            storage.release_empty(anchor)
    def test_logical_reservation_is_exclusive_across_storage_instances(self) -> None:
        first = self.storage()
        second = self.storage()
        anchor = first.reserve("DOC-001", attempt=1)
        try:
            with self.assertRaisesRegex(WorkspaceStorageError, "already reserved"):
                second.reserve("DOC-001", attempt=1)
        finally:
            first.release_empty(anchor)
            first.close()
            second.close()
    def test_forged_anchor_is_not_a_capability(self) -> None:
        with self.storage() as storage:
            anchor = storage.reserve("DOC-001", attempt=1)
            forged = replace(anchor, inode=anchor.inode + 1)
            with self.assertRaisesRegex(WorkspaceStorageError, "unavailable"):
                storage.open_anchor(forged)
            storage.release_empty(anchor)
    def test_permission_mutation_fails_closed(self) -> None:
        with self.storage() as storage:
            anchor = storage.reserve("DOC-001", attempt=1)
            root = self.parent / "task-storage"
            (root / anchor.name).chmod(0o777)
            with self.assertRaisesRegex(WorkspaceStorageError, "permissions"):
                storage.open_anchor(anchor)
            (root / anchor.name).chmod(0o700)
            root.chmod(0o777)
            with self.assertRaisesRegex(WorkspaceStorageError, "permissions"):
                storage.reserve("DOC-002", attempt=1)
            root.chmod(0o700)
            storage.release_empty(anchor)
    def test_post_creation_failure_is_quarantined_and_retryable(self) -> None:
        with self.storage() as storage:
            original = os.listdir

            def fail_child(descriptor: int) -> list[str]:
                if os.fstat(descriptor).st_ino != (self.parent / "task-storage").stat().st_ino:
                    raise OSError("injected")
                return original(descriptor)

            with patch("td_controller.workspace_storage.os.listdir", side_effect=fail_child):
                with self.assertRaisesRegex(WorkspaceStorageError, "reservation failed"):
                    storage.reserve("DOC-001", attempt=1)
            root = self.parent / "task-storage"
            self.assertTrue((root / f".failed-doc-001-attempt-1-{1:032x}").is_dir())
            replacement = storage.reserve("DOC-001", attempt=1)
            storage.release_empty(replacement)
    def test_root_rename_does_not_redirect_operations(self) -> None:
        storage = self.storage()
        old_root = self.parent / "task-storage"
        moved_root = self.parent / "moved-storage"
        old_root.rename(moved_root)
        old_root.mkdir(mode=0o700)
        anchor = storage.reserve("DOC-001", attempt=1)
        self.assertTrue((moved_root / anchor.name).is_dir())
        storage.release_empty(anchor)
        storage.close()
    def test_invalid_inputs_and_active_close_fail_closed(self) -> None:
        with self.assertRaisesRegex(WorkspaceStorageError, "root name"):
            WorkspaceStorage(self.parent, "../escape")
        with self.storage() as storage:
            for task_id, attempt in (("bad", 1), ("DOC-001", True), ("DOC-001", 100)):
                with self.assertRaises(WorkspaceStorageError):
                    storage.reserve(task_id, attempt=attempt)
            anchor = storage.reserve("DOC-001", attempt=1)
            with self.assertRaisesRegex(WorkspaceStorageError, "active"):
                storage.close()
            storage.release_empty(anchor)
        with self.assertRaisesRegex(WorkspaceStorageError, "closed"):
            storage.reserve("DOC-001", attempt=1)


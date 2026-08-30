from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from td_controller.workspace_storage import WorkspaceStorage, WorkspaceStorageError


class WorkspaceStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(mode=0o700)
        self.descriptor = os.open(
            self.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )

    def tearDown(self) -> None:
        os.close(self.descriptor)
        self.temporary.cleanup()

    def register(self, storage: WorkspaceStorage):
        return storage.register(
            "DOC-001", attempt=1, generation="0" * 32,
            descriptor=self.descriptor,
        )

    def test_pin_remains_exact_after_path_replacement(self) -> None:
        with WorkspaceStorage() as storage:
            anchor = self.register(storage)
            self.workspace.rename(self.root / "held")
            self.workspace.mkdir(mode=0o700)
            snapshot = storage.inspect(anchor)
            self.assertEqual(snapshot.inode, (self.root / "held").stat().st_ino)
            self.assertNotEqual(snapshot.inode, self.workspace.stat().st_ino)
            storage.retire(anchor)

    def test_symlink_replacement_cannot_redirect_inspection(self) -> None:
        with WorkspaceStorage() as storage:
            anchor = self.register(storage)
            self.workspace.rename(self.root / "held")
            self.workspace.symlink_to(self.root)
            snapshot = storage.inspect(anchor)
            self.assertEqual(snapshot.inode, (self.root / "held").stat().st_ino)
            storage.retire(anchor)

    def test_registration_duplicates_the_supplied_descriptor(self) -> None:
        alternate = os.dup(self.descriptor)
        with WorkspaceStorage() as storage:
            anchor = storage.register(
                "DOC-001", attempt=1, generation="0" * 32,
                descriptor=alternate,
            )
            os.close(alternate)
            self.assertEqual(storage.inspect(anchor).inode, anchor.inode)
            storage.retire(anchor)

    def test_registration_performs_no_pathname_operations(self) -> None:
        with WorkspaceStorage() as storage, patch(
            "td_controller.workspace_storage.os.open",
            side_effect=AssertionError("pathname access"),
            create=True,
        ), patch(
            "td_controller.workspace_storage.os.mkdir",
            side_effect=AssertionError("pathname mutation"),
            create=True,
        ):
            anchor = self.register(storage)
            storage.retire(anchor)

    def test_retirement_never_mutates_workspace_bytes(self) -> None:
        with WorkspaceStorage() as storage:
            anchor = self.register(storage)
            evidence = self.workspace / "evidence"
            evidence.write_text("retain")
            self.assertEqual(storage.inspect(anchor).entry_count, 1)
            storage.retire(anchor)
            self.assertEqual(evidence.read_text(), "retain")
            with self.assertRaisesRegex(WorkspaceStorageError, "unavailable"):
                storage.inspect(anchor)

    def test_duplicate_and_forged_anchors_are_rejected(self) -> None:
        with WorkspaceStorage() as storage:
            anchor = self.register(storage)
            with self.assertRaisesRegex(WorkspaceStorageError, "already active"):
                self.register(storage)
            forged = replace(anchor, inode=anchor.inode + 1)
            with self.assertRaisesRegex(WorkspaceStorageError, "unavailable"):
                storage.inspect(forged)
            storage.retire(anchor)

    def test_permission_and_file_type_changes_fail_closed(self) -> None:
        with WorkspaceStorage() as storage:
            anchor = self.register(storage)
            self.workspace.chmod(0o777)
            with self.assertRaisesRegex(WorkspaceStorageError, "permissions"):
                storage.inspect(anchor)
            self.workspace.chmod(0o700)
            storage.retire(anchor)
        file_path = self.root / "file"
        file_path.write_text("not a directory")
        file_descriptor = os.open(file_path, os.O_RDONLY)
        try:
            with WorkspaceStorage() as storage:
                with self.assertRaisesRegex(WorkspaceStorageError, "ownership"):
                    storage.register(
                        "DOC-001", attempt=1, generation="0" * 32,
                        descriptor=file_descriptor,
                    )
        finally:
            os.close(file_descriptor)

    def test_invalid_inputs_active_close_and_closed_use_fail(self) -> None:
        with WorkspaceStorage() as storage:
            invalid = (
                ("bad", 1, "0" * 32, self.descriptor),
                ("DOC-001", True, "0" * 32, self.descriptor),
                ("DOC-001", 1, "bad", self.descriptor),
                ("DOC-001", 1, "0" * 32, True),
            )
            for task_id, attempt, generation, descriptor in invalid:
                with self.assertRaises(WorkspaceStorageError):
                    storage.register(
                        task_id, attempt=attempt, generation=generation,
                        descriptor=descriptor,
                    )
            anchor = self.register(storage)
            with self.assertRaisesRegex(WorkspaceStorageError, "active"):
                storage.close()
            storage.retire(anchor)
        with self.assertRaisesRegex(WorkspaceStorageError, "closed"):
            self.register(storage)

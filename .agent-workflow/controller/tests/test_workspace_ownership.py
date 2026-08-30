from __future__ import annotations

import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from td_controller.workspace_ownership import (
    WorkspaceOwnership,
    WorkspaceOwnershipError,
)


class WorkspaceOwnershipTests(unittest.TestCase):
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

    def register(
        self,
        ownership: WorkspaceOwnership,
        task_id: str = "DOC-001",
        attempt: int = 1,
    ):
        return ownership.register(
            task_id, attempt=attempt, generation="0" * 32,
            descriptor=self.descriptor,
        )

    def test_physical_inode_has_only_one_logical_owner(self) -> None:
        ownership = WorkspaceOwnership()
        original = self.register(ownership)
        for task_id, attempt in (("DOC-002", 1), ("DOC-001", 2)):
            with self.assertRaisesRegex(WorkspaceOwnershipError, "physical"):
                self.register(ownership, task_id, attempt)
        self.assertEqual(ownership.inspect(original).inode, original.inode)
        ownership.retire(original)
        ownership.close()

    def test_pin_survives_path_and_symlink_replacement(self) -> None:
        ownership = WorkspaceOwnership()
        owned = self.register(ownership)
        self.workspace.rename(self.root / "held")
        self.workspace.symlink_to(self.root)
        self.assertEqual(
            ownership.inspect(owned).inode, (self.root / "held").stat().st_ino
        )
        ownership.retire(owned)
        ownership.close()

    def test_supplied_descriptor_is_duplicated_and_not_exported(self) -> None:
        supplied = os.dup(self.descriptor)
        ownership = WorkspaceOwnership()
        owned = ownership.register(
            "DOC-001", attempt=1, generation="0" * 32,
            descriptor=supplied,
        )
        os.close(supplied)
        self.assertEqual(ownership.inspect(owned).inode, owned.inode)
        ownership.retire(owned)
        ownership.close()

    def test_no_operation_uses_pathname_apis(self) -> None:
        ownership = WorkspaceOwnership()
        with patch(
            "td_controller.workspace_ownership.os.open",
            side_effect=AssertionError("pathname access"),
            create=True,
        ), patch(
            "td_controller.workspace_ownership.os.mkdir",
            side_effect=AssertionError("pathname mutation"),
            create=True,
        ):
            owned = self.register(ownership)
            ownership.inspect(owned)
            ownership.retire(owned)
        ownership.close()

    def test_retirement_preserves_workspace_contents(self) -> None:
        ownership = WorkspaceOwnership()
        owned = self.register(ownership)
        evidence = self.workspace / "evidence"
        evidence.write_text("retain")
        self.assertEqual(ownership.inspect(owned).entry_count, 1)
        ownership.retire(owned)
        self.assertEqual(evidence.read_text(), "retain")
        with self.assertRaisesRegex(WorkspaceOwnershipError, "not owned"):
            ownership.inspect(owned)
        ownership.close()

    def test_failed_retirement_cannot_reuse_stale_descriptor_number(self) -> None:
        ownership = WorkspaceOwnership()
        owned = self.register(ownership)
        real_close = os.close

        def close_then_fail(descriptor: int) -> None:
            real_close(descriptor)
            raise OSError("injected")

        with patch(
            "td_controller.workspace_ownership.os.close",
            side_effect=close_then_fail,
        ):
            with self.assertRaisesRegex(WorkspaceOwnershipError, "retirement failed"):
                ownership.retire(owned)
        with self.assertRaisesRegex(WorkspaceOwnershipError, "not owned"):
            ownership.inspect(owned)
        replacement = self.register(ownership)
        ownership.retire(replacement)
        ownership.close()

    def test_close_is_synchronous_and_replays_failure(self) -> None:
        ownership = WorkspaceOwnership()
        self.register(ownership)
        entered = threading.Event()
        release = threading.Event()
        errors: list[Exception] = []

        def failing_close(_: int) -> None:
            entered.set()
            release.wait(timeout=2)
            raise OSError("injected")

        def close() -> None:
            try:
                ownership.close()
            except Exception as exc:
                errors.append(exc)

        with patch(
            "td_controller.workspace_ownership.os.close",
            side_effect=failing_close,
        ):
            first = threading.Thread(target=close)
            first.start()
            self.assertTrue(entered.wait(timeout=2))
            second = threading.Thread(target=close)
            second.start()
            self.assertTrue(second.is_alive())
            release.set()
            first.join(timeout=2)
            second.join(timeout=2)
        self.assertEqual(len(errors), 2)
        self.assertTrue(
            all(isinstance(error, WorkspaceOwnershipError) for error in errors)
        )

    def test_forgery_permissions_and_invalid_inputs_fail_closed(self) -> None:
        ownership = WorkspaceOwnership()
        owned = self.register(ownership)
        with self.assertRaisesRegex(WorkspaceOwnershipError, "logical"):
            self.register(ownership)
        forged = replace(owned, inode=owned.inode + 1)
        with self.assertRaisesRegex(WorkspaceOwnershipError, "not owned"):
            ownership.inspect(forged)
        self.workspace.chmod(0o777)
        with self.assertRaisesRegex(WorkspaceOwnershipError, "permissions"):
            ownership.inspect(owned)
        self.workspace.chmod(0o700)
        ownership.retire(owned)
        invalid = (
            ("bad", 1, "0" * 32, self.descriptor),
            ("DOC-001", True, "0" * 32, self.descriptor),
            ("DOC-001", 1, "bad", self.descriptor),
            ("DOC-001", 1, "0" * 32, True),
        )
        for task_id, attempt, generation, descriptor in invalid:
            with self.assertRaises(WorkspaceOwnershipError):
                ownership.register(
                    task_id, attempt=attempt, generation=generation,
                    descriptor=descriptor,
                )
        ownership.close()

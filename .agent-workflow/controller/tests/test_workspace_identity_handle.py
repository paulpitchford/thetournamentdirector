from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from td_controller.workspace_identity_handle import (
    WorkspaceIdentityHandle,
    WorkspaceIdentityHandleError,
)


class WorkspaceIdentityHandleTests(unittest.TestCase):
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

    def handle(self, descriptor: int | None = None) -> WorkspaceIdentityHandle:
        return WorkspaceIdentityHandle(
            "DOC-001",
            attempt=1,
            generation="0" * 32,
            descriptor=self.descriptor if descriptor is None else descriptor,
        )

    def test_identity_survives_path_and_symlink_replacement(self) -> None:
        handle = self.handle()
        self.workspace.rename(self.root / "held")
        self.workspace.symlink_to(self.root)
        handle.verify()
        self.assertEqual(handle.identity.inode, (self.root / "held").stat().st_ino)
        handle.close()

    def test_supplied_descriptor_is_duplicated(self) -> None:
        supplied = os.dup(self.descriptor)
        handle = self.handle(supplied)
        os.close(supplied)
        handle.verify()
        handle.close()

    def test_verify_uses_only_bounded_inode_metadata(self) -> None:
        handle = self.handle()
        with patch(
            "td_controller.workspace_identity_handle.os.open",
            side_effect=AssertionError("pathname access"),
            create=True,
        ), patch(
            "td_controller.workspace_identity_handle.os.listdir",
            side_effect=AssertionError("directory enumeration"),
            create=True,
        ):
            handle.verify()
        handle.close()

    def test_permission_and_non_directory_inputs_fail_closed(self) -> None:
        handle = self.handle()
        self.workspace.chmod(0o777)
        with self.assertRaisesRegex(WorkspaceIdentityHandleError, "permissions"):
            handle.verify()
        self.workspace.chmod(0o700)
        handle.close()
        file_path = self.root / "file"
        file_path.write_text("not a directory")
        file_descriptor = os.open(file_path, os.O_RDONLY)
        try:
            with self.assertRaisesRegex(WorkspaceIdentityHandleError, "ownership"):
                self.handle(file_descriptor)
        finally:
            os.close(file_descriptor)

    def test_initialization_cleanup_failure_is_normalized(self) -> None:
        file_path = self.root / "file"
        file_path.write_text("not a directory")
        file_descriptor = os.open(file_path, os.O_RDONLY)
        real_close = os.close

        def close_then_fail(descriptor: int) -> None:
            real_close(descriptor)
            raise OSError("injected")

        try:
            with patch(
                "td_controller.workspace_identity_handle.os.close",
                side_effect=close_then_fail,
            ):
                with self.assertRaisesRegex(
                    WorkspaceIdentityHandleError, "cleanup failed"
                ):
                    self.handle(file_descriptor)
        finally:
            os.close(file_descriptor)

    def test_close_is_synchronous_idempotent_and_replays_failure(self) -> None:
        handle = self.handle()
        entered = threading.Event()
        release = threading.Event()
        errors: list[Exception] = []

        def failing_close(_: int) -> None:
            entered.set()
            release.wait(timeout=2)
            raise OSError("injected")

        def close() -> None:
            try:
                handle.close()
            except Exception as exc:
                errors.append(exc)

        with patch(
            "td_controller.workspace_identity_handle.os.close",
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
            all(isinstance(error, WorkspaceIdentityHandleError) for error in errors)
        )
        with self.assertRaisesRegex(WorkspaceIdentityHandleError, "cleanup failed"):
            handle.close()

    def test_invalid_identity_fields_and_closed_verify_fail(self) -> None:
        invalid = (
            ("bad", 1, "0" * 32, self.descriptor),
            ("DOC-001", True, "0" * 32, self.descriptor),
            ("DOC-001", 1, "bad", self.descriptor),
            ("DOC-001", 1, "0" * 32, True),
        )
        for task_id, attempt, generation, descriptor in invalid:
            with self.assertRaises(WorkspaceIdentityHandleError):
                WorkspaceIdentityHandle(
                    task_id,
                    attempt=attempt,
                    generation=generation,
                    descriptor=descriptor,
                )
        handle = self.handle()
        handle.close()
        handle.close()
        with self.assertRaisesRegex(WorkspaceIdentityHandleError, "closed"):
            handle.verify()

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from td_controller.workspace_identity import (
    WorkspaceIdentityError,
    WorkspaceIdentityRegistry,
)


class WorkspaceIdentityRegistryTests(unittest.TestCase):
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
        registry: WorkspaceIdentityRegistry,
        task_id: str = "DOC-001",
        attempt: int = 1,
    ):
        return registry.register(
            task_id, attempt=attempt, generation="0" * 32,
            descriptor=self.descriptor,
        )

    def test_physical_identity_has_one_logical_owner(self) -> None:
        registry = WorkspaceIdentityRegistry()
        identity = self.register(registry)
        for task_id, attempt in (("DOC-002", 1), ("DOC-001", 2)):
            with self.assertRaisesRegex(WorkspaceIdentityError, "physical"):
                self.register(registry, task_id, attempt)
        registry.verify(identity)
        registry.retire(identity)
        registry.close()

    def test_verification_survives_path_and_symlink_replacement(self) -> None:
        registry = WorkspaceIdentityRegistry()
        identity = self.register(registry)
        self.workspace.rename(self.root / "held")
        self.workspace.symlink_to(self.root)
        registry.verify(identity)
        self.assertEqual(identity.inode, (self.root / "held").stat().st_ino)
        registry.retire(identity)
        registry.close()

    def test_verify_reads_no_paths_or_directory_entries(self) -> None:
        registry = WorkspaceIdentityRegistry()
        identity = self.register(registry)
        with patch(
            "td_controller.workspace_identity.os.open",
            side_effect=AssertionError("pathname access"),
            create=True,
        ), patch(
            "td_controller.workspace_identity.os.listdir",
            side_effect=AssertionError("directory enumeration"),
            create=True,
        ):
            registry.verify(identity)
            registry.retire(identity)
        registry.close()

    def test_retirement_failure_is_terminal(self) -> None:
        registry = WorkspaceIdentityRegistry()
        identity = self.register(registry)
        real_close = os.close

        def close_then_fail(descriptor: int) -> None:
            real_close(descriptor)
            raise OSError("injected")

        with patch(
            "td_controller.workspace_identity.os.close",
            side_effect=close_then_fail,
        ):
            with self.assertRaisesRegex(WorkspaceIdentityError, "retirement failed"):
                registry.retire(identity)
        with self.assertRaisesRegex(WorkspaceIdentityError, "unavailable"):
            registry.verify(identity)
        replacement = self.register(registry)
        registry.retire(replacement)
        registry.close()

    def test_failed_registration_cleanup_is_terminal(self) -> None:
        file_path = self.root / "file"
        file_path.write_text("not a directory")
        file_descriptor = os.open(file_path, os.O_RDONLY)
        registry = WorkspaceIdentityRegistry()
        real_close = os.close

        def close_then_fail(descriptor: int) -> None:
            real_close(descriptor)
            raise OSError("injected")

        try:
            with patch(
                "td_controller.workspace_identity.os.close",
                side_effect=close_then_fail,
            ):
                with self.assertRaisesRegex(WorkspaceIdentityError, "cleanup failed"):
                    registry.register(
                        "DOC-001", attempt=1, generation="0" * 32,
                        descriptor=file_descriptor,
                    )
            with self.assertRaisesRegex(WorkspaceIdentityError, "closed"):
                self.register(registry)
            with self.assertRaisesRegex(WorkspaceIdentityError, "cleanup failed"):
                registry.close()
        finally:
            os.close(file_descriptor)

    def test_close_is_synchronous_and_replays_failure(self) -> None:
        registry = WorkspaceIdentityRegistry()
        self.register(registry)
        entered = threading.Event()
        release = threading.Event()
        errors: list[Exception] = []

        def failing_close(_: int) -> None:
            entered.set()
            release.wait(timeout=2)
            raise OSError("injected")

        def close() -> None:
            try:
                registry.close()
            except Exception as exc:
                errors.append(exc)

        with patch(
            "td_controller.workspace_identity.os.close",
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
            all(isinstance(error, WorkspaceIdentityError) for error in errors)
        )

    def test_forgery_permissions_and_invalid_inputs_fail_closed(self) -> None:
        registry = WorkspaceIdentityRegistry()
        identity = self.register(registry)
        with self.assertRaisesRegex(WorkspaceIdentityError, "logical"):
            self.register(registry)
        forged = replace(identity, inode=identity.inode + 1)
        with self.assertRaisesRegex(WorkspaceIdentityError, "unavailable"):
            registry.verify(forged)
        self.workspace.chmod(0o777)
        with self.assertRaisesRegex(WorkspaceIdentityError, "permissions"):
            registry.verify(identity)
        self.workspace.chmod(0o700)
        registry.retire(identity)
        invalid = (
            ("bad", 1, "0" * 32, self.descriptor),
            ("DOC-001", True, "0" * 32, self.descriptor),
            ("DOC-001", 1, "bad", self.descriptor),
            ("DOC-001", 1, "0" * 32, True),
        )
        for task_id, attempt, generation, descriptor in invalid:
            with self.assertRaises(WorkspaceIdentityError):
                registry.register(
                    task_id, attempt=attempt, generation=generation,
                    descriptor=descriptor,
                )
        registry.close()

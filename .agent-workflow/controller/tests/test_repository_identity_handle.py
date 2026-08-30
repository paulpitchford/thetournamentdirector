from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from td_controller.repository_identity_handle import (
    RepositoryIdentityHandle,
    RepositoryIdentityHandleError,
)


class RepositoryIdentityHandleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="td-repository-handle-")
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o755)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_handle(self, path: Path | None = None) -> RepositoryIdentityHandle:
        descriptor = os.open(
            path or self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            return RepositoryIdentityHandle(
                path or self.root, descriptor=descriptor
            )
        finally:
            os.close(descriptor)

    def test_supplied_descriptor_is_duplicated_and_path_is_held(self) -> None:
        handle = self.make_handle()
        try:
            with handle.hold_path() as path:
                self.assertEqual(path, self.root)
                self.assertEqual(
                    (path.stat().st_dev, path.stat().st_ino),
                    (handle.identity.device, handle.identity.inode),
                )
            handle.verify()
        finally:
            handle.close()

    def test_descriptor_for_different_root_is_rejected(self) -> None:
        other = self.root / "other"
        other.mkdir(mode=0o755)
        descriptor = os.open(
            other, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            with self.assertRaisesRegex(
                RepositoryIdentityHandleError, "does not match"
            ):
                RepositoryIdentityHandle(self.root, descriptor=descriptor)
        finally:
            os.close(descriptor)

    def test_path_replacement_does_not_redirect_pinned_identity(self) -> None:
        handle = self.make_handle()
        held = self.root.with_name(self.root.name + "-held")
        self.root.rename(held)
        self.root.mkdir(mode=0o755)
        try:
            with self.assertRaisesRegex(
                RepositoryIdentityHandleError, "identity changed"
            ):
                handle.verify()
            self.assertEqual(
                (held.stat().st_dev, held.stat().st_ino),
                (handle.identity.device, handle.identity.inode),
            )
        finally:
            handle.close()
            self.root.rmdir()
            held.rename(self.root)

    def test_hold_blocks_concurrent_close_until_operation_finishes(self) -> None:
        handle = self.make_handle()
        entered = threading.Event()
        release = threading.Event()
        closed = threading.Event()

        def operation() -> None:
            with handle.hold_path():
                entered.set()
                release.wait(timeout=2)

        def close() -> None:
            handle.close()
            closed.set()

        worker = threading.Thread(target=operation)
        closer = threading.Thread(target=close)
        worker.start()
        self.assertTrue(entered.wait(timeout=2))
        closer.start()
        time.sleep(0.05)
        self.assertFalse(closed.is_set())
        release.set()
        worker.join(timeout=2)
        closer.join(timeout=2)
        self.assertTrue(closed.is_set())

    def test_nested_hold_same_thread_close_and_closed_verify_fail(self) -> None:
        handle = self.make_handle()
        with handle.hold_path():
            with self.assertRaisesRegex(RepositoryIdentityHandleError, "already held"):
                with handle.hold_path():
                    pass
            with self.assertRaisesRegex(RepositoryIdentityHandleError, "hold is active"):
                handle.close()
        handle.close()
        handle.close()
        with self.assertRaisesRegex(RepositoryIdentityHandleError, "closed"):
            handle.verify()

    def test_symlink_relative_and_writable_roots_are_rejected(self) -> None:
        link = self.root.with_name(self.root.name + "-link")
        link.symlink_to(self.root, target_is_directory=True)
        descriptor = os.open(
            self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            for path in (link, Path("relative")):
                with self.subTest(path=path):
                    with self.assertRaises(RepositoryIdentityHandleError):
                        RepositoryIdentityHandle(path, descriptor=descriptor)
            self.root.chmod(0o777)
            with self.assertRaisesRegex(RepositoryIdentityHandleError, "unsafe"):
                RepositoryIdentityHandle(self.root, descriptor=descriptor)
        finally:
            os.close(descriptor)
            self.root.chmod(0o755)
            link.unlink()

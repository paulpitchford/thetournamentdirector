from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from td_controller.pinned_directory_executor import (
    PinnedDirectoryExecutor,
    PinnedDirectoryExecutorError,
    PinnedDirectoryIdentity,
)


class PinnedDirectoryIdentityTests(unittest.TestCase):
    def make_executor(self, path: Path) -> PinnedDirectoryExecutor:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            return PinnedDirectoryExecutor(descriptor=descriptor)
        finally:
            os.close(descriptor)

    def test_identity_is_frozen_evidence_without_authority(self) -> None:
        with tempfile.TemporaryDirectory(prefix="td-pinned-identity-") as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o755)
            executor = self.make_executor(root)
            metadata = root.stat()
            try:
                identity = executor.identity
                self.assertEqual(
                    identity,
                    PinnedDirectoryIdentity(
                        metadata.st_dev, metadata.st_ino, os.geteuid()
                    ),
                )
                self.assertFalse(hasattr(identity, "path"))
                self.assertFalse(hasattr(identity, "descriptor"))
                with self.assertRaises(FrozenInstanceError):
                    identity.inode = 0
            finally:
                executor.close()

    def test_identity_evidence_cannot_authorize_a_closed_capability(self) -> None:
        with tempfile.TemporaryDirectory(prefix="td-pinned-identity-") as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o755)
            executor = self.make_executor(root)
            identity = executor.identity
            executor.close()
            self.assertEqual(executor.identity, identity)
            with self.assertRaisesRegex(PinnedDirectoryExecutorError, "closed"):
                executor.verify()

    def test_different_directories_have_distinct_identity_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="td-pinned-identity-") as temporary:
            parent = Path(temporary).resolve()
            first = parent / "first"
            second = parent / "second"
            first.mkdir(mode=0o755)
            second.mkdir(mode=0o755)
            first_executor = self.make_executor(first)
            second_executor = self.make_executor(second)
            try:
                self.assertNotEqual(first_executor.identity, second_executor.identity)
            finally:
                first_executor.close()
                second_executor.close()

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from td_controller.exact_git_blob import verify_exact_git_blob
from td_controller.git_tree_manifest import GitTreeEntry
from td_controller.workspace_blob_writer import (
    WorkspaceBlobIndeterminateError,
    WorkspaceBlobRejectedError,
    write_workspace_blob,
)
from td_controller.workspace_identity_handle import WorkspaceIdentity


class WorkspaceBlobWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="td-blob-writer-")
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.descriptor = os.open(
            self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        metadata = os.fstat(self.descriptor)
        self.identity = WorkspaceIdentity(
            "ORCH-TEST", 1, "a" * 32, metadata.st_dev, metadata.st_ino
        )

    def tearDown(self) -> None:
        os.close(self.descriptor)
        self.temporary.cleanup()

    @staticmethod
    def entry(path: str, payload: bytes, executable: bool = False):
        header = f"blob {len(payload)}\0".encode("ascii")
        digest = hashlib.sha1(
            header + payload, usedforsecurity=False
        ).hexdigest()
        entry = GitTreeEntry(PurePosixPath(path), digest, executable, True)
        return entry, verify_exact_git_blob(entry, payload)

    def test_atomically_writes_nested_regular_and_executable_blobs(self) -> None:
        regular_entry, regular_blob = self.entry("docs/readme.md", b"readme\n")
        executable_entry, executable_blob = self.entry(
            "tools/run.sh", b"#!/bin/sh\n", True
        )
        regular = write_workspace_blob(
            descriptor=self.descriptor, expected_identity=self.identity,
            entry=regular_entry, blob=regular_blob,
        )
        executable = write_workspace_blob(
            descriptor=self.descriptor, expected_identity=self.identity,
            entry=executable_entry, blob=executable_blob,
        )
        self.assertEqual((self.root / "docs/readme.md").read_bytes(), b"readme\n")
        self.assertEqual((self.root / "tools/run.sh").read_bytes(), b"#!/bin/sh\n")
        self.assertEqual((self.root / "docs/readme.md").stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.root / "tools/run.sh").stat().st_mode & 0o777, 0o755)
        self.assertEqual(regular.blob_sha, regular_entry.blob_sha)
        self.assertTrue(executable.executable)
        self.assertFalse(any(".partial" in path.name for path in self.root.rglob("*")))

    def test_workspace_path_replacement_cannot_redirect_write(self) -> None:
        entry, blob = self.entry("file.txt", b"pinned\n")
        held = self.root.with_name(self.root.name + "-held")
        self.root.rename(held)
        self.root.mkdir(mode=0o700)
        try:
            write_workspace_blob(
                descriptor=self.descriptor, expected_identity=self.identity,
                entry=entry, blob=blob,
            )
            self.assertEqual((held / "file.txt").read_bytes(), b"pinned\n")
            self.assertFalse((self.root / "file.txt").exists())
        finally:
            self.root.rmdir()
            held.rename(self.root)

    def test_existing_target_symlink_parent_and_forged_blob_are_rejected(self) -> None:
        entry, blob = self.entry("file.txt", b"content")
        (self.root / "file.txt").write_bytes(b"existing")
        with self.assertRaises(WorkspaceBlobRejectedError):
            write_workspace_blob(
                descriptor=self.descriptor, expected_identity=self.identity,
                entry=entry, blob=blob,
            )
        self.assertEqual((self.root / "file.txt").read_bytes(), b"existing")
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        (self.root / "link").symlink_to(outside, target_is_directory=True)
        nested, nested_blob = self.entry("link/new.txt", b"new")
        with self.assertRaises(WorkspaceBlobRejectedError):
            write_workspace_blob(
                descriptor=self.descriptor, expected_identity=self.identity,
                entry=nested, blob=nested_blob,
            )
        forged = type(blob)(b"forged", blob.blob_sha, blob.executable)
        with self.assertRaises(WorkspaceBlobRejectedError):
            write_workspace_blob(
                descriptor=self.descriptor, expected_identity=self.identity,
                entry=entry, blob=forged,
            )

    def test_parent_rename_during_publish_is_indeterminate_and_cleaned(self) -> None:
        entry, blob = self.entry("nested/file.txt", b"content")
        moved = self.root / "moved-parent"
        real_link = os.link

        def move_parent(*args, **kwargs):
            real_link(*args, **kwargs)
            (self.root / "nested").rename(moved)
            (self.root / "nested").mkdir(mode=0o700)

        with patch(
            "td_controller.workspace_blob_writer.os.link",
            side_effect=move_parent,
        ):
            with self.assertRaises(WorkspaceBlobIndeterminateError):
                write_workspace_blob(
                    descriptor=self.descriptor,
                    expected_identity=self.identity,
                    entry=entry, blob=blob,
                )
        self.assertFalse((moved / "file.txt").exists())
        self.assertFalse((self.root / "nested/file.txt").exists())

    def test_write_failure_is_indeterminate_and_partial_is_removed(self) -> None:
        entry, blob = self.entry("failed.txt", b"content")
        with patch(
            "td_controller.workspace_blob_writer.os.write",
            side_effect=OSError("write failure"),
        ):
            with self.assertRaises(WorkspaceBlobIndeterminateError):
                write_workspace_blob(
                    descriptor=self.descriptor,
                    expected_identity=self.identity,
                    entry=entry, blob=blob,
                )
        self.assertFalse((self.root / "failed.txt").exists())
        self.assertFalse(any(".partial" in path.name for path in self.root.iterdir()))

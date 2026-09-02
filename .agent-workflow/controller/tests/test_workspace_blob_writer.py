from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import td_controller.workspace_blob_writer as writer_module
from td_controller.exact_git_blob import verify_exact_git_blob
from td_controller.git_tree_manifest import GitTreeEntry
from td_controller.workspace_blob_writer import (
    WorkspaceBlobIndeterminateError,
    WorkspaceBlobRejectedError,
    write_workspace_blob,
)
from td_controller.workspace_identity_handle import (
    WorkspaceIdentityHandle,
    WorkspaceIdentityHandleError,
)


class WorkspaceBlobWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="td-blob-writer-")
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.descriptor = os.open(
            self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        self.workspace = WorkspaceIdentityHandle(
            "ORCH-TEST", attempt=1, generation="a" * 32,
            descriptor=self.descriptor,
        )

    def tearDown(self) -> None:
        self.workspace.close()
        os.close(self.descriptor)
        self.temporary.cleanup()

    @staticmethod
    def entry(path: str, payload: bytes, executable: bool = False):
        header = f"blob {len(payload)}\0".encode("ascii")
        digest = hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()
        entry = GitTreeEntry(PurePosixPath(path), digest, executable, True)
        return entry, verify_exact_git_blob(entry, payload)

    def test_atomically_writes_nested_regular_and_executable_blobs(self) -> None:
        regular_entry, regular_blob = self.entry("docs/readme.md", b"readme\n")
        executable_entry, executable_blob = self.entry(
            "tools/run.sh", b"#!/bin/sh\n", True)
        write_workspace_blob(
            descriptor=self.descriptor, workspace=self.workspace,
            entry=regular_entry, blob=regular_blob,
        )
        write_workspace_blob(
            descriptor=self.descriptor, workspace=self.workspace,
            entry=executable_entry, blob=executable_blob,
        )
        self.assertEqual((self.root / "docs/readme.md").read_bytes(), b"readme\n")
        self.assertEqual((self.root / "docs/readme.md").stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.root / "tools/run.sh").stat().st_mode & 0o777, 0o755)


    def test_live_workspace_hold_serializes_controller_publishers(self) -> None:
        entry, blob = self.entry("serialized.txt", b"content")
        completed = threading.Event()

        def publish() -> None:
            write_workspace_blob(
                workspace=self.workspace, descriptor=self.descriptor,
                entry=entry, blob=blob,
            )
            completed.set()

        with self.workspace.hold_identity():
            thread = threading.Thread(target=publish)
            thread.start()
            self.assertFalse(completed.wait(0.05))
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(completed.is_set())

    def test_hold_exit_failure_after_publish_is_indeterminate(self) -> None:
        entry, blob = self.entry("exit-failure.txt", b"content")

        @contextmanager
        def failing_hold():
            yield self.workspace.identity
            raise WorkspaceIdentityHandleError("release failed")

        with patch.object(self.workspace, "hold_identity", failing_hold):
            with self.assertRaises(WorkspaceBlobIndeterminateError):
                write_workspace_blob(
                    workspace=self.workspace, descriptor=self.descriptor,
                    entry=entry, blob=blob,
                )
        self.assertEqual((self.root / "exit-failure.txt").read_bytes(), b"content")

    def test_workspace_path_replacement_cannot_redirect_write(self) -> None:
        entry, blob = self.entry("file.txt", b"pinned\n")
        held = self.root.with_name(self.root.name + "-held")
        self.root.rename(held)
        self.root.mkdir(mode=0o700)
        try:
            write_workspace_blob(
                descriptor=self.descriptor, workspace=self.workspace,
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
                descriptor=self.descriptor, workspace=self.workspace,
                entry=entry, blob=blob,
            )
        self.assertEqual((self.root / "file.txt").read_bytes(), b"existing")
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        (self.root / "link").symlink_to(outside, target_is_directory=True)
        nested, nested_blob = self.entry("link/new.txt", b"new")
        with self.assertRaises(WorkspaceBlobRejectedError):
            write_workspace_blob(
                descriptor=self.descriptor, workspace=self.workspace,
                entry=nested, blob=nested_blob,
            )
        forged = type(blob)(b"forged", blob.blob_sha, blob.executable)
        with self.assertRaises(WorkspaceBlobRejectedError):
            write_workspace_blob(
                descriptor=self.descriptor, workspace=self.workspace,
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
                    workspace=self.workspace,
                    entry=entry, blob=blob,
                )
        self.assertEqual((moved / "file.txt").read_bytes(), b"content")
        self.assertFalse((self.root / "nested/file.txt").exists())

    def test_target_replacement_is_preserved_for_reconciliation(self) -> None:
        entry, blob = self.entry("file.txt", b"trusted")
        moved = self.root / "created-file"
        real_open = writer_module._open_published

        def replace_target(root_descriptor, path):
            (self.root / "file.txt").rename(moved)
            (self.root / "file.txt").write_bytes(b"replacement")
            return real_open(root_descriptor, path)

        with patch(
            "td_controller.workspace_blob_writer._open_published",
            side_effect=replace_target,
        ):
            with self.assertRaises(WorkspaceBlobIndeterminateError):
                write_workspace_blob(
                    descriptor=self.descriptor,
                    workspace=self.workspace,
                    entry=entry, blob=blob,
                )
        self.assertEqual((self.root / "file.txt").read_bytes(), b"replacement")
        self.assertEqual(moved.read_bytes(), b"trusted")

    def test_in_place_mutation_during_resolution_is_indeterminate(self) -> None:
        entry, blob = self.entry("mutated.txt", b"trusted")
        real_open = writer_module._open_published

        def mutate(root_descriptor, path):
            (self.root / "mutated.txt").write_bytes(b"changed")
            return real_open(root_descriptor, path)

        with patch(
            "td_controller.workspace_blob_writer._open_published",
            side_effect=mutate,
        ):
            with self.assertRaises(WorkspaceBlobIndeterminateError):
                write_workspace_blob(
                    descriptor=self.descriptor,
                    workspace=self.workspace,
                    entry=entry, blob=blob,
                )
        self.assertEqual((self.root / "mutated.txt").read_bytes(), b"changed")

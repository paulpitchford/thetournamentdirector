from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from td_controller.git_tree_manifest import GitTreeEntry
from td_controller.pinned_directory_executor import (
    PinnedDirectoryExecutor,
    PinnedDirectoryExecutorError,
    WorktreeAdminBinding,
)
from td_controller.workspace_identity_handle import WorkspaceIdentityHandle
from td_controller.workspace_tree_verifier import (
    WorkspaceTreeVerificationError,
    verify_workspace_tree,
)


class WorkspaceTreeVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="td-tree-verify-")
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        (self.root / ".git").write_text("gitdir: /trusted/admin\n")
        self.descriptor = os.open(
            self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        self.workspace = WorkspaceIdentityHandle(
            "ORCH-TEST", attempt=1, generation="a" * 32,
            descriptor=self.descriptor,
        )
        self.repository = PinnedDirectoryExecutor(descriptor=self.descriptor)

    def tearDown(self) -> None:
        self.repository.close()
        self.workspace.close()
        os.close(self.descriptor)
        self.temporary.cleanup()

    @staticmethod
    def entry(path: str, payload: bytes, executable: bool = False):
        digest = hashlib.sha1(
            f"blob {len(payload)}\0".encode() + payload,
            usedforsecurity=False,
        ).hexdigest()
        return GitTreeEntry(PurePosixPath(path), digest, executable, True)

    def install(self, entry: GitTreeEntry, payload: bytes) -> None:
        target = self.root / entry.path
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(payload)
        target.chmod(0o755 if entry.executable else 0o644)

    def marker_binding(self) -> WorktreeAdminBinding:
        metadata = (self.root / ".git").stat()
        return WorktreeAdminBinding(
            "admin", 1, 2, metadata.st_dev, metadata.st_ino
        )

    def test_verifies_exact_files_modes_directories_and_denied_absence(self) -> None:
        regular = self.entry("docs/readme.md", b"readme\n")
        executable = self.entry("tools/run.sh", b"#!/bin/sh\n", True)
        denied = GitTreeEntry(
            PurePosixPath("analysis/private"), "1" * 40, False, False
        )
        self.install(regular, b"readme\n")
        self.install(executable, b"#!/bin/sh\n")
        with patch.object(
            self.repository, "verify_worktree_admin_binding",
            return_value=self.marker_binding(),
        ):
            result = verify_workspace_tree(
                repository=self.repository, workspace=self.workspace,
                descriptor=self.descriptor,
                manifest=(denied, regular, executable),
            )
        self.assertEqual(result.file_count, 2)
        self.assertEqual(result.directory_count, 2)

    def test_changed_missing_extra_and_unsafe_nodes_fail_closed(self) -> None:
        entry = self.entry("file.txt", b"content")
        cases = (
            "changed", "missing-marker", "extra", "symlink",
            "fifo-file", "fifo-marker",
        )
        for case in cases:
            with self.subTest(case=case):
                self.install(entry, b"content")
                if case == "changed":
                    (self.root / "file.txt").write_bytes(b"changed")
                elif case == "missing-marker":
                    (self.root / ".git").unlink()
                elif case == "extra":
                    (self.root / "extra.txt").write_bytes(b"extra")
                elif case == "symlink":
                    (self.root / "file.txt").unlink()
                    (self.root / "file.txt").symlink_to("elsewhere")
                elif case == "fifo-file":
                    (self.root / "file.txt").unlink()
                    os.mkfifo(self.root / "file.txt")
                else:
                    (self.root / ".git").unlink()
                    os.mkfifo(self.root / ".git")
                binding = (
                    PinnedDirectoryExecutorError("invalid marker")
                    if case == "missing-marker"
                    else self.marker_binding()
                )
                with patch.object(
                    self.repository, "verify_worktree_admin_binding",
                    side_effect=binding if isinstance(binding, Exception) else None,
                    return_value=None if isinstance(binding, Exception) else binding,
                ):
                    with self.assertRaises(WorkspaceTreeVerificationError):
                        verify_workspace_tree(
                            repository=self.repository, workspace=self.workspace,
                            descriptor=self.descriptor, manifest=(entry,),
                        )
                for name in ("file.txt", "extra.txt"):
                    target = self.root / name
                    if target.is_symlink() or target.exists():
                        target.unlink()
                marker = self.root / ".git"
                if not marker.is_file():
                    if marker.exists():
                        marker.unlink()
                    marker.write_text("gitdir: /trusted/admin\n")

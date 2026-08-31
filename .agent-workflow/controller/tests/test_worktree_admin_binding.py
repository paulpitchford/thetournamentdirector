from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from td_controller.exact_worktree_command import build_exact_worktree_command
from td_controller.pinned_directory_executor import (
    PinnedDirectoryExecutor,
    PinnedDirectoryExecutorError,
)
from td_controller.workspace_identity_handle import WorkspaceIdentity

REF_NAME = "refs/heads/agent-orch-test-0123456789abcdef0123456789abcdef"


class WorktreeAdminBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="td-admin-binding-")
        self.parent = Path(self.temporary.name).resolve()
        self.repository_path = self.parent / "repository"
        self.workspace_path = self.parent / "workspace"
        self.repository_path.mkdir(mode=0o755)
        self.workspace_path.mkdir(mode=0o700)
        self.initialize_repository(self.repository_path, "primary")
        self.repository_fd = os.open(
            self.repository_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        self.workspace_fd = os.open(
            self.workspace_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        metadata = os.fstat(self.workspace_fd)
        self.identity = WorkspaceIdentity(
            "ORCH-TEST", 1, "a" * 32, metadata.st_dev, metadata.st_ino
        )
        self.repository = PinnedDirectoryExecutor(descriptor=self.repository_fd)
        command = build_exact_worktree_command(REF_NAME)
        output = self.repository.run_with_workspace_descriptor(
            list(command.argv), environment=command.environment,
            descriptor=self.workspace_fd, expected_identity=self.identity,
        )
        self.assertEqual(output.returncode, 0)

    def tearDown(self) -> None:
        self.repository.close()
        os.close(self.repository_fd)
        os.close(self.workspace_fd)
        self.temporary.cleanup()

    @staticmethod
    def initialize_repository(path: Path, message: str) -> None:
        subprocess.run(["/usr/bin/git", "init", "-q", str(path)], check=True)
        environment = {
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid", "PATH": "/usr/bin:/bin",
        }
        subprocess.run(
            ["/usr/bin/git", "-C", str(path), "commit", "--allow-empty",
             "--no-verify", "-q", "-m", message],
            check=True, env=environment,
        )
        subprocess.run(
            ["/usr/bin/git", "-C", str(path), "update-ref", REF_NAME,
             "HEAD", "0" * 40], check=True,
        )

    def test_binds_marker_admin_entry_and_workspace_backlink(self) -> None:
        binding = self.repository.verify_worktree_admin_binding(
            descriptor=self.workspace_fd, expected_identity=self.identity
        )
        self.assertTrue(binding.admin_name)
        self.assertGreater(binding.admin_inode, 0)
        self.assertGreater(binding.marker_inode, 0)
        self.assertFalse(hasattr(binding, "descriptor"))

    def test_marker_copied_from_another_repository_is_rejected(self) -> None:
        other_repository = self.parent / "other-repository"
        other_workspace = self.parent / "other-workspace"
        other_repository.mkdir(mode=0o755)
        other_workspace.mkdir(mode=0o700)
        self.initialize_repository(other_repository, "other")
        subprocess.run(
            ["/usr/bin/git", "-C", str(other_repository), "worktree", "add",
             "--no-checkout", "--quiet", str(other_workspace),
             REF_NAME.removeprefix("refs/heads/")],
            check=True,
        )
        shutil.copyfile(other_workspace / ".git", self.workspace_path / ".git")
        with self.assertRaisesRegex(PinnedDirectoryExecutorError, "binding"):
            self.repository.verify_worktree_admin_binding(
                descriptor=self.workspace_fd, expected_identity=self.identity
            )

    def test_changed_backlink_and_symlink_marker_are_rejected(self) -> None:
        binding = self.repository.verify_worktree_admin_binding(
            descriptor=self.workspace_fd, expected_identity=self.identity
        )
        backlink = (
            self.repository_path / ".git/worktrees" / binding.admin_name / "gitdir"
        )
        backlink.write_text("/different/workspace/.git\n", encoding="ascii")
        with self.assertRaises(PinnedDirectoryExecutorError):
            self.repository.verify_worktree_admin_binding(
                descriptor=self.workspace_fd, expected_identity=self.identity
            )
        (self.workspace_path / ".git").unlink()
        (self.workspace_path / ".git").symlink_to(backlink)
        with self.assertRaises(PinnedDirectoryExecutorError):
            self.repository.verify_worktree_admin_binding(
                descriptor=self.workspace_fd, expected_identity=self.identity
            )

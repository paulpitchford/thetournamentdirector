from __future__ import annotations

import os
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


class PinnedWorkspaceArgumentTests(unittest.TestCase):
    def test_exact_command_registers_only_in_pinned_workspace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="td-pinned-argument-") as temporary:
            parent = Path(temporary).resolve()
            repository_path = parent / "repository"
            workspace_path = parent / "workspace"
            repository_path.mkdir(mode=0o755)
            workspace_path.mkdir(mode=0o700)
            self.initialize_repository(repository_path)
            repository_fd = os.open(
                repository_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            workspace_fd = os.open(
                workspace_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            metadata = os.fstat(workspace_fd)
            identity = WorkspaceIdentity(
                "ORCH-TEST", 1, "a" * 32,
                metadata.st_dev, metadata.st_ino,
            )
            repository = PinnedDirectoryExecutor(descriptor=repository_fd)
            held = parent / "workspace-held"
            workspace_path.rename(held)
            workspace_path.mkdir(mode=0o700)
            try:
                command = build_exact_worktree_command(REF_NAME)
                output = repository.run_with_workspace_descriptor(
                    list(command.argv), environment=command.environment,
                    descriptor=workspace_fd, expected_identity=identity,
                )
                self.assertEqual(output.returncode, 0)
                self.assertEqual(output.stdout, b"")
                self.assertEqual(output.stderr, b"")
                self.assertTrue((held / ".git").is_file())
                self.assertFalse((workspace_path / ".git").exists())
            finally:
                repository.close()
                os.close(repository_fd)
                os.close(workspace_fd)
                workspace_path.rmdir()
                held.rename(workspace_path)

    def test_wrong_workspace_identity_is_normalized(self) -> None:
        with tempfile.TemporaryDirectory(prefix="td-pinned-argument-") as temporary:
            parent = Path(temporary).resolve()
            repository_fd = os.open(
                parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            workspace_fd = os.open(
                parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            repository = PinnedDirectoryExecutor(descriptor=repository_fd)
            wrong = WorkspaceIdentity("ORCH-TEST", 1, "a" * 32, 0, 0)
            try:
                command = build_exact_worktree_command(REF_NAME)
                with self.assertRaisesRegex(
                    PinnedDirectoryExecutorError, "workspace argument"
                ) as raised:
                    repository.run_with_workspace_descriptor(
                        list(command.argv), environment=command.environment,
                        descriptor=workspace_fd, expected_identity=wrong,
                    )
                self.assertIsNone(raised.exception.__cause__)
                self.assertIsNone(raised.exception.__context__)
            finally:
                repository.close()
                os.close(repository_fd)
                os.close(workspace_fd)

    @staticmethod
    def initialize_repository(path: Path) -> None:
        subprocess.run(["/usr/bin/git", "init", "-q", str(path)], check=True)
        environment = {
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "PATH": "/usr/bin:/bin",
        }
        subprocess.run(
            ["/usr/bin/git", "-C", str(path), "commit", "--allow-empty",
             "--no-verify", "-q", "-m", "seed"],
            check=True, env=environment,
        )
        subprocess.run(
            ["/usr/bin/git", "-C", str(path), "update-ref", REF_NAME,
             "HEAD", "0" * 40],
            check=True,
        )

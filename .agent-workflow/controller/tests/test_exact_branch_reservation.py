from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from td_controller.exact_branch_reservation import (
    ExactBranchIndeterminateError,
    ExactBranchRejectedError,
    reserve_exact_task_branch,
)
from td_controller.pinned_directory_executor import PinnedDirectoryExecutor
from td_controller.workspace_identity_handle import WorkspaceIdentityHandle

GENERATION = "0123456789abcdef0123456789abcdef"


class ExactBranchReservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="td-branch-reservation-")
        self.parent = Path(self.temporary.name).resolve()
        self.repository_path = self.parent / "repository"
        self.repository_path.mkdir(mode=0o755)
        self.commit_sha = self.initialize_repository(self.repository_path, "primary")
        self.workspace_path = self.parent / "workspace"
        self.workspace_path.mkdir(mode=0o700)
        repository_fd = os.open(
            self.repository_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        workspace_fd = os.open(
            self.workspace_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            self.repository = PinnedDirectoryExecutor(descriptor=repository_fd)
            self.workspace = WorkspaceIdentityHandle(
                "ORCH-TEST", attempt=1, generation=GENERATION,
                descriptor=workspace_fd,
            )
        finally:
            os.close(repository_fd)
            os.close(workspace_fd)

    def tearDown(self) -> None:
        self.workspace.close()
        self.repository.close()
        self.temporary.cleanup()

    @staticmethod
    def initialize_repository(path: Path, message: str) -> str:
        subprocess.run(
            ["/usr/bin/git", "init", "-q", str(path)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        environment = {
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "PATH": "/usr/bin:/bin",
        }
        subprocess.run(
            ["/usr/bin/git", "-C", str(path), "commit", "--allow-empty",
             "--no-verify", "-q", "-m", message],
            check=True, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return subprocess.check_output(
            ["/usr/bin/git", "-C", str(path), "rev-parse", "HEAD"],
            text=True,
        ).strip()

    def branch_ref(self) -> str:
        return f"refs/heads/agent-orch-test-{GENERATION}"

    def direct_ref(self, path: Path) -> str | None:
        result = subprocess.run(
            ["/usr/bin/git", "-C", str(path), "show-ref", "--hash", "--verify",
             self.branch_ref()],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if result.returncode in (1, 128) and not result.stdout:
            return None
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.decode("ascii").strip()

    def test_reserves_derived_direct_branch_and_suppresses_hooks(self) -> None:
        marker = self.parent / "hook-ran"
        hook = self.repository_path / ".git/hooks/reference-transaction"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        hook.chmod(0o700)
        subprocess.run(
            ["/usr/bin/git", "-C", str(self.repository_path), "config",
             "core.hooksPath", str(hook.parent)],
            check=True,
        )
        reserved = reserve_exact_task_branch(
            repository=self.repository,
            workspace=self.workspace,
            commit_sha=self.commit_sha,
        )
        self.assertEqual(reserved.ref_name, self.branch_ref())
        self.assertEqual(self.direct_ref(self.repository_path), self.commit_sha)
        self.assertFalse(marker.exists())
        with self.assertRaises(ExactBranchIndeterminateError):
            reserve_exact_task_branch(
                repository=self.repository,
                workspace=self.workspace,
                commit_sha=self.commit_sha,
            )

    def test_repository_path_replacement_cannot_redirect_reservation(self) -> None:
        replacement = self.parent / "replacement"
        self.initialize_repository(replacement, "replacement")
        held = self.parent / "repository-held"
        self.repository_path.rename(held)
        replacement.rename(self.repository_path)
        reserve_exact_task_branch(
            repository=self.repository,
            workspace=self.workspace,
            commit_sha=self.commit_sha,
        )
        self.assertEqual(self.direct_ref(held), self.commit_sha)
        self.assertIsNone(self.direct_ref(self.repository_path))

    def test_symbolic_existing_ref_is_indeterminate(self) -> None:
        subprocess.run(
            ["/usr/bin/git", "-C", str(self.repository_path), "branch", "other"],
            check=True,
        )
        subprocess.run(
            ["/usr/bin/git", "-C", str(self.repository_path), "symbolic-ref",
             self.branch_ref(), "refs/heads/other"],
            check=True,
        )
        with self.assertRaises(ExactBranchIndeterminateError):
            reserve_exact_task_branch(
                repository=self.repository,
                workspace=self.workspace,
                commit_sha=self.commit_sha,
            )

    def test_invalid_sha_and_closed_workspace_fail_without_effect(self) -> None:
        with self.assertRaises(ExactBranchRejectedError):
            reserve_exact_task_branch(
                repository=self.repository,
                workspace=self.workspace,
                commit_sha="not-a-sha",
            )
        with self.assertRaises(ExactBranchRejectedError):
            reserve_exact_task_branch(
                repository=self.repository,
                workspace=self.workspace,
                commit_sha="f" * 40,
            )
        self.workspace.close()
        with self.assertRaises(ExactBranchIndeterminateError):
            reserve_exact_task_branch(
                repository=self.repository,
                workspace=self.workspace,
                commit_sha=self.commit_sha,
            )
        self.assertIsNone(self.direct_ref(self.repository_path))

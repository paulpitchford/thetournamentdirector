from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from td_controller.exact_branch_reservation import reserve_exact_task_branch
from td_controller.pinned_directory_executor import PinnedDirectoryExecutor
from td_controller.workspace_identity_handle import WorkspaceIdentityHandle
from td_controller.worktree_registration import (
    WorktreeRegistrationIndeterminateError,
    WorktreeRegistrationRejectedError,
    register_no_checkout_worktree,
)

GENERATION = "0123456789abcdef0123456789abcdef"


class WorktreeRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="td-worktree-register-")
        self.parent = Path(self.temporary.name).resolve()
        self.repo_path = self.parent / "repository"
        self.workspace_path = self.parent / "workspace"
        self.repo_path.mkdir(mode=0o755)
        self.workspace_path.mkdir(mode=0o700)
        self.commit_sha = self.initialize_repository(self.repo_path)
        self.repo_fd = os.open(
            self.repo_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        self.workspace_fd = os.open(
            self.workspace_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        self.repository = PinnedDirectoryExecutor(descriptor=self.repo_fd)
        self.workspace = WorkspaceIdentityHandle(
            "ORCH-TEST", attempt=1, generation=GENERATION,
            descriptor=self.workspace_fd,
        )
        self.reserved = reserve_exact_task_branch(
            repository=self.repository, workspace=self.workspace,
            commit_sha=self.commit_sha,
        )

    def tearDown(self) -> None:
        self.workspace.close()
        self.repository.close()
        os.close(self.repo_fd)
        os.close(self.workspace_fd)
        self.temporary.cleanup()

    @staticmethod
    def initialize_repository(path: Path) -> str:
        subprocess.run(["/usr/bin/git", "init", "-q", str(path)], check=True)
        (path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        environment = {
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid", "PATH": "/usr/bin:/bin",
        }
        subprocess.run(
            ["/usr/bin/git", "-C", str(path), "add", "tracked.txt"],
            check=True, env=environment,
        )
        subprocess.run(
            ["/usr/bin/git", "-C", str(path), "commit", "--no-verify", "-q",
             "-m", "seed"], check=True, env=environment,
        )
        return subprocess.check_output(
            ["/usr/bin/git", "-C", str(path), "rev-parse", "HEAD"], text=True
        ).strip()

    def register(self):
        return register_no_checkout_worktree(
            repository=self.repository, workspace=self.workspace,
            workspace_descriptor=self.workspace_fd,
            reserved_branch=self.reserved,
        )

    def test_registers_exact_no_checkout_state_and_rejects_replay(self) -> None:
        hook_marker = self.parent / "hook-ran"
        hook = self.repo_path / ".git/hooks/post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch {hook_marker}\n", encoding="utf-8")
        hook.chmod(0o700)
        registered = self.register()
        self.assertEqual(registered.ref_name, self.reserved.ref_name)
        self.assertEqual(registered.commit_sha, self.commit_sha)
        self.assertEqual(registered.workspace, self.workspace.identity)
        self.assertEqual(os.listdir(self.workspace_path), [".git"])
        self.assertFalse((self.workspace_path / "tracked.txt").exists())
        self.assertFalse(hook_marker.exists())
        with self.assertRaises(WorktreeRegistrationIndeterminateError):
            self.register()

    def test_dual_path_replacement_cannot_redirect_registration(self) -> None:
        held_repo = self.parent / "repository-held"
        held_workspace = self.parent / "workspace-held"
        self.repo_path.rename(held_repo)
        self.workspace_path.rename(held_workspace)
        self.repo_path.mkdir(mode=0o755)
        self.workspace_path.mkdir(mode=0o700)
        self.register()
        self.assertTrue((held_workspace / ".git").is_file())
        self.assertFalse((self.workspace_path / ".git").exists())

    def test_repository_registration_without_marker_is_indeterminate(self) -> None:
        other = self.parent / "other-worktree"
        subprocess.run(
            [
                "/usr/bin/git", "-C", str(self.repo_path), "-c",
                "core.hooksPath=/dev/null", "worktree", "add", "--no-checkout",
                "--quiet", str(other),
                self.reserved.ref_name.removeprefix("refs/heads/"),
            ],
            check=True,
        )
        shutil.copyfile(other / ".git", self.workspace_path / ".git")
        with self.assertRaises(WorktreeRegistrationIndeterminateError):
            self.register()
        self.assertEqual(os.listdir(self.workspace_path), [".git"])

    def test_missing_branch_and_wrong_descriptor_fail_without_effect(self) -> None:
        subprocess.run(
            ["/usr/bin/git", "-C", str(self.repo_path), "update-ref", "-d",
             self.reserved.ref_name], check=True,
        )
        with self.assertRaises(WorktreeRegistrationRejectedError):
            self.register()
        with self.assertRaises(WorktreeRegistrationIndeterminateError):
            register_no_checkout_worktree(
                repository=self.repository, workspace=self.workspace,
                workspace_descriptor=self.repo_fd,
                reserved_branch=self.reserved,
            )
        self.assertEqual(os.listdir(self.workspace_path), [])

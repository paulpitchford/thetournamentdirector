from __future__ import annotations
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from td_controller.lease import LeaseError, TaskLease
from td_controller.workflow_state import TaskState
from td_controller.worktree import (
    AmbiguousGitError,
    MetadataWorktreeManager,
    WorktreeError,
)
class MetadataWorktreeManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(dir="/var/tmp")
        self.root = Path(self.temporary_directory.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.git("init", "--initial-branch=main")
        (self.repository / "README.md").write_text("reviewed\n")
        self.git("add", "README.md")
        self.git(
            "-c", "user.name=Controller Test", "-c",
            "user.email=controller@example.invalid", "commit", "-m", "base",
        )
        self.base_sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.worktrees = self.root / "worktrees"
        self.lease = TaskLease(
            lease_id="lease-worktree-0001", task_id="DOC-001",
            branch="agent/doc-001/attempt-1", owner="controller-primary",
            state="ACTIVE", acquired_at="2026-08-29T00:00:00+00:00",
            heartbeat_at="2026-08-29T00:00:00+00:00",
            expires_at="2026-08-29T01:00:00+00:00", released_at=None,
        )
        self.lease_ledger = Mock()
        self.lease_ledger.heartbeat.return_value = self.lease
        self.state_ledger = Mock()
        self.state_ledger.current.return_value = TaskState(
            task_id="DOC-001", state="QUEUED", revision=1, attempt=1,
            max_attempts=2, base_sha=self.base_sha, head_sha=self.base_sha,
            updated_at="2026-08-29T00:00:00+00:00",
        )

    def manager(self, root: Path | None = None) -> MetadataWorktreeManager:
        return MetadataWorktreeManager(
            self.repository, root or self.worktrees,
            lease_ledger=self.lease_ledger, state_ledger=self.state_ledger,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/usr/bin/git", "-C", str(self.repository), *arguments],
            check=True, capture_output=True, text=True,
        )

    def test_reservation_is_empty_branch_bound_and_runs_no_checkout_hook(self) -> None:
        hook = self.repository / ".git/hooks/post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch '{self.root / 'hook-ran'}'\n")
        hook.chmod(0o700)
        manager = self.manager()

        reservation = manager.reserve(self.lease, attempt=1, base_sha=self.base_sha)

        self.assertEqual(tuple(reservation.path.iterdir()), (reservation.path / ".git",))
        self.assertEqual(reservation.branch, self.lease.branch)
        self.assertEqual(self.git("rev-parse", self.lease.branch).stdout.strip(), self.base_sha)
        self.assertFalse((self.root / "hook-ran").exists())
        self.assertFalse((reservation.path / "README.md").exists())

    def test_replaced_git_directory_is_rejected_before_mutation(self) -> None:
        manager = self.manager()
        git_directory = self.repository / ".git"
        original = self.repository / ".git-original"
        git_directory.rename(original)
        git_directory.mkdir()
        with self.assertRaisesRegex(WorktreeError, "identity changed"):
            manager.reserve(self.lease, attempt=1, base_sha=self.base_sha)
        self.assertEqual(tuple(self.worktrees.iterdir()), ())
        git_directory.rmdir()
        original.rename(git_directory)

    def test_ambiguous_branch_creation_is_quarantined_without_deletion(self) -> None:
        manager = self.manager()
        original = manager._run_git

        def ambiguous(*arguments: str):
            result = original(*arguments)
            if arguments[0] == "update-ref":
                raise AmbiguousGitError("ambiguous update")
            return result

        with patch.object(manager, "_run_git", side_effect=ambiguous):
            with self.assertRaisesRegex(WorktreeError, "ambiguous"):
                manager.reserve(self.lease, attempt=1, base_sha=self.base_sha)
        self.assertIn(self.lease.branch, self.git("branch", "--list").stdout)
        self.assertTrue((self.worktrees / ".doc-001-attempt-1.reservation").is_dir())

    def test_post_creation_failure_rolls_back_branch_path_and_registration(self) -> None:
        manager = self.manager()
        with patch.object(Path, "iterdir", side_effect=OSError("inspection failed")):
            with self.assertRaisesRegex(WorktreeError, "inspection failed"):
                manager.reserve(self.lease, attempt=1, base_sha=self.base_sha)

        self.assertFalse((self.worktrees / "doc-001-attempt-1").exists())
        self.assertNotIn(self.lease.branch, self.git("branch", "--list").stdout)
        reservation = manager.reserve(self.lease, attempt=1, base_sha=self.base_sha)
        self.assertTrue((reservation.path / ".git").is_file())

    def test_authoritative_state_and_lease_are_rechecked(self) -> None:
        manager = self.manager()
        state = self.state_ledger.current.return_value
        self.state_ledger.current.return_value = TaskState(
            **{**state.__dict__, "base_sha": "c" * 40}
        )
        with self.assertRaisesRegex(WorktreeError, "does not authorize"):
            manager.reserve(self.lease, attempt=1, base_sha=self.base_sha)
        self.state_ledger.current.return_value = state
        self.lease_ledger.heartbeat.side_effect = [self.lease, LeaseError("expired")]
        with self.assertRaisesRegex(WorktreeError, "authority or inspection"):
            manager.reserve(self.lease, attempt=1, base_sha=self.base_sha)
        self.assertEqual(tuple(self.worktrees.iterdir()), ())
        self.assertNotIn(self.lease.branch, self.git("branch", "--list").stdout)

    def test_replaced_storage_root_is_rejected_before_git_mutation(self) -> None:
        manager = self.manager()
        original = self.root / "renamed-worktrees"
        destination = self.root / "destination"
        self.worktrees.rename(original)
        destination.mkdir()
        self.worktrees.symlink_to(destination, target_is_directory=True)

        with self.assertRaisesRegex(WorktreeError, "permissions are unsafe"):
            manager.reserve(self.lease, attempt=1, base_sha=self.base_sha)

        self.assertEqual(tuple(destination.iterdir()), ())
        self.worktrees.unlink()
        original.rename(self.worktrees)


if __name__ == "__main__":
    unittest.main()

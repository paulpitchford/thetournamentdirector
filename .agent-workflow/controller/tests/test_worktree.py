"""Tests for metadata-only task worktree reservation."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from td_controller.lease import TaskLease
from td_controller.worktree import MetadataWorktreeManager, WorktreeError


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
        manager = MetadataWorktreeManager(self.repository, self.worktrees)

        reservation = manager.reserve(self.lease, attempt=1, base_sha=self.base_sha)

        self.assertEqual(tuple(reservation.path.iterdir()), (reservation.path / ".git",))
        self.assertEqual(reservation.branch, self.lease.branch)
        self.assertEqual(self.git("rev-parse", self.lease.branch).stdout.strip(), self.base_sha)
        self.assertFalse((self.root / "hook-ran").exists())
        self.assertFalse((reservation.path / "README.md").exists())

    def test_duplicate_branch_or_path_is_rejected_without_reuse(self) -> None:
        manager = MetadataWorktreeManager(self.repository, self.worktrees)
        first = manager.reserve(self.lease, attempt=1, base_sha=self.base_sha)

        with self.assertRaisesRegex(WorktreeError, "already reserved"):
            manager.reserve(self.lease, attempt=1, base_sha=self.base_sha)

        self.assertTrue((first.path / ".git").is_file())

    def test_lease_branch_state_attempt_and_sha_are_exact(self) -> None:
        manager = MetadataWorktreeManager(self.repository, self.worktrees)
        invalid = (
            (self.lease.__class__(**{**self.lease.__dict__, "state": "EXPIRED"}), 1, self.base_sha),
            (
                self.lease.__class__(
                    **{**self.lease.__dict__, "branch": "agent/other/attempt-1"}
                ),
                1,
                self.base_sha,
            ),
            (self.lease, 2, self.base_sha),
            (self.lease, 1, "A" * 40),
        )
        for lease, attempt, sha in invalid:
            with self.subTest(lease=lease, attempt=attempt, sha=sha):
                with self.assertRaises(WorktreeError):
                    manager.reserve(lease, attempt=attempt, base_sha=sha)

        self.assertEqual(tuple(self.worktrees.iterdir()), ())

    def test_roots_must_be_disjoint_private_and_self_contained(self) -> None:
        with self.assertRaisesRegex(WorktreeError, "overlaps"):
            MetadataWorktreeManager(self.repository, self.repository / "nested")
        unsafe = self.root / "unsafe"
        unsafe.mkdir(mode=0o755)
        with self.assertRaisesRegex(WorktreeError, "permissions"):
            MetadataWorktreeManager(self.repository, unsafe)
        external = self.root / "external-git"
        external.mkdir()
        linked = self.root / "linked"
        linked.mkdir()
        (linked / ".git").write_text(f"gitdir: {external}\n")
        with self.assertRaisesRegex(WorktreeError, "self-contained"):
            MetadataWorktreeManager(linked, self.root / "linked-worktrees")

    def test_unknown_base_is_rejected_before_target_creation(self) -> None:
        manager = MetadataWorktreeManager(self.repository, self.worktrees)

        with self.assertRaisesRegex(WorktreeError, "rejected"):
            manager.reserve(self.lease, attempt=1, base_sha="f" * 40)

        self.assertEqual(tuple(self.worktrees.iterdir()), ())


if __name__ == "__main__":
    unittest.main()

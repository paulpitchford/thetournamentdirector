from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from td_controller.git_reservation import (
    GIT,
    GitReservationError,
    _environment,
    reserve_git_branch,
)
from td_controller.workspace_identity_handle import WorkspaceIdentityHandle


class GitReservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="td-git-reservation-")
        self.root = Path(self.temporary.name)
        environment = _environment() | {
            "GIT_AUTHOR_NAME": "TD", "GIT_AUTHOR_EMAIL": "td@example.invalid",
            "GIT_COMMITTER_NAME": "TD", "GIT_COMMITTER_EMAIL": "td@example.invalid",
        }
        self.environment = environment
        subprocess.run([GIT, "init", "-q", str(self.root)], check=True, env=environment)
        (self.root / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run([GIT, "add", "tracked.txt"], cwd=self.root, check=True, env=environment)
        subprocess.run(
            [GIT, "commit", "-q", "-m", "base"], cwd=self.root,
            check=True, env=environment,
        )
        self.base = subprocess.check_output(
            [GIT, "rev-parse", "HEAD"], cwd=self.root, env=_environment()
        ).decode().strip()
        workspace = self.root / "workspace"
        workspace.mkdir(mode=0o700)
        descriptor = os.open(
            workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            self.handle = WorkspaceIdentityHandle(
                "ORCH-003D1B0K2", attempt=1, generation="a" * 32,
                descriptor=descriptor,
            )
        finally:
            os.close(descriptor)

    def tearDown(self) -> None:
        self.handle.close()
        self.temporary.cleanup()

    def test_reservation_is_derived_atomic_and_does_not_move_head(self) -> None:
        before = subprocess.check_output(
            [GIT, "symbolic-ref", "HEAD"], cwd=self.root, env=_environment()
        )

        reservation = reserve_git_branch(
            self.root, self.handle, base_sha=self.base
        )

        self.assertEqual(
            reservation.ref_name,
            "refs/heads/agent-orch-003d1b0k2-" + "a" * 32,
        )
        value = subprocess.check_output(
            [GIT, "rev-parse", reservation.ref_name], cwd=self.root,
            env=_environment(),
        ).decode().strip()
        after = subprocess.check_output(
            [GIT, "symbolic-ref", "HEAD"], cwd=self.root, env=_environment()
        )
        self.assertEqual(value, self.base)
        self.assertEqual(after, before)

    def test_duplicate_reservation_fails_without_moving_ref(self) -> None:
        reservation = reserve_git_branch(
            self.root, self.handle, base_sha=self.base
        )
        with self.assertRaisesRegex(GitReservationError, "not created"):
            reserve_git_branch(self.root, self.handle, base_sha=self.base)
        value = subprocess.check_output(
            [GIT, "rev-parse", reservation.ref_name], cwd=self.root,
            env=_environment(),
        ).decode().strip()
        self.assertEqual(value, self.base)

    def test_invalid_or_missing_base_fails_before_ref_creation(self) -> None:
        for base in ("0" * 40, "bad"):
            with self.subTest(base=base):
                with self.assertRaises(GitReservationError):
                    reserve_git_branch(self.root, self.handle, base_sha=base)
        refs = subprocess.check_output(
            [GIT, "for-each-ref", "refs/heads/agent"], cwd=self.root,
            env=_environment(),
        )
        self.assertEqual(refs, b"")

    def test_annotated_tag_object_is_not_accepted_as_base_commit(self) -> None:
        subprocess.run(
            [GIT, "tag", "-a", "base-tag", "-m", "tag"], cwd=self.root,
            check=True, env=self.environment,
        )
        tag_sha = subprocess.check_output(
            [GIT, "rev-parse", "base-tag^{tag}"], cwd=self.root,
            env=_environment(),
        ).decode().strip()
        with self.assertRaisesRegex(GitReservationError, "format or base"):
            reserve_git_branch(self.root, self.handle, base_sha=tag_sha)

    def test_symlinked_ref_parent_cannot_redirect_write(self) -> None:
        heads = self.root / ".git" / "refs" / "heads"
        held = heads.with_name("held-heads")
        external = self.root / "external"
        external.mkdir()
        heads.rename(held)
        heads.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(GitReservationError, "layout"):
            reserve_git_branch(self.root, self.handle, base_sha=self.base)
        self.assertEqual(tuple(external.iterdir()), ())

    def test_object_alternates_are_rejected_before_ref_creation(self) -> None:
        info = self.root / ".git" / "objects" / "info"
        info.mkdir(exist_ok=True)
        (info / "alternates").write_text("/external/objects\n", encoding="utf-8")
        with self.assertRaisesRegex(GitReservationError, "layout"):
            reserve_git_branch(self.root, self.handle, base_sha=self.base)
        refs = subprocess.check_output(
            [GIT, "for-each-ref", "refs/heads/agent"], cwd=self.root,
            env=_environment(),
        )
        self.assertEqual(refs, b"")

    def test_exact_ref_filesystem_symlink_cannot_redirect(self) -> None:
        name = "agent-orch-003d1b0k2-" + "a" * 32
        ref_path = self.root / ".git" / "refs" / "heads" / name
        external = self.root / "external-ref"
        external.write_text("sentinel\n", encoding="utf-8")
        ref_path.symlink_to(external)
        with self.assertRaisesRegex(GitReservationError, "not created"):
            reserve_git_branch(self.root, self.handle, base_sha=self.base)
        self.assertTrue(ref_path.is_symlink())
        self.assertEqual(external.read_text(encoding="utf-8"), "sentinel\n")

    def test_repository_hook_and_symbolic_ref_cannot_redirect(self) -> None:
        ref = "refs/heads/agent-orch-003d1b0k2-" + "a" * 32
        hooks = self.root / "hooks"
        hooks.mkdir()
        marker = self.root / "hook-ran"
        hook = hooks / "reference-transaction"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        hook.chmod(0o700)
        subprocess.run(
            [GIT, "config", "core.hooksPath", str(hooks)], cwd=self.root,
            check=True, env=self.environment,
        )
        target = "refs/heads/unintended"
        subprocess.run(
            [GIT, "symbolic-ref", ref, target], cwd=self.root,
            check=True, env=self.environment,
        )
        marker.unlink(missing_ok=True)
        with self.assertRaisesRegex(GitReservationError, "not created"):
            reserve_git_branch(self.root, self.handle, base_sha=self.base)
        self.assertFalse(marker.exists())
        self.assertNotEqual(
            subprocess.run(
                [GIT, "show-ref", "--verify", target], cwd=self.root,
                env=self.environment, check=False, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode,
            0,
        )

    def test_closed_handle_cannot_authorize_reservation(self) -> None:
        self.handle.close()
        with self.assertRaisesRegex(GitReservationError, "unavailable"):
            reserve_git_branch(self.root, self.handle, base_sha=self.base)

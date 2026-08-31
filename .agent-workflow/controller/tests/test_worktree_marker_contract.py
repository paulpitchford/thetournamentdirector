from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from pathlib import PurePosixPath

from td_controller.worktree_marker_contract import (
    WorktreeMarkerContractError,
    WorktreeMarkerTarget,
    parse_worktree_marker,
)


class WorktreeMarkerContractTests(unittest.TestCase):
    def test_canonical_marker_returns_frozen_non_authority_evidence(self) -> None:
        target = parse_worktree_marker(
            b"gitdir: /controller/repository/.git/worktrees/task-1\n"
        )
        self.assertEqual(
            target,
            WorktreeMarkerTarget(
                PurePosixPath(
                    "/controller/repository/.git/worktrees/task-1"
                ),
                "task-1",
            ),
        )
        self.assertFalse(hasattr(target, "descriptor"))
        with self.assertRaises(FrozenInstanceError):
            target.admin_name = "changed"

    def test_malformed_ambiguous_and_unbounded_markers_fail_closed(self) -> None:
        invalid = (
            b"", b"gitdir: relative/.git/worktrees/task\n",
            b"gitdir: /repo/.git/worktrees/task",
            b"gitdir: /repo/.git/worktrees/task\nextra\n",
            b"gitdir: /repo/.git/worktrees/../task\n",
            b"gitdir: /repo/.git/modules/task\n",
            b"gitdir: /repo/.git/worktrees/-task\n",
            b"gitdir: /repo/.git/worktrees/task name\n",
            b"gitdir: /repo/.git/worktrees/task\x00other\n",
            b"gitdir: /" + b"a" * 4090 + b"\n",
        )
        for payload in invalid:
            with self.subTest(payload=payload[:80]):
                with self.assertRaises(WorktreeMarkerContractError):
                    parse_worktree_marker(payload)

    def test_text_and_mutable_inputs_are_rejected(self) -> None:
        for payload in (
            "gitdir: /repo/.git/worktrees/task\n",
            bytearray(b"gitdir: /repo/.git/worktrees/task\n"),
        ):
            with self.assertRaises(WorktreeMarkerContractError):
                parse_worktree_marker(payload)

"""Tests for mutation-detecting read-only planning trials."""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from td_controller.codex_planner import PlannerRequest
from td_controller.planning_trial import (
    MAX_BACKLOG_BYTES,
    PlanningTrialError,
    RepositorySnapshot,
    load_reviewed_backlog,
    planner_contract_context,
    repository_snapshot,
    run_planning_trial,
)
from td_controller.provider import ProviderResult

BASE_SHA = "a" * 40
BACKLOG = "# backlog\n"


def request() -> PlannerRequest:
    return PlannerRequest(
        plan_id="PLAN-TRIAL-001",
        base_sha=BASE_SHA,
        backlog_sha256=hashlib.sha256(BACKLOG.encode()).hexdigest(),
        backlog=BACKLOG,
        planning_context=planner_contract_context(),
        known_task_ids=frozenset({"HUM-001", "HUM-002"}),
    )


class FakePlanner:
    def __init__(
        self,
        result: ProviderResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result or ProviderResult("{}", "planner-session")
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def run(self, *, task_id: str, role: str) -> ProviderResult:
        self.calls.append((task_id, role))
        if self.error is not None:
            raise self.error
        return self.result


class SnapshotSequence:
    def __init__(self, *values: RepositorySnapshot) -> None:
        self.values = list(values)

    def __call__(self, _: Path) -> RepositorySnapshot:
        return self.values.pop(0)


class PlanningTrialTests(unittest.TestCase):
    def test_reviewed_backlog_loader_accepts_only_bounded_regular_direct_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "docs").mkdir()
            backlog = root / "docs" / "DELIVERY_BACKLOG.md"
            backlog.write_text(BACKLOG)
            self.assertEqual(load_reviewed_backlog(root), BACKLOG)

            backlog.unlink()
            backlog.symlink_to(root / "missing")
            with self.assertRaisesRegex(PlanningTrialError, "unavailable"):
                load_reviewed_backlog(root)

    def test_reviewed_backlog_size_and_utf8_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "docs").mkdir()
            backlog = root / "docs" / "DELIVERY_BACKLOG.md"
            backlog.write_bytes(b"x" * (MAX_BACKLOG_BYTES + 1))
            with self.assertRaisesRegex(PlanningTrialError, "size limit"):
                load_reviewed_backlog(root)
            backlog.write_bytes(b"\xff")
            with self.assertRaisesRegex(PlanningTrialError, "UTF-8"):
                load_reviewed_backlog(root)

    def test_clean_exact_snapshot_returns_trial_record(self) -> None:
        clean = RepositorySnapshot(BASE_SHA, b"")
        planner = FakePlanner()

        record = run_planning_trial(
            request(),
            repository_root=Path("/trusted/repository"),
            provider_factory=lambda _: planner,
            snapshot=SnapshotSequence(clean, clean),
        )

        self.assertEqual(record.session_id, "planner-session")
        self.assertEqual(record.plan_id, "PLAN-TRIAL-001")
        self.assertEqual(planner.calls, [("PLAN-TRIAL-001", "planner")])

    def test_dirty_or_stale_repository_prevents_provider_execution(self) -> None:
        for before in (
            RepositorySnapshot("b" * 40, b""),
            RepositorySnapshot(BASE_SHA, b"?? unexpected"),
        ):
            planner = FakePlanner()
            with self.assertRaisesRegex(PlanningTrialError, "exact clean"):
                run_planning_trial(
                    request(),
                    repository_root=Path("/trusted/repository"),
                    provider_factory=lambda _: planner,
                    snapshot=SnapshotSequence(before),
                )
            self.assertEqual(planner.calls, [])

    def test_repository_mutation_is_rejected_after_success_or_failure(self) -> None:
        clean = RepositorySnapshot(BASE_SHA, b"")
        changed = RepositorySnapshot(BASE_SHA, b" M changed")
        for planner in (FakePlanner(), FakePlanner(error=RuntimeError("failed"))):
            with self.assertRaisesRegex(PlanningTrialError, "changed repository"):
                run_planning_trial(
                    request(),
                    repository_root=Path("/trusted/repository"),
                    provider_factory=lambda _, value=planner: value,
                    snapshot=SnapshotSequence(clean, changed),
                )

    def test_provider_failure_is_normalized_after_clean_recheck(self) -> None:
        clean = RepositorySnapshot(BASE_SHA, b"")
        with self.assertRaisesRegex(PlanningTrialError, "contained planner failed"):
            run_planning_trial(
                request(),
                repository_root=Path("/trusted/repository"),
                provider_factory=lambda _: FakePlanner(error=RuntimeError("secret")),
                snapshot=SnapshotSequence(clean, clean),
            )

    def test_missing_session_identity_is_rejected(self) -> None:
        clean = RepositorySnapshot(BASE_SHA, b"")
        planner = FakePlanner(ProviderResult("{}", None))
        with self.assertRaisesRegex(PlanningTrialError, "session identity"):
            run_planning_trial(
                request(),
                repository_root=Path("/trusted/repository"),
                provider_factory=lambda _: planner,
                snapshot=SnapshotSequence(clean, clean),
            )

    def test_repository_snapshot_observes_head_and_untracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {
                "HOME": temporary,
                "PATH": "/usr/bin:/bin",
                "GIT_AUTHOR_NAME": "test",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "test",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
            }
            subprocess.run(["/usr/bin/git", "init", "-q"], cwd=root,
                           env=environment, check=True)
            (root / "tracked").write_text("value")
            subprocess.run(["/usr/bin/git", "add", "tracked"], cwd=root,
                           env=environment, check=True)
            subprocess.run(["/usr/bin/git", "commit", "-qm", "initial"], cwd=root,
                           env=environment, check=True)

            clean = repository_snapshot(root)
            self.assertEqual(clean.worktree_status, b"")
            self.assertEqual(len(clean.head_sha), 40)
            (root / "untracked").write_text("new")
            self.assertIn(b"untracked", repository_snapshot(root).worktree_status)

    def test_context_exposes_strict_proposal_constraints(self) -> None:
        context = planner_contract_context()

        self.assertEqual(context["status"], "PROPOSED")
        self.assertIs(context["humanApprovalRequired"], True)
        self.assertIn("ASSERT TEST_PASS", context["criterionGrammar"])
        self.assertIn("HUM-001", context["completedKnownTasks"])


if __name__ == "__main__":
    unittest.main()

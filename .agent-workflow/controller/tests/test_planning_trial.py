"""Tests for mutation-detecting read-only planning trials."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


def initialize_repository(root: Path, backlog: bytes) -> str:
    environment = {
        "HOME": str(root),
        "PATH": "/usr/bin:/bin",
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    subprocess.run(["/usr/bin/git", "init", "-q"], cwd=root,
                   env=environment, check=True)
    (root / "docs").mkdir()
    (root / "docs" / "DELIVERY_BACKLOG.md").write_bytes(backlog)
    subprocess.run(["/usr/bin/git", "add", "docs/DELIVERY_BACKLOG.md"], cwd=root,
                   env=environment, check=True)
    subprocess.run(["/usr/bin/git", "commit", "-qm", "backlog"], cwd=root,
                   env=environment, check=True)
    return subprocess.check_output(
        ["/usr/bin/git", "rev-parse", "HEAD"], cwd=root, env=environment,
        text=True,
    ).strip()


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


def run_fake_trial(
    planner: FakePlanner, *snapshots: RepositorySnapshot
) -> tuple[object, object]:
    with (
        patch(
            "td_controller.planning_trial.repository_snapshot",
            side_effect=snapshots,
        ),
        patch(
            "td_controller.planning_trial.load_reviewed_backlog",
            return_value=BACKLOG,
        ),
        patch(
            "td_controller.planning_trial.CodexPlannerProvider",
            return_value=planner,
        ) as factory,
    ):
        record = run_planning_trial(
            "PLAN-TRIAL-001",
            repository_root=Path.cwd(),
            expected_base_sha=BASE_SHA,
            known_task_ids=frozenset({"HUM-001", "HUM-002"}),
        )
    return record, factory


class PlanningTrialTests(unittest.TestCase):
    def test_reviewed_backlog_is_loaded_from_approved_commit_not_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base_sha = initialize_repository(root, BACKLOG.encode())
            backlog = root / "docs" / "DELIVERY_BACKLOG.md"
            backlog.unlink()
            backlog.symlink_to(root / "missing")

            self.assertEqual(load_reviewed_backlog(root, base_sha), BACKLOG)

    def test_reviewed_backlog_size_and_utf8_are_bounded(self) -> None:
        cases = (
            (b"x" * (MAX_BACKLOG_BYTES + 1), "size limit"),
            (b"\xff", "UTF-8"),
        )
        for payload, error in cases:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                base_sha = initialize_repository(root, payload)
                with self.assertRaisesRegex(PlanningTrialError, error):
                    load_reviewed_backlog(root, base_sha)

    def test_clean_snapshot_builds_request_from_controller_inputs(self) -> None:
        clean = RepositorySnapshot(BASE_SHA, b"")
        planner = FakePlanner()

        record, factory = run_fake_trial(planner, clean, clean)

        self.assertEqual(record.session_id, "planner-session")
        self.assertEqual(record.plan_id, "PLAN-TRIAL-001")
        self.assertEqual(planner.calls, [("PLAN-TRIAL-001", "planner")])
        request = factory.call_args.args[0]
        self.assertEqual(request.backlog, BACKLOG)
        self.assertEqual(request.planning_context, planner_contract_context())
        self.assertEqual(request.base_sha, BASE_SHA)
        executor = factory.call_args.kwargs["executor"]
        self.assertEqual(executor._inaccessible_paths, (Path.cwd().resolve(),))

    def test_dirty_repository_prevents_provider_construction(self) -> None:
        dirty = RepositorySnapshot(BASE_SHA, b"?? unexpected")
        with (
            patch(
                "td_controller.planning_trial.repository_snapshot",
                return_value=dirty,
            ),
            patch("td_controller.planning_trial.CodexPlannerProvider") as factory,
        ):
            with self.assertRaisesRegex(PlanningTrialError, "exact clean"):
                run_planning_trial(
                    "PLAN-TRIAL-001",
                    repository_root=Path.cwd(),
                    expected_base_sha=BASE_SHA,
                    known_task_ids=frozenset(),
                )
        factory.assert_not_called()

    def test_stale_or_malformed_approved_base_prevents_backlog_loading(self) -> None:
        clean = RepositorySnapshot(BASE_SHA, b"")
        for expected in ("b" * 40, "INVALID"):
            with (
                patch(
                    "td_controller.planning_trial.repository_snapshot",
                    return_value=clean,
                ),
                patch(
                    "td_controller.planning_trial.load_reviewed_backlog"
                ) as loader,
                patch("td_controller.planning_trial.CodexPlannerProvider") as factory,
            ):
                with self.assertRaisesRegex(PlanningTrialError, "revision"):
                    run_planning_trial(
                        "PLAN-TRIAL-001",
                        repository_root=Path.cwd(),
                        expected_base_sha=expected,
                        known_task_ids=frozenset(),
                    )
            loader.assert_not_called()
            factory.assert_not_called()

    def test_repository_mutation_is_rejected_after_success_or_failure(self) -> None:
        clean = RepositorySnapshot(BASE_SHA, b"")
        changed = RepositorySnapshot(BASE_SHA, b" M changed")
        for planner in (FakePlanner(), FakePlanner(error=RuntimeError("failed"))):
            with self.assertRaisesRegex(PlanningTrialError, "changed repository"):
                run_fake_trial(planner, clean, changed)

    def test_factory_mutation_is_always_rechecked(self) -> None:
        clean = RepositorySnapshot(BASE_SHA, b"")
        changed = RepositorySnapshot(BASE_SHA, b" M factory-change")
        with (
            patch(
                "td_controller.planning_trial.repository_snapshot",
                side_effect=(clean, changed),
            ),
            patch(
                "td_controller.planning_trial.load_reviewed_backlog",
                return_value=BACKLOG,
            ),
            patch(
                "td_controller.planning_trial.CodexPlannerProvider",
                side_effect=RuntimeError("factory failed"),
            ),
        ):
            with self.assertRaisesRegex(PlanningTrialError, "changed repository"):
                run_planning_trial(
                    "PLAN-TRIAL-001",
                    repository_root=Path.cwd(),
                    expected_base_sha=BASE_SHA,
                    known_task_ids=frozenset(),
                )

    def test_factory_failure_is_normalized_after_clean_recheck(self) -> None:
        clean = RepositorySnapshot(BASE_SHA, b"")
        with (
            patch(
                "td_controller.planning_trial.repository_snapshot",
                side_effect=(clean, clean),
            ),
            patch(
                "td_controller.planning_trial.load_reviewed_backlog",
                return_value=BACKLOG,
            ),
            patch(
                "td_controller.planning_trial.CodexPlannerProvider",
                side_effect=RuntimeError("secret"),
            ),
        ):
            with self.assertRaisesRegex(PlanningTrialError, "contained planner failed"):
                run_planning_trial(
                    "PLAN-TRIAL-001",
                    repository_root=Path.cwd(),
                    expected_base_sha=BASE_SHA,
                    known_task_ids=frozenset(),
                )

    def test_missing_session_identity_is_rejected(self) -> None:
        clean = RepositorySnapshot(BASE_SHA, b"")
        planner = FakePlanner(ProviderResult("{}", None))
        with self.assertRaisesRegex(PlanningTrialError, "session identity"):
            run_fake_trial(planner, clean, clean)

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

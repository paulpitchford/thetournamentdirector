"""Tests for mutation-detecting read-only planning trials."""

from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from td_controller.planning_trial import (
    MAX_BACKLOG_BYTES,
    PlanningTrialError,
    RepositorySnapshot,
    _read_bounded_process,
    _validate_repository_root,
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
        clean = RepositorySnapshot(BASE_SHA)
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

    def test_bounded_blob_reader_times_out_and_reaps_partial_process(self) -> None:
        process = subprocess.Popen(
            [
                "/usr/bin/python3", "-c",
                "import os,time; os.write(1,b'x'); time.sleep(5)",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        started = time.monotonic()

        with self.assertRaises(PlanningTrialError):
            _read_bounded_process(
                process, expected_size=2, timeout_seconds=0.1
            )

        self.assertLess(time.monotonic() - started, 1)
        self.assertIsNotNone(process.poll())

    def test_stale_or_malformed_approved_base_prevents_backlog_loading(self) -> None:
        current = RepositorySnapshot(BASE_SHA)
        for expected in ("b" * 40, "INVALID"):
            with (
                patch(
                    "td_controller.planning_trial.repository_snapshot",
                    return_value=current,
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

    def test_base_revision_change_during_trial_is_rejected(self) -> None:
        current = RepositorySnapshot(BASE_SHA)
        changed = RepositorySnapshot("b" * 40)
        for planner in (FakePlanner(), FakePlanner(error=RuntimeError("failed"))):
            with self.assertRaisesRegex(PlanningTrialError, "changed during trial"):
                run_fake_trial(planner, current, changed)

    def test_factory_failure_is_normalized(self) -> None:
        current = RepositorySnapshot(BASE_SHA)
        with (
            patch(
                "td_controller.planning_trial.repository_snapshot",
                return_value=current,
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
        current = RepositorySnapshot(BASE_SHA)
        planner = FakePlanner(ProviderResult("{}", None))
        with self.assertRaisesRegex(PlanningTrialError, "session identity"):
            run_fake_trial(planner, current, current)

    def test_nested_and_linked_worktree_roots_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            main = parent / "main"
            linked = parent / "linked"
            main.mkdir()
            initialize_repository(main, BACKLOG.encode())
            nested = main / "nested"
            nested.mkdir()
            with self.assertRaisesRegex(PlanningTrialError, "self-contained"):
                _validate_repository_root(nested)
            subprocess.run(
                [
                    "/usr/bin/git", "worktree", "add", "-q", "-b",
                    "linked-test", str(linked),
                ],
                cwd=main,
                env={"HOME": temporary, "PATH": "/usr/bin:/bin"},
                check=True,
            )
            with self.assertRaisesRegex(PlanningTrialError, "self-contained"):
                _validate_repository_root(linked)

    def test_repository_identity_does_not_execute_worktree_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base_sha = initialize_repository(root, BACKLOG.encode())
            sentinel = root / "filter-executed"
            environment = {"HOME": temporary, "PATH": "/usr/bin:/bin"}
            subprocess.run(
                [
                    "/usr/bin/git", "config", "filter.sentinel.clean",
                    f"/usr/bin/touch {sentinel}",
                ],
                cwd=root, env=environment, check=True,
            )
            (root / ".gitattributes").write_text("tracked filter=sentinel\n")
            (root / "tracked").write_text("changed")

            identity = repository_snapshot(root)

            self.assertEqual(identity.head_sha, base_sha)
            self.assertFalse(sentinel.exists())

    def test_context_exposes_strict_proposal_constraints(self) -> None:
        context = planner_contract_context()

        self.assertEqual(context["status"], "PROPOSED")
        self.assertIs(context["humanApprovalRequired"], True)
        self.assertIn("ASSERT TEST_PASS", context["criterionGrammar"])
        self.assertIn("repository-policy", context["criterionExample"])
        self.assertNotIn("python3", context["criterionExample"])
        self.assertEqual(
            context["wireEvidenceShape"]["evidenceIds"], ["repository-policy"]
        )
        self.assertEqual(context["protectedPathsExact"], [".github/**"])
        self.assertNotIn(".git/**", context["protectedPathsExact"])
        self.assertIn("HUM-001", context["completedKnownTasks"])


if __name__ == "__main__":
    unittest.main()

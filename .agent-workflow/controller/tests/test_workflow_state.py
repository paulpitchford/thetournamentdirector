"""Tests for durable optimistic task-state transitions."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from td_controller.task_contract import parse_task
from td_controller.workflow_state import TaskStateLedger, WorkflowStateError

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 29, 16, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self) -> None:
        self.value += timedelta(seconds=1)


class Identifiers:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"transition-{self.value:04d}"


def task(*, status: str = "APPROVED", max_attempts: int = 2):
    criterion = "state-ledger|WHEN task state transition executes|ASSERT TEST_PASS evidence"
    return parse_task(
        {
            "id": "DOC-001",
            "status": status,
            "parentEpic": "ORCHESTRATION-PILOT",
            "objective": "Exercise deterministic task state transitions.",
            "nonGoals": [],
            "dependsOn": [],
            "acceptanceCriteria": [criterion],
            "acceptanceEvidenceIds": {criterion: ["evidence"]},
            "acceptanceEvidenceRequirements": {},
            "requiredTests": [
                "python3 .agent-workflow/scripts/check_repository.py"
            ],
            "allowedPaths": ["docs/**"],
            "protectedPaths": [".github/**"],
            "riskClass": "R0",
            "maxChangedLines": 100,
            "maxAttempts": max_attempts,
            "humanApprovalRequired": False,
        }
    )


class TaskStateLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary_directory.name) / "controller.sqlite3"
        self.clock = Clock()
        self.identifiers = Identifiers()
        self.ledger = TaskStateLedger(
            self.path,
            now=self.clock,
            transition_id_factory=self.identifiers,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def register(self, *, status: str = "APPROVED", max_attempts: int = 2):
        return self.ledger.register(
            task(status=status, max_attempts=max_attempts),
            base_sha=BASE_SHA,
            actor="controller-primary",
            gate_id="task-admission",
        )

    def transition(
        self,
        expected_state: str,
        new_state: str,
        *,
        expected_revision: int | None = None,
        expected_head_sha: str | None = None,
        attempt: int = 1,
        head_sha: str | None = None,
        result: str = "PASS",
        artifact_ids: tuple[str, ...] | None = None,
    ):
        current = self.ledger.current("DOC-001")
        expected_revision = expected_revision or current.revision
        expected_head_sha = expected_head_sha or current.head_sha
        if head_sha is None:
            head_sha = HEAD_SHA if new_state == "VERIFYING" else current.head_sha
        gate = (expected_state, new_state) in {
            ("VERIFYING", "PR_DRAFT"),
            ("REVIEWING", "CI_PENDING"),
            ("CI_PENDING", "READY_FOR_POLICY_MERGE"),
        }
        if artifact_ids is None:
            artifact_ids = ("gate-evidence",) if gate else ()
        self.clock.advance()
        return self.ledger.transition(
            "DOC-001",
            expected_state=expected_state,
            expected_revision=expected_revision,
            expected_head_sha=expected_head_sha,
            new_state=new_state,
            attempt=attempt,
            head_sha=head_sha,
            actor="controller-primary",
            gate_id="test-gate",
            result=result,
            artifact_ids=artifact_ids,
        )

    def test_register_records_bound_initial_state_and_event(self) -> None:
        state = self.register()

        self.assertEqual(state.state, "APPROVED")
        self.assertEqual(state.revision, 1)
        self.assertEqual(state.attempt, 1)
        self.assertEqual(state.max_attempts, 2)
        self.assertEqual(state.base_sha, BASE_SHA)
        self.assertEqual(state.head_sha, BASE_SHA)
        history = self.ledger.history("DOC-001")
        self.assertEqual(len(history), 1)
        self.assertIsNone(history[0].prior_state)
        self.assertEqual(history[0].event_order, 1)
        self.assertEqual(history[0].new_state, "APPROVED")
        self.assertEqual(history[0].base_sha, BASE_SHA)
        self.assertEqual(history[0].result, "PASS")

    def test_duplicate_registration_is_atomic(self) -> None:
        original = self.register()

        with self.assertRaisesRegex(WorkflowStateError, "conflicts"):
            self.register()

        self.assertEqual(self.ledger.current("DOC-001"), original)
        self.assertEqual(len(self.ledger.history("DOC-001")), 1)

    def test_non_gated_path_is_ordered_and_fabricated_evidence_cannot_advance(self) -> None:
        self.register()
        path = (
            ("APPROVED", "QUEUED"),
            ("QUEUED", "LEASED"),
            ("LEASED", "IMPLEMENTING"),
            ("IMPLEMENTING", "VERIFYING"),
        )
        for prior, new in path:
            self.transition(prior, new)
        current = self.ledger.current("DOC-001")
        with self.assertRaisesRegex(WorkflowStateError, "authoritative"):
            self.transition(
                "VERIFYING", "PR_DRAFT", artifact_ids=("fabricated",)
            )
        history = self.ledger.history("DOC-001")
        self.assertEqual(self.ledger.current("DOC-001"), current)
        self.assertEqual(
            [record.event_order for record in history],
            list(range(1, len(history) + 1)),
        )

    def test_stale_identity_and_illegal_transition_roll_back(self) -> None:
        self.register()
        current = self.transition("APPROVED", "QUEUED")

        invalid = (
            {"expected_revision": 1},
            {"expected_head_sha": "c" * 40},
            {"head_sha": "c" * 40},
            {"result": "FAIL"},
            {"artifact_ids": ("fabricated",)},
        )
        for override in invalid:
            with self.subTest(override=override):
                with self.assertRaises(WorkflowStateError):
                    self.transition("QUEUED", "LEASED", **override)

        self.assertEqual(self.ledger.current("DOC-001"), current)
        self.assertEqual(len(self.ledger.history("DOC-001")), 2)

    def test_retry_increments_attempt_once_and_enforces_maximum(self) -> None:
        self.register(max_attempts=2)
        failed = self.transition(
            "APPROVED", "FAILED_RETRYABLE", result="FAIL"
        )
        self.assertEqual(failed.attempt, 1)

        queued = self.transition("FAILED_RETRYABLE", "QUEUED", attempt=2)
        self.assertEqual(queued.attempt, 2)
        failed_again = self.transition(
            "QUEUED", "FAILED_RETRYABLE", attempt=2, result="FAIL"
        )
        self.assertEqual(failed_again.attempt, 2)
        with self.assertRaisesRegex(WorkflowStateError, "attempt"):
            self.transition("FAILED_RETRYABLE", "QUEUED", attempt=3)

        self.assertEqual(
            self.ledger.current("DOC-001").state, "FAILED_RETRYABLE"
        )
        quarantined = self.transition(
            "FAILED_RETRYABLE", "QUARANTINED", attempt=2, result="FAIL"
        )
        self.assertEqual(quarantined.state, "QUARANTINED")

    def test_interruptions_require_compatible_results(self) -> None:
        self.register()
        with self.assertRaisesRegex(WorkflowStateError, "incompatible"):
            self.transition("APPROVED", "QUARANTINED", result="PASS")
        state = self.transition("APPROVED", "QUARANTINED", result="FAIL")

        self.assertEqual(state.state, "QUARANTINED")
        self.assertEqual(self.ledger.active(), ())
        event = self.ledger.history("DOC-001")[-1]
        self.assertEqual(event.artifact_ids, ())
        with self.assertRaisesRegex(WorkflowStateError, "not allowed"):
            self.transition("QUARANTINED", "QUEUED")

    def test_restart_recovers_current_non_terminal_state_and_history(self) -> None:
        self.register()
        expected = self.transition("APPROVED", "QUEUED")

        restarted = TaskStateLedger(
            self.path,
            now=self.clock,
            transition_id_factory=self.identifiers,
        )

        self.assertEqual(restarted.current("DOC-001"), expected)
        self.assertEqual(restarted.active(), (expected,))
        self.assertEqual(len(restarted.history("DOC-001")), 2)

    def test_duplicate_transition_identity_cannot_update_current_state(self) -> None:
        self.register()
        duplicate = TaskStateLedger(
            self.path,
            now=self.clock,
            transition_id_factory=lambda: "transition-0001",
        )

        with self.assertRaisesRegex(WorkflowStateError, "identity conflicts"):
            duplicate.transition(
                "DOC-001",
                expected_state="APPROVED",
                expected_revision=1,
                expected_head_sha=BASE_SHA,
                new_state="QUEUED",
                attempt=1,
                head_sha=BASE_SHA,
                actor="controller-primary",
                gate_id="test-gate",
                result="PASS",
            )

        self.assertEqual(self.ledger.current("DOC-001").state, "APPROVED")


if __name__ == "__main__":
    unittest.main()

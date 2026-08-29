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
        attempt: int = 1,
        head_sha: str = HEAD_SHA,
        result: str = "PASS",
        artifact_ids: tuple[str, ...] = (),
    ):
        self.clock.advance()
        return self.ledger.transition(
            "DOC-001",
            expected_state=expected_state,
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

    def test_complete_normal_path_is_recorded_and_becomes_inactive(self) -> None:
        self.register()
        path = (
            ("APPROVED", "QUEUED"),
            ("QUEUED", "LEASED"),
            ("LEASED", "IMPLEMENTING"),
            ("IMPLEMENTING", "VERIFYING"),
            ("VERIFYING", "PR_DRAFT"),
            ("PR_DRAFT", "REVIEWING"),
            ("REVIEWING", "CI_PENDING"),
            ("CI_PENDING", "READY_FOR_POLICY_MERGE"),
            ("READY_FOR_POLICY_MERGE", "AUTO_MERGE_PENDING"),
            ("AUTO_MERGE_PENDING", "MERGED"),
            ("MERGED", "DONE"),
        )

        for prior, new in path:
            with self.subTest(new=new):
                state = self.transition(prior, new)
                self.assertEqual(state.state, new)

        self.assertEqual(self.ledger.active(), ())
        history = self.ledger.history("DOC-001")
        self.assertEqual(
            [record.new_state for record in history],
            ["APPROVED", *(new for _, new in path)],
        )
        self.assertEqual(
            [record.event_order for record in history],
            list(range(1, len(history) + 1)),
        )

    def test_review_remediation_loop_returns_to_verification(self) -> None:
        self.register(status="QUEUED")
        for prior, new in (
            ("QUEUED", "LEASED"),
            ("LEASED", "IMPLEMENTING"),
            ("IMPLEMENTING", "VERIFYING"),
            ("VERIFYING", "PR_DRAFT"),
            ("PR_DRAFT", "REVIEWING"),
            ("REVIEWING", "REMEDIATING"),
            ("REMEDIATING", "VERIFYING"),
        ):
            self.transition(prior, new)

        self.assertEqual(self.ledger.current("DOC-001").state, "VERIFYING")

    def test_stale_or_illegal_transition_rolls_back(self) -> None:
        self.register()

        for expected, new in (
            ("QUEUED", "LEASED"),
            ("APPROVED", "IMPLEMENTING"),
        ):
            with self.subTest(expected=expected, new=new):
                with self.assertRaises(WorkflowStateError):
                    self.transition(expected, new)

        self.assertEqual(self.ledger.current("DOC-001").state, "APPROVED")
        self.assertEqual(len(self.ledger.history("DOC-001")), 1)

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

    def test_interruptions_are_terminal_and_preserve_failure_evidence(self) -> None:
        self.register()
        state = self.transition(
            "APPROVED",
            "QUARANTINED",
            result="FAIL",
            artifact_ids=("scope-report", "provider-log"),
        )

        self.assertEqual(state.state, "QUARANTINED")
        self.assertEqual(self.ledger.active(), ())
        event = self.ledger.history("DOC-001")[-1]
        self.assertEqual(event.artifact_ids, ("scope-report", "provider-log"))
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
                new_state="QUEUED",
                attempt=1,
                head_sha=HEAD_SHA,
                actor="controller-primary",
                gate_id="test-gate",
                result="PASS",
            )

        self.assertEqual(self.ledger.current("DOC-001").state, "APPROVED")

    def test_clock_rollback_is_rejected_without_mutation(self) -> None:
        registered = self.register()
        self.clock.value -= timedelta(seconds=1)

        with self.assertRaisesRegex(WorkflowStateError, "clock moved backwards"):
            self.ledger.transition(
                "DOC-001",
                expected_state="APPROVED",
                new_state="QUEUED",
                attempt=1,
                head_sha=HEAD_SHA,
                actor="controller-primary",
                gate_id="test-gate",
                result="PASS",
            )

        self.assertEqual(self.ledger.current("DOC-001"), registered)
        self.assertEqual(len(self.ledger.history("DOC-001")), 1)

    def test_malformed_metadata_is_rejected_without_mutation(self) -> None:
        self.register()
        invalid = (
            {"head_sha": "ABC"},
            {"actor": "controller primary"},
            {"gate_id": ""},
            {"result": "SUCCESS"},
            {"artifact_ids": ["not-a-tuple"]},
            {"artifact_ids": ("duplicate", "duplicate")},
        )
        for override in invalid:
            arguments = {
                "task_id": "DOC-001",
                "expected_state": "APPROVED",
                "new_state": "QUEUED",
                "attempt": 1,
                "head_sha": HEAD_SHA,
                "actor": "controller-primary",
                "gate_id": "test-gate",
                "result": "PASS",
                "artifact_ids": (),
            }
            arguments.update(override)
            with self.subTest(override=override):
                with self.assertRaises(WorkflowStateError):
                    self.ledger.transition(**arguments)

        self.assertEqual(self.ledger.current("DOC-001").state, "APPROVED")
        self.assertEqual(len(self.ledger.history("DOC-001")), 1)

    def test_non_dispatchable_task_and_naive_clock_are_rejected(self) -> None:
        with self.assertRaisesRegex(WorkflowStateError, "not dispatchable"):
            self.ledger.register(
                task(status="PROPOSED"),
                base_sha=BASE_SHA,
                actor="controller-primary",
                gate_id="task-admission",
            )
        naive = TaskStateLedger(
            self.path,
            now=lambda: datetime(2026, 8, 29),
            transition_id_factory=self.identifiers,
        )
        with self.assertRaisesRegex(WorkflowStateError, "timezone-aware"):
            naive.register(
                task(),
                base_sha=BASE_SHA,
                actor="controller-primary",
                gate_id="task-admission",
            )


if __name__ == "__main__":
    unittest.main()

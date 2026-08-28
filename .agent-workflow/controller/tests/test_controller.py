"""Tests for pause, provider, and durable run-admission behaviour."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from td_controller.config import LimitConfig
from td_controller.controller import Controller, ControllerPausedError
from td_controller.pause import PauseSwitch
from td_controller.provider import FakeProvider
from td_controller.state import AdmissionError, RunLedger

FIXED_NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def limits() -> LimitConfig:
    """Return the approved pilot limits used by ledger tests."""
    return LimitConfig(
        max_concurrent_runs=1,
        max_run_minutes=60,
        max_runs_per_day=8,
        review_reserve_percent=30,
        max_remediation_rounds=2,
        retention_days=14,
    )


class PausingLedger(RunLedger):
    """Test boundary that activates the pause switch during reservation."""

    def __init__(self, *args: object, pause_switch: PauseSwitch, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.pause_switch = pause_switch

    def reserve(self, *, task_id: str, role: str) -> str:
        """Reserve, then simulate a concurrent operator pause."""
        run_id = super().reserve(task_id=task_id, role=role)
        self.pause_switch.pause("concurrent operator pause")
        return run_id


class ControllerTests(unittest.TestCase):
    """Prove provider calls cannot bypass pause or admission controls."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.ledger = RunLedger(
            self.root / "state.sqlite3",
            limits(),
            now=lambda: FIXED_NOW,
        )
        self.pause_switch = PauseSwitch(self.root / "PAUSED")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_successful_provider_run_is_recorded(self) -> None:
        provider = FakeProvider()
        controller = Controller(
            ledger=self.ledger,
            pause_switch=self.pause_switch,
            provider=provider,
        )

        result = controller.run(task_id="TASK-001", role="planning")

        self.assertEqual(result.summary, "fake:TASK-001:planning")
        self.assertEqual(provider.calls, [("TASK-001", "planning")])
        self.assertEqual(self.ledger.summary()["SUCCEEDED"], 1)

    def test_pause_blocks_provider_before_reservation(self) -> None:
        provider = FakeProvider()
        self.pause_switch.pause("operator requested")
        controller = Controller(
            ledger=self.ledger,
            pause_switch=self.pause_switch,
            provider=provider,
        )

        with self.assertRaisesRegex(ControllerPausedError, "operator requested"):
            controller.run(task_id="TASK-001", role="planning")

        self.assertEqual(provider.calls, [])
        self.assertEqual(self.ledger.records(), [])

    def test_pause_during_reservation_blocks_provider(self) -> None:
        provider = FakeProvider()
        ledger = PausingLedger(
            self.root / "race.sqlite3",
            limits(),
            now=lambda: FIXED_NOW,
            pause_switch=self.pause_switch,
        )
        controller = Controller(
            ledger=ledger,
            pause_switch=self.pause_switch,
            provider=provider,
        )

        with self.assertRaisesRegex(ControllerPausedError, "during admission"):
            controller.run(task_id="TASK-001", role="planning")

        self.assertEqual(provider.calls, [])
        self.assertEqual(ledger.records()[0].status, "FAILED")

    def test_provider_failure_is_recorded_and_reraised(self) -> None:
        provider = FakeProvider(error=RuntimeError("provider unavailable"))
        controller = Controller(
            ledger=self.ledger,
            pause_switch=self.pause_switch,
            provider=provider,
        )

        with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
            controller.run(task_id="TASK-001", role="planning")

        record = self.ledger.records()[0]
        self.assertEqual(record.status, "FAILED")
        self.assertEqual(record.error, "RuntimeError")

    def test_concurrent_run_is_rejected_atomically(self) -> None:
        self.ledger.reserve(task_id="TASK-001", role="planning")

        with self.assertRaisesRegex(AdmissionError, "concurrent"):
            self.ledger.reserve(task_id="TASK-002", role="planning")

    def test_review_reserve_blocks_sixth_non_review_run(self) -> None:
        for index in range(5):
            run_id = self.ledger.reserve(task_id=f"TASK-{index}", role="planning")
            self.ledger.finish(run_id, status="SUCCEEDED")

        with self.assertRaisesRegex(AdmissionError, "reserve"):
            self.ledger.reserve(task_id="TASK-006", role="implementation")

        review_id = self.ledger.reserve(task_id="TASK-REVIEW", role="code_review")
        self.ledger.finish(review_id, status="SUCCEEDED")
        self.assertEqual(self.ledger.summary()["SUCCEEDED"], 6)

    def test_daily_hard_limit_includes_failed_runs(self) -> None:
        for index in range(8):
            run_id = self.ledger.reserve(task_id=f"REVIEW-{index}", role="qa_review")
            self.ledger.finish(run_id, status="FAILED", error="FakeFailure")

        with self.assertRaisesRegex(AdmissionError, "daily"):
            self.ledger.reserve(task_id="REVIEW-9", role="qa_review")

    def test_records_survive_ledger_restart(self) -> None:
        run_id = self.ledger.reserve(task_id="TASK-001", role="planning")
        self.ledger.finish(run_id, status="SUCCEEDED")

        restarted = RunLedger(
            self.root / "state.sqlite3",
            limits(),
            now=lambda: FIXED_NOW,
        )

        record = restarted.records()[0]
        self.assertEqual(record.task_id, "TASK-001")
        self.assertEqual(record.status, "SUCCEEDED")
        self.assertIsNotNone(record.finished_at)
        self.assertIsNone(record.error)
        self.assertEqual(restarted.summary()["SUCCEEDED"], 1)


if __name__ == "__main__":
    unittest.main()

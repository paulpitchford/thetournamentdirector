"""Tests for mandatory local review-agent separation."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from td_controller.config import LimitConfig
from td_controller.pause import PauseSwitch
from td_controller.provider import FakeProvider, Provider
from td_controller.review import ReviewCoordinator, ReviewSeparationError
from td_controller.state import RunLedger

FIXED_NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


class FakeReviewFactory:
    """Create deterministic providers with configured session identities."""

    def __init__(self, sessions: dict[str, str | None], *, reuse: bool = False) -> None:
        self.sessions = sessions
        self.reuse = reuse
        self.created: list[Provider] = []
        self.requests: list[tuple[str, bool]] = []

    def create(self, *, role: str, source_read_only: bool) -> Provider:
        """Create or deliberately reuse a fake provider for one role."""
        self.requests.append((role, source_read_only))
        if self.reuse and self.created:
            return self.created[0]
        provider = FakeProvider(session_id=self.sessions[role])
        self.created.append(provider)
        return provider


def limits() -> LimitConfig:
    """Return approved limits for review-coordinator tests."""
    return LimitConfig(
        max_concurrent_runs=1,
        max_run_minutes=60,
        max_runs_per_day=8,
        review_reserve_percent=30,
        max_remediation_rounds=2,
        retention_days=14,
    )


class ReviewCoordinatorTests(unittest.TestCase):
    """Prove code/security and QA cannot share agent sessions."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.ledger = RunLedger(
            root / "state.sqlite3",
            limits(),
            now=lambda: FIXED_NOW,
        )
        self.pause_switch = PauseSwitch(root / "PAUSED")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_distinct_review_sessions_are_required_and_recorded(self) -> None:
        factory = FakeReviewFactory(
            {"code_review": "code-session", "qa_review": "qa-session"}
        )
        coordinator = ReviewCoordinator(
            ledger=self.ledger,
            pause_switch=self.pause_switch,
            provider_factory=factory,
        )

        evidence = coordinator.run_required_reviews(
            task_id="TASK-001",
            implementation_session_id="implementation-session",
        )

        self.assertEqual(evidence.code_security.session_id, "code-session")
        self.assertEqual(evidence.qa.session_id, "qa-session")
        self.assertEqual(len(factory.created), 2)
        self.assertEqual(
            factory.requests,
            [("code_review", True), ("qa_review", True)],
        )
        self.assertEqual(self.ledger.summary()["SUCCEEDED"], 2)

    def test_implementation_session_identity_is_mandatory(self) -> None:
        factory = FakeReviewFactory(
            {"code_review": "code-session", "qa_review": "qa-session"}
        )
        coordinator = ReviewCoordinator(
            ledger=self.ledger,
            pause_switch=self.pause_switch,
            provider_factory=factory,
        )

        with self.assertRaisesRegex(ReviewSeparationError, "implementation session"):
            coordinator.run_required_reviews(
                task_id="TASK-001",
                implementation_session_id="",
            )

        self.assertEqual(factory.created, [])

    def test_one_provider_instance_cannot_serve_both_roles(self) -> None:
        factory = FakeReviewFactory(
            {"code_review": "shared-session", "qa_review": "shared-session"},
            reuse=True,
        )
        coordinator = ReviewCoordinator(
            ledger=self.ledger,
            pause_switch=self.pause_switch,
            provider_factory=factory,
        )

        with self.assertRaisesRegex(ReviewSeparationError, "provider instance"):
            coordinator.run_required_reviews(
                task_id="TASK-001",
                implementation_session_id="implementation-session",
            )

        self.assertEqual(self.ledger.records(), [])

    def test_qa_cannot_reuse_code_review_session(self) -> None:
        factory = FakeReviewFactory(
            {"code_review": "shared-session", "qa_review": "shared-session"}
        )
        coordinator = ReviewCoordinator(
            ledger=self.ledger,
            pause_switch=self.pause_switch,
            provider_factory=factory,
        )

        with self.assertRaisesRegex(ReviewSeparationError, "forbidden session"):
            coordinator.run_required_reviews(
                task_id="TASK-001",
                implementation_session_id="implementation-session",
            )

    def test_review_cannot_reuse_implementation_session(self) -> None:
        factory = FakeReviewFactory(
            {"code_review": "implementation-session", "qa_review": "qa-session"}
        )
        coordinator = ReviewCoordinator(
            ledger=self.ledger,
            pause_switch=self.pause_switch,
            provider_factory=factory,
        )

        with self.assertRaisesRegex(ReviewSeparationError, "forbidden session"):
            coordinator.run_required_reviews(
                task_id="TASK-001",
                implementation_session_id="implementation-session",
            )


if __name__ == "__main__":
    unittest.main()

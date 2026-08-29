"""Tests for durable exclusive task leases and restart recovery."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from td_controller.lease import LeaseError, TaskLeaseLedger


class Clock:
    """Deterministic mutable clock for lease tests."""

    def __init__(self) -> None:
        self.value = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class IdentifierFactory:
    """Deterministic injected lease ID source."""

    def __init__(self) -> None:
        self.number = 0

    def __call__(self) -> str:
        self.number += 1
        return f"lease-{self.number:04d}"


class TaskLeaseLedgerTests(unittest.TestCase):
    """Prove leases remain exclusive and recoverable across restarts."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary_directory.name) / "controller.sqlite3"
        self.clock = Clock()
        self.identifiers = IdentifierFactory()
        self.ledger = TaskLeaseLedger(
            self.path,
            now=self.clock,
            lease_id_factory=self.identifiers,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def acquire(
        self,
        *,
        task_id: str = "DOC-001",
        branch: str = "agent/doc-001/attempt-1",
        owner: str = "controller-primary",
        ttl_seconds: int = 300,
    ):
        return self.ledger.acquire(
            task_id=task_id,
            branch=branch,
            owner=owner,
            ttl_seconds=ttl_seconds,
        )

    def test_acquire_persists_complete_active_lease(self) -> None:
        lease = self.acquire()

        self.assertEqual(lease.lease_id, "lease-0001")
        self.assertEqual(lease.task_id, "DOC-001")
        self.assertEqual(lease.branch, "agent/doc-001/attempt-1")
        self.assertEqual(lease.owner, "controller-primary")
        self.assertEqual(lease.state, "ACTIVE")
        self.assertEqual(lease.acquired_at, lease.heartbeat_at)
        self.assertIsNone(lease.released_at)
        self.assertEqual(self.ledger.active(), (lease,))

    def test_task_and_branch_are_independently_exclusive(self) -> None:
        self.acquire()

        with self.assertRaisesRegex(LeaseError, "conflicts"):
            self.acquire(branch="agent/doc-001/attempt-2")
        with self.assertRaisesRegex(LeaseError, "conflicts"):
            self.acquire(task_id="DOC-002")

        self.assertEqual(len(self.ledger.records()), 1)

    def test_expired_lease_is_replaced_without_losing_history(self) -> None:
        first = self.acquire(ttl_seconds=60)
        self.clock.advance(seconds=60)

        second = self.acquire()

        self.assertEqual(first.lease_id, "lease-0001")
        self.assertEqual(second.lease_id, "lease-0002")
        records = self.ledger.records()
        self.assertEqual([record.state for record in records], ["EXPIRED", "ACTIVE"])
        self.assertEqual(records[0].released_at, second.acquired_at)

    def test_heartbeat_extends_only_exact_owned_active_lease(self) -> None:
        lease = self.acquire(ttl_seconds=60)
        original_expiry = lease.expires_at
        self.clock.advance(seconds=30)

        heartbeat = self.ledger.heartbeat(
            lease.lease_id,
            owner="controller-primary",
            ttl_seconds=300,
        )

        self.assertGreater(heartbeat.heartbeat_at, lease.heartbeat_at)
        self.assertGreater(heartbeat.expires_at, original_expiry)
        with self.assertRaisesRegex(LeaseError, "unavailable"):
            self.ledger.heartbeat(
                lease.lease_id,
                owner="controller-secondary",
                ttl_seconds=300,
            )

    def test_expired_lease_cannot_be_heartbeated_or_released(self) -> None:
        lease = self.acquire(ttl_seconds=60)
        self.clock.advance(seconds=61)

        with self.assertRaisesRegex(LeaseError, "unavailable"):
            self.ledger.heartbeat(
                lease.lease_id,
                owner="controller-primary",
                ttl_seconds=300,
            )
        with self.assertRaisesRegex(LeaseError, "unavailable"):
            self.ledger.release(lease.lease_id, owner="controller-primary")

        self.assertEqual(self.ledger.active(), ())
        self.assertEqual(self.ledger.get(lease.lease_id).state, "EXPIRED")

    def test_clock_rollback_blocks_heartbeat_and_release(self) -> None:
        lease = self.acquire()
        self.clock.value -= timedelta(seconds=1)

        with self.assertRaisesRegex(LeaseError, "clock moved backwards"):
            self.ledger.heartbeat(
                lease.lease_id,
                owner="controller-primary",
                ttl_seconds=300,
            )
        with self.assertRaisesRegex(LeaseError, "clock moved backwards"):
            self.ledger.release(lease.lease_id, owner="controller-primary")

        self.assertEqual(self.ledger.get(lease.lease_id).state, "ACTIVE")

    def test_release_is_exactly_once_and_allows_reacquisition(self) -> None:
        first = self.acquire()

        released = self.ledger.release(
            first.lease_id, owner="controller-primary"
        )

        self.assertEqual(released.state, "RELEASED")
        self.assertIsNotNone(released.released_at)
        with self.assertRaisesRegex(LeaseError, "unavailable"):
            self.ledger.release(first.lease_id, owner="controller-primary")
        self.assertEqual(self.acquire().state, "ACTIVE")

    def test_active_leases_survive_restart(self) -> None:
        lease = self.acquire()

        restarted = TaskLeaseLedger(
            self.path,
            now=self.clock,
            lease_id_factory=self.identifiers,
        )

        self.assertEqual(restarted.active(), (lease,))
        self.clock.advance(seconds=301)
        self.assertEqual(restarted.active(), ())
        self.assertEqual(restarted.get(lease.lease_id).state, "EXPIRED")

    def test_duplicate_injected_lease_id_fails_closed(self) -> None:
        self.acquire()
        self.ledger.release("lease-0001", owner="controller-primary")
        duplicate = TaskLeaseLedger(
            self.path,
            now=self.clock,
            lease_id_factory=lambda: "lease-0001",
        )

        with self.assertRaisesRegex(LeaseError, "conflicts"):
            duplicate.acquire(
                task_id="DOC-002",
                branch="agent/doc-002/attempt-1",
                owner="controller-primary",
                ttl_seconds=300,
            )

        self.assertEqual(len(self.ledger.records()), 1)

    def test_invalid_inputs_are_rejected_before_database_mutation(self) -> None:
        invalid = (
            {"task_id": "doc-001"},
            {"branch": "feature/doc-001"},
            {"branch": "agent/doc-001/attempt-6"},
            {"branch": f"agent/{'a' * 120}/attempt-1"},
            {"owner": "user"},
            {"owner": f"controller-{'a' * 80}"},
            {"ttl_seconds": 59},
            {"ttl_seconds": True},
        )
        for override in invalid:
            arguments = {
                "task_id": "DOC-001",
                "branch": "agent/doc-001/attempt-1",
                "owner": "controller-primary",
                "ttl_seconds": 300,
            }
            arguments.update(override)
            with self.subTest(override=override):
                with self.assertRaises(LeaseError):
                    self.ledger.acquire(**arguments)

        self.assertEqual(self.ledger.records(), ())

    def test_clock_must_be_timezone_aware(self) -> None:
        ledger = TaskLeaseLedger(
            self.path,
            now=lambda: datetime(2026, 8, 29),
            lease_id_factory=self.identifiers,
        )

        with self.assertRaisesRegex(LeaseError, "timezone-aware"):
            ledger.acquire(
                task_id="DOC-001",
                branch="agent/doc-001/attempt-1",
                owner="controller-primary",
                ttl_seconds=300,
            )


if __name__ == "__main__":
    unittest.main()

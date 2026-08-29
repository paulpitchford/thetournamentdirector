"""Durable exclusive task leases for restart-safe dispatch."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .task_contract import TASK_ID_PATTERN

BRANCH_PATTERN = re.compile(
    r"^agent/[a-z0-9]+(?:-[a-z0-9]+)*/attempt-[1-5]$"
)
OWNER_PATTERN = re.compile(r"^controller-[a-z0-9]+(?:-[a-z0-9]+)*$")
LEASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{7,127}$")


class LeaseError(RuntimeError):
    """Raised when an exclusive lease operation cannot be completed safely."""


@dataclass(frozen=True)
class TaskLease:
    """One immutable view of a durable task lease."""

    lease_id: str
    task_id: str
    branch: str
    owner: str
    state: str
    acquired_at: str
    heartbeat_at: str
    expires_at: str
    released_at: str | None


class TaskLeaseLedger:
    """SQLite lease ledger with atomic task and branch exclusivity."""

    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], datetime],
        lease_id_factory: Callable[[], str],
    ) -> None:
        self.path = path
        self._now = now
        self._lease_id_factory = lease_id_factory
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _migrate(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS task_leases (
                    lease_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('ACTIVE', 'RELEASED', 'EXPIRED')
                    ),
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    released_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS active_lease_task
                ON task_leases(task_id) WHERE state = 'ACTIVE'
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS active_lease_branch
                ON task_leases(branch) WHERE state = 'ACTIVE'
                """
            )

    def acquire(
        self,
        *,
        task_id: str,
        branch: str,
        owner: str,
        ttl_seconds: int,
    ) -> TaskLease:
        """Atomically acquire one lease after expiring stale active leases."""
        _validate_identity(task_id, branch, owner)
        _validate_ttl(ttl_seconds)
        lease_id = self._lease_id_factory()
        if not isinstance(lease_id, str) or not LEASE_ID_PATTERN.fullmatch(lease_id):
            raise LeaseError("lease ID factory returned an invalid identifier")
        now = self._utc_now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_stale(connection, now)
            connection.execute(
                """
                INSERT INTO task_leases(
                    lease_id, task_id, branch, owner, state, acquired_at,
                    heartbeat_at, expires_at
                ) VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, ?)
                """,
                (
                    lease_id,
                    task_id,
                    branch,
                    owner,
                    _timestamp(now),
                    _timestamp(now),
                    _timestamp(expires_at),
                ),
            )
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise LeaseError("lease identity, task, or branch conflicts") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(lease_id)

    def heartbeat(
        self,
        lease_id: str,
        *,
        owner: str,
        ttl_seconds: int,
    ) -> TaskLease:
        """Extend an active lease owned by the exact controller identity."""
        _validate_lease_id(lease_id)
        _validate_owner(owner)
        _validate_ttl(ttl_seconds)
        now = self._utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_stale(connection, now)
            row = connection.execute(
                """
                SELECT heartbeat_at FROM task_leases
                WHERE lease_id = ? AND owner = ? AND state = 'ACTIVE'
                """,
                (lease_id, owner),
            ).fetchone()
            if row is None:
                raise LeaseError("active lease is unavailable")
            if _parse_timestamp(row["heartbeat_at"]) > now:
                raise LeaseError("lease clock moved backwards")
            connection.execute(
                """
                UPDATE task_leases SET heartbeat_at = ?, expires_at = ?
                WHERE lease_id = ? AND owner = ? AND state = 'ACTIVE'
                """,
                (
                    _timestamp(now),
                    _timestamp(now + timedelta(seconds=ttl_seconds)),
                    lease_id,
                    owner,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(lease_id)

    def release(self, lease_id: str, *, owner: str) -> TaskLease:
        """Release one active lease exactly once."""
        _validate_lease_id(lease_id)
        _validate_owner(owner)
        now = self._utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_stale(connection, now)
            row = connection.execute(
                """
                SELECT heartbeat_at FROM task_leases
                WHERE lease_id = ? AND owner = ? AND state = 'ACTIVE'
                """,
                (lease_id, owner),
            ).fetchone()
            if row is None:
                raise LeaseError("active lease is unavailable")
            if _parse_timestamp(row["heartbeat_at"]) > now:
                raise LeaseError("lease clock moved backwards")
            connection.execute(
                """
                UPDATE task_leases
                SET state = 'RELEASED', released_at = ?
                WHERE lease_id = ? AND owner = ? AND state = 'ACTIVE'
                """,
                (_timestamp(now), lease_id, owner),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(lease_id)

    def active(self) -> tuple[TaskLease, ...]:
        """Expire stale rows and return active leases for restart recovery."""
        now = self._utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_stale(connection, now)
            rows = connection.execute(
                """
                SELECT * FROM task_leases WHERE state = 'ACTIVE'
                ORDER BY task_id, branch, lease_id
                """
            ).fetchall()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return tuple(_lease(row) for row in rows)

    def get(self, lease_id: str) -> TaskLease:
        """Return one lease without changing its state."""
        _validate_lease_id(lease_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM task_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
        if row is None:
            raise LeaseError("lease is unavailable")
        return _lease(row)

    def records(self) -> tuple[TaskLease, ...]:
        """Return complete lease history in deterministic order."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM task_leases
                ORDER BY acquired_at, lease_id
                """
            ).fetchall()
        return tuple(_lease(row) for row in rows)

    def _utc_now(self) -> datetime:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise LeaseError("lease clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _expire_stale(connection: sqlite3.Connection, now: datetime) -> None:
        timestamp = _timestamp(now)
        connection.execute(
            """
            UPDATE task_leases
            SET state = 'EXPIRED', released_at = ?
            WHERE state = 'ACTIVE' AND expires_at <= ?
            """,
            (timestamp, timestamp),
        )


def _validate_identity(task_id: str, branch: str, owner: str) -> None:
    if (
        not isinstance(task_id, str)
        or len(task_id) > 128
        or not TASK_ID_PATTERN.fullmatch(task_id)
    ):
        raise LeaseError("task ID is invalid")
    if (
        not isinstance(branch, str)
        or len(branch) > 128
        or not BRANCH_PATTERN.fullmatch(branch)
    ):
        raise LeaseError("task branch is invalid")
    _validate_owner(owner)


def _validate_owner(owner: str) -> None:
    if (
        not isinstance(owner, str)
        or len(owner) > 80
        or not OWNER_PATTERN.fullmatch(owner)
    ):
        raise LeaseError("lease owner is invalid")


def _validate_lease_id(lease_id: str) -> None:
    if not isinstance(lease_id, str) or not LEASE_ID_PATTERN.fullmatch(lease_id):
        raise LeaseError("lease ID is invalid")


def _validate_ttl(ttl_seconds: int) -> None:
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise LeaseError("lease TTL must be an integer")
    if not 60 <= ttl_seconds <= 7_200:
        raise LeaseError("lease TTL is outside the approved range")


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise LeaseError("stored lease timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise LeaseError("stored lease timestamp is invalid")
    return parsed.astimezone(UTC)


def _lease(row: sqlite3.Row) -> TaskLease:
    return TaskLease(
        lease_id=row["lease_id"],
        task_id=row["task_id"],
        branch=row["branch"],
        owner=row["owner"],
        state=row["state"],
        acquired_at=row["acquired_at"],
        heartbeat_at=row["heartbeat_at"],
        expires_at=row["expires_at"],
        released_at=row["released_at"],
    )

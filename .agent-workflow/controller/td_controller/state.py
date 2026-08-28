"""Durable SQLite run ledger and atomic admission controls."""

from __future__ import annotations

import math
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import LimitConfig

REVIEW_ROLES = frozenset({"code_review", "security_review", "qa_review", "remediation"})
RUN_ROLES = REVIEW_ROLES | {"implementation", "planning"}


class AdmissionError(RuntimeError):
    """Raised when a hard concurrency or budget rule rejects a run."""


@dataclass(frozen=True)
class RunRecord:
    """One durable provider-run record."""

    run_id: str
    task_id: str
    role: str
    status: str
    started_at: str
    finished_at: str | None
    error: str | None


class RunLedger:
    """SQLite-backed run ledger safe across controller restarts."""

    def __init__(
        self,
        path: Path,
        limits: LimitConfig,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = path
        self.limits = limits
        self._now = now or (lambda: datetime.now(UTC))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _migrate(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    error TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS runs_started_at ON runs(started_at)"
            )

    def reserve(self, *, task_id: str, role: str) -> str:
        """Atomically reserve one run after enforcing all hard limits."""
        if role not in RUN_ROLES:
            raise AdmissionError(f"unknown run role: {role}")
        if not task_id.strip():
            raise AdmissionError("task_id must not be empty")

        started_at = self._now().astimezone(UTC).isoformat()
        day_prefix = started_at[:10]
        run_id = str(uuid.uuid4())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE status = 'RUNNING'"
            ).fetchone()[0]
            if active >= self.limits.max_concurrent_runs:
                raise AdmissionError("maximum concurrent runs reached")

            total = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE substr(started_at, 1, 10) = ?",
                (day_prefix,),
            ).fetchone()[0]
            if total >= self.limits.max_runs_per_day:
                raise AdmissionError("daily run budget reached")

            if role not in REVIEW_ROLES:
                reserved = math.ceil(
                    self.limits.max_runs_per_day
                    * self.limits.review_reserve_percent
                    / 100
                )
                non_review_limit = self.limits.max_runs_per_day - reserved
                non_review_used = connection.execute(
                    """
                    SELECT COUNT(*) FROM runs
                    WHERE substr(started_at, 1, 10) = ?
                      AND role NOT IN ('code_review', 'security_review', 'qa_review', 'remediation')
                    """,
                    (day_prefix,),
                ).fetchone()[0]
                if non_review_used >= non_review_limit:
                    raise AdmissionError("review/remediation reserve is protected")

            connection.execute(
                """
                INSERT INTO runs(run_id, task_id, role, status, started_at)
                VALUES (?, ?, ?, 'RUNNING', ?)
                """,
                (run_id, task_id, role, started_at),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return run_id

    def finish(self, run_id: str, *, status: str, error: str | None = None) -> None:
        """Finish an existing active run exactly once."""
        if status not in {"SUCCEEDED", "FAILED"}:
            raise ValueError(f"invalid terminal run status: {status}")
        finished_at = self._now().astimezone(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE runs
                SET status = ?, finished_at = ?, error = ?
                WHERE run_id = ? AND status = 'RUNNING'
                """,
                (status, finished_at, error, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"active run not found: {run_id}")

    def records(self) -> list[RunRecord]:
        """Return all records in deterministic start/run order."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT run_id, task_id, role, status, started_at, finished_at, error
                FROM runs ORDER BY started_at, run_id
                """
            ).fetchall()
        return [RunRecord(**dict(row)) for row in rows]

    def summary(self) -> dict[str, int]:
        """Return run counts by status for operator display."""
        counts = {"RUNNING": 0, "SUCCEEDED": 0, "FAILED": 0}
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM runs GROUP BY status"
            ).fetchall()
        for row in rows:
            counts[row["status"]] = row["count"]
        return counts

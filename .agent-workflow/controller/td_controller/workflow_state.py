"""Durable optimistic task-state transitions for controlled dispatch."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .task_contract import (
    DISPATCHABLE_STATES,
    EVIDENCE_SOURCES,
    TASK_ID_PATTERN,
    TaskContract,
)

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TOKEN_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
NORMAL_TRANSITIONS = {
    "APPROVED": frozenset({"QUEUED"}),
    "QUEUED": frozenset({"LEASED"}),
    "LEASED": frozenset({"IMPLEMENTING"}),
    "IMPLEMENTING": frozenset({"VERIFYING"}),
    "VERIFYING": frozenset({"PR_DRAFT"}),
    "PR_DRAFT": frozenset({"REVIEWING"}),
    "REVIEWING": frozenset({"REMEDIATING", "CI_PENDING"}),
    "REMEDIATING": frozenset({"VERIFYING"}),
    "CI_PENDING": frozenset({"READY_FOR_POLICY_MERGE"}),
    "READY_FOR_POLICY_MERGE": frozenset({"AUTO_MERGE_PENDING"}),
    "AUTO_MERGE_PENDING": frozenset({"MERGED"}),
    "MERGED": frozenset({"DONE"}),
    "FAILED_RETRYABLE": frozenset({"QUEUED"}),
}
INTERRUPTION_STATES = frozenset(
    {
        "BLOCKED_REQUIREMENTS",
        "BLOCKED_DEPENDENCY",
        "FAILED_RETRYABLE",
        "QUARANTINED",
        "CANCELLED",
        "SUPERSEDED",
    }
)
INTERRUPTIBLE_STATES = frozenset(NORMAL_TRANSITIONS) - {"MERGED"}
HEAD_CHANGE_TRANSITIONS = frozenset(
    {("IMPLEMENTING", "VERIFYING"), ("REMEDIATING", "VERIFYING")}
)
EVIDENCE_GATE_TRANSITIONS = frozenset(
    {
        ("VERIFYING", "PR_DRAFT"),
        ("REVIEWING", "CI_PENDING"),
        ("CI_PENDING", "READY_FOR_POLICY_MERGE"),
    }
)
FAILURE_STATES = frozenset(
    {
        "BLOCKED_REQUIREMENTS",
        "BLOCKED_DEPENDENCY",
        "FAILED_RETRYABLE",
        "QUARANTINED",
    }
)
TERMINAL_STATES = frozenset(
    {
        "DONE",
        "BLOCKED_REQUIREMENTS",
        "BLOCKED_DEPENDENCY",
        "QUARANTINED",
        "CANCELLED",
        "SUPERSEDED",
    }
)


class WorkflowStateError(RuntimeError):
    """Raised when a task-state operation is stale or invalid."""


@dataclass(frozen=True)
class TaskState:
    """Current durable state for one approved task."""

    task_id: str
    state: str
    revision: int
    attempt: int
    max_attempts: int
    base_sha: str
    head_sha: str
    updated_at: str


@dataclass(frozen=True)
class TransitionRecord:
    """One append-only task transition event."""

    transition_id: str
    task_id: str
    event_order: int
    attempt: int
    prior_state: str | None
    new_state: str
    timestamp: str
    base_sha: str
    head_sha: str
    actor: str
    gate_id: str
    result: str
    artifact_ids: tuple[str, ...]


@dataclass(frozen=True)
class GateEvidence:
    """One immutable gate result bound to an exact task revision."""

    evidence_id: str
    task_id: str
    attempt: int
    revision: int
    gate_id: str
    head_sha: str
    source: str
    result: str
    artifact_sha256: str
    recorded_at: str
    actor: str


class TaskStateLedger:
    """SQLite task state with optimistic transitions and append-only history."""

    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], datetime],
        transition_id_factory: Callable[[], str],
    ) -> None:
        self.path = path
        self._now = now
        self._transition_id_factory = transition_id_factory
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
                CREATE TABLE IF NOT EXISTS task_states (
                    task_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    attempt INTEGER NOT NULL,
                    max_attempts INTEGER NOT NULL,
                    base_sha TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS task_transitions (
                    transition_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    event_order INTEGER NOT NULL,
                    attempt INTEGER NOT NULL,
                    prior_state TEXT,
                    new_state TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    base_sha TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    gate_id TEXT NOT NULL,
                    result TEXT NOT NULL,
                    artifact_ids TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES task_states(task_id),
                    UNIQUE(task_id, event_order)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS task_transitions_task
                ON task_transitions(task_id, timestamp, transition_id)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS gate_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    gate_id TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    source TEXT NOT NULL,
                    result TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES task_states(task_id)
                )
                """
            )

    def register(
        self,
        task: TaskContract,
        *,
        base_sha: str,
        actor: str,
        gate_id: str,
    ) -> TaskState:
        """Register one approved or queued task exactly once."""
        if task.status not in DISPATCHABLE_STATES:
            raise WorkflowStateError("task is not dispatchable")
        _validate_sha(base_sha)
        _validate_token(actor, "actor")
        _validate_token(gate_id, "gate ID")
        timestamp = self._timestamp()
        transition_id = self._transition_id()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO task_states(
                    task_id, state, revision, attempt, max_attempts, base_sha,
                    head_sha, updated_at
                ) VALUES (?, ?, 1, 1, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.status,
                    task.max_attempts,
                    base_sha,
                    base_sha,
                    timestamp,
                ),
            )
            self._insert_transition(
                connection,
                transition_id=transition_id,
                task_id=task.task_id,
                event_order=1,
                attempt=1,
                prior_state=None,
                new_state=task.status,
                timestamp=timestamp,
                base_sha=base_sha,
                head_sha=base_sha,
                actor=actor,
                gate_id=gate_id,
                result="PASS",
                artifact_ids=(),
            )
            committed = _task_state(connection.execute(
                "SELECT * FROM task_states WHERE task_id = ?", (task.task_id,)
            ).fetchone())
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise WorkflowStateError("task or transition identity conflicts") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return committed

    def record_evidence(
        self,
        task_id: str,
        *,
        evidence_id: str,
        expected_revision: int,
        expected_head_sha: str,
        attempt: int,
        lease_id: str,
        gate_id: str,
        source: str,
        result: str,
        artifact_sha256: str,
    ) -> GateEvidence:
        """Append one immutable gate result under the active task lease."""
        _validate_task_id(task_id)
        for value, field in (
            (evidence_id, "evidence ID"),
            (lease_id, "lease ID"),
            (gate_id, "gate ID"),
        ):
            _validate_token(value, field)
        _validate_sha(expected_head_sha)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (expected_revision, attempt)
        ):
            raise WorkflowStateError("gate evidence version is invalid")
        if (
            not isinstance(source, str)
            or source not in EVIDENCE_SOURCES
            or not isinstance(result, str)
            or result not in {"PASS", "FAIL"}
        ):
            raise WorkflowStateError("gate evidence classification is invalid")
        if not isinstance(artifact_sha256, str) or not DIGEST_PATTERN.fullmatch(
            artifact_sha256
        ):
            raise WorkflowStateError("gate evidence digest is invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = self._timestamp()
            row = connection.execute(
                "SELECT * FROM task_states WHERE task_id = ?", (task_id,)
            ).fetchone()
            lease = connection.execute(
                """
                SELECT owner FROM task_leases
                WHERE lease_id = ? AND task_id = ? AND state = 'ACTIVE'
                    AND expires_at > ?
                """,
                (lease_id, task_id, timestamp),
            ).fetchone()
            if row is None or lease is None:
                raise WorkflowStateError("authoritative evidence context is unavailable")
            if (
                row["revision"] != expected_revision
                or row["head_sha"] != expected_head_sha
                or row["attempt"] != attempt
            ):
                raise WorkflowStateError("task state changed before evidence")
            connection.execute(
                """
                INSERT INTO gate_evidence(
                    evidence_id, task_id, attempt, revision, gate_id, head_sha,
                    source, result, artifact_sha256, recorded_at, actor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id, task_id, attempt, expected_revision, gate_id,
                    expected_head_sha, source, result, artifact_sha256,
                    timestamp, lease["owner"],
                ),
            )
            committed = connection.execute(
                "SELECT * FROM gate_evidence WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise WorkflowStateError("evidence identity conflicts") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return _gate_evidence(committed)

    def transition(
        self,
        task_id: str,
        *,
        expected_state: str,
        expected_revision: int,
        expected_head_sha: str,
        new_state: str,
        attempt: int,
        head_sha: str,
        lease_id: str,
        gate_id: str,
        result: str,
        artifact_ids: tuple[str, ...] = (),
    ) -> TaskState:
        """Apply one legal transition under an exact active task lease."""
        _validate_task_id(task_id)
        _validate_state(expected_state)
        _validate_state(new_state)
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise WorkflowStateError("task revision is invalid")
        _validate_sha(expected_head_sha)
        _validate_sha(head_sha)
        _validate_token(lease_id, "lease ID")
        _validate_token(gate_id, "gate ID")
        if not isinstance(result, str) or result not in {"PASS", "FAIL", "NONE"}:
            raise WorkflowStateError("transition result is invalid")
        artifacts = _validate_artifact_ids(artifact_ids)
        transition_id = self._transition_id()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = self._timestamp()
            row = connection.execute(
                "SELECT * FROM task_states WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise WorkflowStateError("task state is unavailable")
            lease = connection.execute(
                """
                SELECT owner FROM task_leases
                WHERE lease_id = ? AND task_id = ? AND state = 'ACTIVE'
                    AND expires_at > ?
                """,
                (lease_id, task_id, timestamp),
            ).fetchone()
            if lease is None:
                raise WorkflowStateError("authoritative task lease is unavailable")
            if (
                row["state"] != expected_state
                or row["revision"] != expected_revision
                or row["head_sha"] != expected_head_sha
            ):
                raise WorkflowStateError("task state changed before transition")
            if _parse_timestamp(row["updated_at"]) > _parse_timestamp(timestamp):
                raise WorkflowStateError("workflow clock moved backwards")
            evidence_authorized = bool(artifacts) and all(
                connection.execute(
                    """
                    SELECT 1 FROM gate_evidence
                    WHERE evidence_id = ? AND task_id = ? AND attempt = ?
                        AND revision = ? AND gate_id = ? AND head_sha = ?
                        AND result = 'PASS'
                    """,
                    (
                        evidence_id, task_id, attempt, expected_revision,
                        gate_id, expected_head_sha,
                    ),
                ).fetchone() is not None
                for evidence_id in artifacts
            )
            _validate_transition(
                expected_state,
                new_state,
                current_attempt=row["attempt"],
                new_attempt=attempt,
                max_attempts=row["max_attempts"],
                head_changed=head_sha != expected_head_sha,
                result=result,
                has_artifacts=bool(artifacts),
                evidence_authorized=evidence_authorized,
            )
            cursor = connection.execute(
                """
                UPDATE task_states
                SET state = ?, revision = revision + 1, attempt = ?,
                    head_sha = ?, updated_at = ?
                WHERE task_id = ? AND state = ? AND revision = ?
                    AND attempt = ? AND head_sha = ?
                """,
                (
                    new_state,
                    attempt,
                    head_sha,
                    timestamp,
                    task_id,
                    expected_state,
                    expected_revision,
                    row["attempt"],
                    expected_head_sha,
                ),
            )
            if cursor.rowcount != 1:
                raise WorkflowStateError("task state changed before transition")
            self._insert_transition(
                connection,
                transition_id=transition_id,
                task_id=task_id,
                event_order=connection.execute(
                    "SELECT COUNT(*) + 1 FROM task_transitions WHERE task_id = ?",
                    (task_id,),
                ).fetchone()[0],
                attempt=attempt,
                prior_state=expected_state,
                new_state=new_state,
                timestamp=timestamp,
                base_sha=row["base_sha"],
                head_sha=head_sha,
                actor=lease["owner"],
                gate_id=gate_id,
                result=result,
                artifact_ids=artifacts,
            )
            committed = _task_state(connection.execute(
                "SELECT * FROM task_states WHERE task_id = ?", (task_id,)
            ).fetchone())
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise WorkflowStateError("transition identity conflicts") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return committed

    def current(self, task_id: str) -> TaskState:
        """Return current state for one registered task."""
        _validate_task_id(task_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM task_states WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise WorkflowStateError("task state is unavailable")
        return _task_state(row)

    def active(self) -> tuple[TaskState, ...]:
        """Return non-terminal tasks for deterministic restart inspection."""
        placeholders = ",".join("?" for _ in TERMINAL_STATES)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM task_states WHERE state NOT IN ({placeholders})
                ORDER BY task_id
                """,
                tuple(sorted(TERMINAL_STATES)),
            ).fetchall()
        return tuple(_task_state(row) for row in rows)

    def history(self, task_id: str) -> tuple[TransitionRecord, ...]:
        """Return append-only history for one task."""
        _validate_task_id(task_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM task_transitions WHERE task_id = ?
                ORDER BY event_order
                """,
                (task_id,),
            ).fetchall()
        return tuple(_transition(row) for row in rows)

    def evidence(self, evidence_id: str) -> GateEvidence:
        """Return one immutable evidence record."""
        _validate_token(evidence_id, "evidence ID")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM gate_evidence WHERE evidence_id = ?", (evidence_id,)
            ).fetchone()
        if row is None:
            raise WorkflowStateError("gate evidence is unavailable")
        return _gate_evidence(row)

    def _timestamp(self) -> str:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise WorkflowStateError("workflow clock must be timezone-aware")
        return value.astimezone(UTC).isoformat(timespec="microseconds")

    def _transition_id(self) -> str:
        value = self._transition_id_factory()
        _validate_token(value, "transition ID")
        return value

    @staticmethod
    def _insert_transition(
        connection: sqlite3.Connection,
        *,
        transition_id: str,
        task_id: str,
        event_order: int,
        attempt: int,
        prior_state: str | None,
        new_state: str,
        timestamp: str,
        base_sha: str,
        head_sha: str,
        actor: str,
        gate_id: str,
        result: str,
        artifact_ids: tuple[str, ...],
    ) -> None:
        connection.execute(
            """
            INSERT INTO task_transitions(
                transition_id, task_id, event_order, attempt, prior_state,
                new_state, timestamp, base_sha, head_sha, actor, gate_id,
                result, artifact_ids
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transition_id,
                task_id,
                event_order,
                attempt,
                prior_state,
                new_state,
                timestamp,
                base_sha,
                head_sha,
                actor,
                gate_id,
                result,
                "\n".join(artifact_ids),
            ),
        )


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise WorkflowStateError("stored workflow timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise WorkflowStateError("stored workflow timestamp is invalid")
    return parsed.astimezone(UTC)


def _validate_transition(
    prior: str,
    new: str,
    *,
    current_attempt: int,
    new_attempt: int,
    max_attempts: int,
    head_changed: bool,
    result: str,
    has_artifacts: bool,
    evidence_authorized: bool,
) -> None:
    allowed = set(NORMAL_TRANSITIONS.get(prior, ()))
    if prior in INTERRUPTIBLE_STATES:
        allowed.update(INTERRUPTION_STATES)
    allowed.discard(prior)
    if new not in allowed:
        raise WorkflowStateError("task transition is not allowed")
    edge = (prior, new)
    if head_changed and edge not in HEAD_CHANGE_TRANSITIONS:
        raise WorkflowStateError("task head change is not allowed")
    if edge in EVIDENCE_GATE_TRANSITIONS:
        if result != "PASS" or not evidence_authorized:
            raise WorkflowStateError("authoritative gate evidence is unavailable")
    elif has_artifacts:
        raise WorkflowStateError("gate evidence is not allowed for this transition")
    expected_result = (
        "FAIL" if new in FAILURE_STATES
        else "NONE" if new in {"CANCELLED", "SUPERSEDED"}
        else "PASS"
    )
    if result != expected_result:
        raise WorkflowStateError("task transition result is incompatible")
    retrying = prior == "FAILED_RETRYABLE" and new == "QUEUED"
    expected_attempt = current_attempt + 1 if retrying else current_attempt
    if new_attempt != expected_attempt or not 1 <= new_attempt <= max_attempts:
        raise WorkflowStateError("task transition attempt is invalid")


def _validate_task_id(task_id: str) -> None:
    if (
        not isinstance(task_id, str)
        or len(task_id) > 128
        or not TASK_ID_PATTERN.fullmatch(task_id)
    ):
        raise WorkflowStateError("task ID is invalid")


def _validate_state(state: str) -> None:
    states = set(NORMAL_TRANSITIONS) | set(TERMINAL_STATES) | {"MERGED"}
    if not isinstance(state, str) or state not in states:
        raise WorkflowStateError("task state is invalid")


def _validate_sha(value: str) -> None:
    if not isinstance(value, str) or not SHA_PATTERN.fullmatch(value):
        raise WorkflowStateError("task Git identity is invalid")


def _validate_token(value: str, field: str) -> None:
    if not isinstance(value, str) or not TOKEN_PATTERN.fullmatch(value):
        raise WorkflowStateError(f"{field} is invalid")


def _validate_artifact_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > 32 or len(values) != len(set(values)):
        raise WorkflowStateError("artifact IDs are invalid")
    for value in values:
        _validate_token(value, "artifact ID")
    return values


def _task_state(row: sqlite3.Row) -> TaskState:
    return TaskState(
        task_id=row["task_id"],
        state=row["state"],
        revision=row["revision"],
        attempt=row["attempt"],
        max_attempts=row["max_attempts"],
        base_sha=row["base_sha"],
        head_sha=row["head_sha"],
        updated_at=row["updated_at"],
    )


def _gate_evidence(row: sqlite3.Row) -> GateEvidence:
    return GateEvidence(
        evidence_id=row["evidence_id"],
        task_id=row["task_id"],
        attempt=row["attempt"],
        revision=row["revision"],
        gate_id=row["gate_id"],
        head_sha=row["head_sha"],
        source=row["source"],
        result=row["result"],
        artifact_sha256=row["artifact_sha256"],
        recorded_at=row["recorded_at"],
        actor=row["actor"],
    )


def _transition(row: sqlite3.Row) -> TransitionRecord:
    artifacts = tuple(filter(None, row["artifact_ids"].split("\n")))
    return TransitionRecord(
        transition_id=row["transition_id"],
        task_id=row["task_id"],
        event_order=row["event_order"],
        attempt=row["attempt"],
        prior_state=row["prior_state"],
        new_state=row["new_state"],
        timestamp=row["timestamp"],
        base_sha=row["base_sha"],
        head_sha=row["head_sha"],
        actor=row["actor"],
        gate_id=row["gate_id"],
        result=row["result"],
        artifact_ids=artifacts,
    )

"""Repository-inaccessible coordinator for contained read-only planning trials."""

from __future__ import annotations

import hashlib
import os
import select
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .codex_planner import CodexPlannerProvider, PlannerRequest
from .review_runtime import SystemdCgroupExecutor

MAX_BACKLOG_BYTES = 100_000
APPROVED_BACKLOG = Path("docs/DELIVERY_BACKLOG.md")


class PlanningTrialError(RuntimeError):
    """Raised when a trial input, session, or repository boundary is invalid."""


@dataclass(frozen=True)
class RepositorySnapshot:
    head_sha: str


@dataclass(frozen=True)
class PlanningTrialRecord:
    plan_id: str
    session_id: str
    base_sha: str
    backlog_sha256: str
    summary: str


def load_reviewed_backlog(repository_root: Path, expected_base_sha: str) -> str:
    """Load the exact approved backlog blob from the expected Git commit."""
    root = repository_root.resolve(strict=True)
    environment = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }
    object_name = f"{expected_base_sha}:{APPROVED_BACKLOG.as_posix()}"

    def cat_file(mode: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["/usr/bin/git", "cat-file", mode, object_name],
            cwd=root,
            env=environment,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )

    try:
        size_result = cat_file("-s")
        if size_result.returncode != 0 or len(size_result.stdout) > 32:
            raise PlanningTrialError("reviewed backlog is unavailable")
        size = int(size_result.stdout.strip())
        if size > MAX_BACKLOG_BYTES:
            raise PlanningTrialError("reviewed backlog exceeds the size limit")
        process = subprocess.Popen(
            ["/usr/bin/git", "cat-file", "blob", object_name],
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        payload, returncode = _read_bounded_process(
            process, expected_size=size, timeout_seconds=10
        )
    except PlanningTrialError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise PlanningTrialError("reviewed backlog is unavailable") from exc
    if returncode != 0 or len(payload) != size:
        raise PlanningTrialError("reviewed backlog is unavailable")
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PlanningTrialError("reviewed backlog is not valid UTF-8") from exc


def _read_bounded_process(
    process: subprocess.Popen[bytes], *, expected_size: int, timeout_seconds: float
) -> tuple[bytes, int]:
    if process.stdout is None:
        process.kill()
        process.wait(timeout=5)
        raise PlanningTrialError("reviewed backlog is unavailable")
    payload = bytearray()
    deadline = time.monotonic() + timeout_seconds
    descriptor = process.stdout.fileno()
    os.set_blocking(descriptor, False)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            readable, _, _ = select.select([descriptor], [], [], remaining)
            if not readable:
                raise TimeoutError
            try:
                chunk = os.read(descriptor, min(65_536, expected_size + 1 - len(payload)))
            except BlockingIOError:
                continue
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > expected_size:
                break
    except TimeoutError as exc:
        process.kill()
        process.wait(timeout=5)
        raise PlanningTrialError("reviewed backlog is unavailable") from exc
    finally:
        process.stdout.close()
    if len(payload) > expected_size:
        process.kill()
    try:
        returncode = process.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait(timeout=5)
        raise PlanningTrialError("reviewed backlog is unavailable") from exc
    return bytes(payload), returncode


def repository_snapshot(repository_root: Path) -> RepositorySnapshot:
    """Capture only the approved Git identity; planner input comes from that commit."""
    root = repository_root.resolve(strict=True)
    environment = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }
    try:
        head = subprocess.run(
            ["/usr/bin/git", "rev-parse", "HEAD"],
            cwd=root,
            env=environment,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise PlanningTrialError("repository snapshot failed") from exc
    try:
        head_sha = head.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise PlanningTrialError("repository identity is invalid") from exc
    if len(head_sha) != 40 or any(character not in "0123456789abcdef" for character in head_sha):
        raise PlanningTrialError("repository identity is invalid")
    return RepositorySnapshot(head_sha)


def run_planning_trial(
    plan_id: str,
    *,
    repository_root: Path,
    expected_base_sha: str,
    known_task_ids: frozenset[str],
) -> PlanningTrialRecord:
    """Run one fresh planner from controller-derived inputs inside a read-only boundary."""
    if len(expected_base_sha) != 40 or any(
        character not in "0123456789abcdef" for character in expected_base_sha
    ):
        raise PlanningTrialError("approved planning base revision is invalid")
    root = repository_root.resolve(strict=True)
    before = repository_snapshot(root)
    if before.head_sha != expected_base_sha:
        raise PlanningTrialError("planning trial requires the exact approved base revision")
    backlog = load_reviewed_backlog(root, expected_base_sha)
    request = PlannerRequest(
        plan_id=plan_id,
        base_sha=before.head_sha,
        backlog_sha256=hashlib.sha256(backlog.encode("utf-8")).hexdigest(),
        backlog=backlog,
        planning_context=planner_contract_context(),
        known_task_ids=known_task_ids,
    )
    provider_error: Exception | None = None
    result = None
    try:
        executor = SystemdCgroupExecutor(inaccessible_paths=(root,))
        provider = CodexPlannerProvider(request, executor=executor)
        result = provider.run(task_id=request.plan_id, role="planner")
    except Exception as exc:
        provider_error = exc
    finally:
        after = repository_snapshot(root)
        if after != before:
            raise PlanningTrialError("planning trial changed repository state")
    if provider_error is not None:
        raise PlanningTrialError("contained planner failed") from provider_error
    if result is None or not isinstance(result.session_id, str) or not result.session_id.strip():
        raise PlanningTrialError("planning trial returned no fresh session identity")
    return PlanningTrialRecord(
        request.plan_id,
        result.session_id,
        request.base_sha,
        request.backlog_sha256,
        result.summary,
    )


def planner_contract_context() -> dict[str, object]:
    """Return controller-owned grammar supplied as data to every planning trial."""
    return {
        "scope": "propose one to three earliest incomplete tasks justified by the backlog",
        "taskIdGrammar": "uppercase segments separated by hyphens, for example FND-001",
        "parentEpic": "MODERN-APP",
        "status": "PROPOSED",
        "humanApprovalRequired": True,
        "criterionGrammar": (
            "criterion-id|WHEN condition of at least three words|ASSERT "
            "TEST_PASS selected-evidence-id"
        ),
        "wireEvidenceShape": {
            "criterion": "the exact full criterion string",
            "evidenceIds": ["selected-evidence-id"],
            "requiredSources": [],
        },
        "registeredTests": [
            ".agent-workflow/scripts/verify_controller.sh",
            "python3 .agent-workflow/scripts/check_repository.py",
        ],
        "pathRules": (
            "use canonical repository-relative paths; never propose .github, .git, "
            "downloads, extracted, analysis, controller policy, or controller scripts"
        ),
        "completedKnownTasks": ["HUM-001", "HUM-002"],
    }

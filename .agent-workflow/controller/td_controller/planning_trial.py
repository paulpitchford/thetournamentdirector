"""Mutation-detecting coordinator for contained read-only planning trials."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .codex_planner import CodexPlannerProvider, PlannerRequest

MAX_BACKLOG_BYTES = 100_000
APPROVED_BACKLOG = Path("docs/DELIVERY_BACKLOG.md")


class PlanningTrialError(RuntimeError):
    """Raised when a trial input, session, or repository boundary is invalid."""


@dataclass(frozen=True)
class RepositorySnapshot:
    head_sha: str
    worktree_status: bytes


@dataclass(frozen=True)
class PlanningTrialRecord:
    plan_id: str
    session_id: str
    base_sha: str
    backlog_sha256: str
    summary: str


def load_reviewed_backlog(repository_root: Path) -> str:
    """Load the approved backlog through bounded descriptor-relative no-follow opens."""
    try:
        root = repository_root.resolve(strict=True)
        root_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            docs_descriptor = os.open(
                "docs",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_descriptor,
            )
        finally:
            os.close(root_descriptor)
        try:
            descriptor = os.open(
                APPROVED_BACKLOG.name,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                dir_fd=docs_descriptor,
            )
        finally:
            os.close(docs_descriptor)
        with os.fdopen(descriptor, "rb") as backlog_file:
            file_stat = os.fstat(backlog_file.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise PlanningTrialError("reviewed backlog is not a regular file")
            payload = backlog_file.read(MAX_BACKLOG_BYTES + 1)
    except PlanningTrialError:
        raise
    except OSError as exc:
        raise PlanningTrialError("reviewed backlog is unavailable") from exc
    if len(payload) > MAX_BACKLOG_BYTES:
        raise PlanningTrialError("reviewed backlog exceeds the size limit")
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PlanningTrialError("reviewed backlog is not valid UTF-8") from exc


def repository_snapshot(repository_root: Path) -> RepositorySnapshot:
    """Capture controller-visible Git identity and mutation state without hooks."""
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
        status = subprocess.run(
            [
                "/usr/bin/git", "-c", "core.hooksPath=/dev/null",
                "-c", "core.fsmonitor=false", "status", "--porcelain=v1",
                "--untracked-files=all",
            ],
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
    return RepositorySnapshot(head_sha, status)


def run_planning_trial(
    plan_id: str,
    *,
    repository_root: Path,
    known_task_ids: frozenset[str],
) -> PlanningTrialRecord:
    """Run one fresh planner from controller-derived inputs inside a read-only boundary."""
    before = repository_snapshot(repository_root)
    if before.worktree_status:
        raise PlanningTrialError("planning trial requires the exact clean base revision")
    backlog = load_reviewed_backlog(repository_root)
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
        provider = CodexPlannerProvider(request)
        result = provider.run(task_id=request.plan_id, role="planner")
    except Exception as exc:
        provider_error = exc
    finally:
        after = repository_snapshot(repository_root)
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

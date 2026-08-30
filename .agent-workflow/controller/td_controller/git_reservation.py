"""Reserve one exact branch in controller-exclusive Git metadata."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .exact_ref_command import build_exact_ref_command
from .planning_trial import PlanningTrialError, _validate_repository_root
from .review_contract import CodexReviewError
from .review_runtime import SubprocessExecutor
from .workspace_identity_handle import (
    WorkspaceIdentity,
    WorkspaceIdentityHandle,
    WorkspaceIdentityHandleError,
)

GIT = "/usr/bin/git"
SHA = re.compile(r"[0-9a-f]{40}")


class GitReservationError(RuntimeError):
    """Raised when an exact branch reservation cannot be created."""


@dataclass(frozen=True)
class GitBranchReservation:
    """Exact reserved ref and approved base commit."""

    ref_name: str
    base_sha: str


def _environment() -> dict[str, str]:
    return {
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1", "HOME": "/nonexistent",
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin",
    }


def reserve_git_branch(
    repository_root: Path, handle: WorkspaceIdentityHandle, *, base_sha: str,
) -> GitBranchReservation:
    """Hold workspace authority through one absent exact-ref transaction."""
    if not isinstance(handle, WorkspaceIdentityHandle):
        raise GitReservationError("workspace handle is invalid")
    if not isinstance(base_sha, str) or not SHA.fullmatch(base_sha):
        raise GitReservationError("reservation base SHA is invalid")
    try:
        root = _validate_repository_root(repository_root)
        _validate_metadata(root)
    except (PlanningTrialError, OSError):
        raise GitReservationError("repository layout is invalid") from None
    try:
        with handle.hold_identity() as identity:
            return _reserve_held(root, identity, base_sha)
    except WorkspaceIdentityHandleError:
        raise GitReservationError("workspace handle is unavailable") from None


def _validate_metadata(root: Path) -> None:
    git_dir = root / ".git"
    objects = git_dir / "objects"
    paths = (
        git_dir, objects, objects / "info", git_dir / "refs",
        git_dir / "refs" / "heads", git_dir / "logs",
        git_dir / "logs" / "refs", git_dir / "logs" / "refs" / "heads",
    )
    for path in paths:
        metadata = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or path.is_symlink()
        ):
            raise OSError("unsafe Git metadata")
    alternates = objects / "info" / "alternates"
    if alternates.exists() or alternates.is_symlink():
        raise OSError("Git alternates are prohibited")


def _reserve_held(
    root: Path, identity: WorkspaceIdentity, base_sha: str,
) -> GitBranchReservation:
    ref_name = f"refs/heads/agent-{identity.task_id.lower()}-{identity.generation}"
    command = build_exact_ref_command(ref_name, base_sha)
    executor = SubprocessExecutor(lambda _: dict(command.environment))
    try:
        object_format = executor.run(
            [GIT, "rev-parse", "--show-object-format"], input_bytes=b"",
            cwd=root, timeout_seconds=10,
        )
        ref_format = executor.run(
            [GIT, "rev-parse", "--show-ref-format"], input_bytes=b"",
            cwd=root, timeout_seconds=10,
        )
        commit_type = executor.run(
            [GIT, "cat-file", "-t", base_sha], input_bytes=b"",
            cwd=root, timeout_seconds=10,
        )
        if (
            object_format != type(object_format)(0, b"sha1\n", b"")
            or ref_format != type(ref_format)(0, b"files\n", b"")
            or commit_type != type(commit_type)(0, b"commit\n", b"")
        ):
            raise GitReservationError("repository format or base is invalid")
        created = executor.run(
            list(command.argv), input_bytes=b"", cwd=root,
            timeout_seconds=10,
        )
    except (CodexReviewError, OSError):
        raise GitReservationError("branch reservation process failed") from None
    if created.returncode != 0 or created.stdout or created.stderr:
        raise GitReservationError("branch reservation was not created")
    return GitBranchReservation(ref_name, base_sha)

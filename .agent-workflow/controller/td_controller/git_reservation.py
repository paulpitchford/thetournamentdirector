"""Atomic controller-owned Git branch reservation without checkout."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .planning_trial import PlanningTrialError, _validate_repository_root
from .review_contract import CodexReviewError
from .review_runtime import SubprocessExecutor
from .workspace_identity_handle import (
    WorkspaceIdentity,
    WorkspaceIdentityHandle,
    WorkspaceIdentityHandleError,
)

SHA = re.compile(r"[0-9a-f]{40}")
GIT = "/usr/bin/git"
ZERO_SHA = "0" * 40


class GitReservationError(RuntimeError):
    """Raised when an exact branch reservation cannot be created."""


@dataclass(frozen=True)
class GitBranchReservation:
    """Controller-derived ref bound to one workspace generation and base."""

    ref_name: str
    base_sha: str


def _environment() -> dict[str, str]:
    return {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }


def reserve_git_branch(
    repository_root: Path,
    handle: WorkspaceIdentityHandle,
    *,
    base_sha: str,
) -> GitBranchReservation:
    """Create one absent ref atomically at the exact approved commit."""
    if not isinstance(handle, WorkspaceIdentityHandle):
        raise GitReservationError("workspace handle is invalid")
    if not isinstance(base_sha, str) or not SHA.fullmatch(base_sha):
        raise GitReservationError("reservation base SHA is invalid")
    try:
        root = _validate_repository_root(repository_root)
        _validate_object_storage(root)
    except (PlanningTrialError, OSError):
        raise GitReservationError("repository layout is invalid") from None
    try:
        with handle.hold_identity() as identity:
            return _reserve_held(root, identity, base_sha)
    except WorkspaceIdentityHandleError:
        raise GitReservationError("workspace handle is unavailable") from None


def _validate_object_storage(root: Path) -> None:
    git_dir = root / ".git"
    objects = git_dir / "objects"
    for path in (git_dir, objects):
        metadata = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or path.is_symlink()
        ):
            raise OSError("unsafe Git object storage")
    alternates = objects / "info" / "alternates"
    if alternates.exists() or alternates.is_symlink():
        raise OSError("Git object alternates are prohibited")


def _reserve_held(
    root: Path, identity: WorkspaceIdentity, base_sha: str
) -> GitBranchReservation:
    ref_name = f"refs/heads/agent/{identity.task_id.lower()}/{identity.generation}"
    executor = SubprocessExecutor(lambda _: _environment())
    try:
        commit = executor.run(
            [GIT, "cat-file", "-e", f"{base_sha}^{{commit}}"],
            input_bytes=b"", cwd=root, timeout_seconds=10,
        )
        if commit.returncode != 0 or commit.stdout or commit.stderr:
            raise GitReservationError("reservation base commit is unavailable")
        created = executor.run(
            [GIT, "update-ref", "--create-reflog", ref_name, base_sha, ZERO_SHA],
            input_bytes=b"", cwd=root, timeout_seconds=10,
        )
    except (CodexReviewError, OSError):
        raise GitReservationError("branch reservation process failed") from None
    if created.returncode != 0 or created.stdout or created.stderr:
        raise GitReservationError("branch reservation was not created")
    return GitBranchReservation(ref_name, base_sha)


def run_local_probe() -> None:
    with tempfile.TemporaryDirectory(prefix="td-git-reservation-", dir="/var/tmp") as temporary:
        root = Path(temporary)
        subprocess.run([GIT, "init", "-q", str(root)], check=True, env=_environment())
        (root / "tracked.txt").write_text("base\n", encoding="utf-8")
        environment = _environment() | {
            "GIT_AUTHOR_NAME": "TD Controller", "GIT_AUTHOR_EMAIL": "td@example.invalid",
            "GIT_COMMITTER_NAME": "TD Controller", "GIT_COMMITTER_EMAIL": "td@example.invalid",
        }
        subprocess.run([GIT, "add", "tracked.txt"], cwd=root, check=True, env=environment)
        subprocess.run([GIT, "commit", "-q", "-m", "base"], cwd=root, check=True, env=environment)
        base = subprocess.check_output(
            [GIT, "rev-parse", "HEAD"], cwd=root, env=_environment()
        ).decode().strip()
        workspace = root / "workspace"
        workspace.mkdir(mode=0o700)
        descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            handle = WorkspaceIdentityHandle(
                "ORCH-003D1B0K", attempt=1, generation="0" * 32,
                descriptor=descriptor,
            )
        finally:
            os.close(descriptor)
        try:
            reservation = reserve_git_branch(root, handle, base_sha=base)
            value = subprocess.check_output(
                [GIT, "rev-parse", reservation.ref_name], cwd=root,
                env=_environment(),
            ).decode().strip()
            if value != base:
                raise GitReservationError("reserved ref has the wrong commit")
            try:
                reserve_git_branch(root, handle, base_sha=base)
            except GitReservationError:
                pass
            else:
                raise GitReservationError("duplicate reservation was accepted")
        finally:
            handle.close()


if __name__ == "__main__":
    run_local_probe()
    print("Atomic Git branch reservation proof passed.")

"""Metadata-only Git worktree reservation for controlled dispatch."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .lease import LeaseError, TaskLease, TaskLeaseLedger
from .workflow_state import TaskStateLedger, WorkflowStateError

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class WorktreeError(RuntimeError):
    """Raised when a task worktree cannot be reserved safely."""


class AmbiguousGitError(WorktreeError):
    """Raised when a timed-out Git mutation may have taken effect."""


@dataclass(frozen=True)
class WorktreeReservation:
    task_id: str
    attempt: int
    branch: str
    base_sha: str
    path: Path
    lease_id: str


class MetadataWorktreeManager:
    """Create branch-bound worktrees without running checkout filters or hooks."""

    def __init__(
        self,
        repository_root: Path,
        worktree_root: Path,
        *,
        lease_ledger: TaskLeaseLedger,
        state_ledger: TaskStateLedger,
    ) -> None:
        try:
            self.repository_root = repository_root.resolve(strict=True)
            git_marker = self.repository_root / ".git"
            git_directory = git_marker.resolve(strict=True)
            parent = worktree_root.parent.resolve(strict=True)
        except OSError as exc:
            raise WorktreeError("worktree trust roots are unavailable") from exc
        if (
            git_marker.is_symlink()
            or not git_directory.is_dir()
            or git_directory.parent != self.repository_root
        ):
            raise WorktreeError("repository must have self-contained Git metadata")
        if (
            not worktree_root.is_absolute()
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", worktree_root.name)
        ):
            raise WorktreeError("worktree root name is invalid")
        self.worktree_root = parent / worktree_root.name
        self.lease_ledger = lease_ledger
        self.state_ledger = state_ledger
        if (
            self.worktree_root == self.repository_root
            or self.worktree_root.is_relative_to(self.repository_root)
            or self.repository_root.is_relative_to(self.worktree_root)
        ):
            raise WorktreeError("worktree root overlaps the repository")
        self._prepare_root()
        self._parent_identity = self._directory_identity(parent, allow_sticky=True)
        self._root_identity = self._directory_identity(self.worktree_root)

    def reserve(
        self,
        lease: TaskLease,
        *,
        attempt: int,
        base_sha: str,
    ) -> WorktreeReservation:
        """Reserve one empty no-checkout worktree from an active exact lease."""
        self._assert_storage_identity()
        if lease.state != "ACTIVE":
            raise WorktreeError("an active task lease is required")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 5:
            raise WorktreeError("worktree attempt is invalid")
        expected_branch = f"agent/{lease.task_id.lower()}/attempt-{attempt}"
        if lease.branch != expected_branch:
            raise WorktreeError("lease branch does not match the task attempt")
        if not isinstance(base_sha, str) or not SHA_PATTERN.fullmatch(base_sha):
            raise WorktreeError("worktree base Git identity is invalid")
        try:
            authoritative_lease = self.lease_ledger.heartbeat(
                lease.lease_id, owner=lease.owner, ttl_seconds=300
            )
            task_state = self.state_ledger.current(lease.task_id)
        except (LeaseError, WorkflowStateError) as exc:
            raise WorktreeError("authoritative dispatch state is unavailable") from exc
        if (
            authoritative_lease.task_id != lease.task_id
            or authoritative_lease.branch != lease.branch
            or task_state.attempt != attempt
            or task_state.base_sha != base_sha
            or task_state.state not in {"QUEUED", "LEASED"}
        ):
            raise WorktreeError("lease or task state does not authorize worktree")
        expected_revision = task_state.revision
        target = self.worktree_root / f"{lease.task_id.lower()}-attempt-{attempt}"
        if target.exists() or target.is_symlink():
            raise WorktreeError("task worktree path is already reserved")
        self._git("cat-file", "-e", f"{base_sha}^{{commit}}")
        reference = f"refs/heads/{lease.branch}"
        lock = self.worktree_root / f".{target.name}.reservation"
        try:
            lock.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise WorktreeError("task reservation is already active") from exc
        except OSError as exc:
            raise WorktreeError("task reservation lock failed") from exc
        branch_may_be_owned = False
        try:
            self._assert_storage_identity()
            if target.exists() or target.is_symlink():
                raise WorktreeError("task worktree path is already reserved")
            existing = self._run_git("show-ref", "--quiet", "--verify", reference)
            if existing.returncode != 1 or existing.stdout or existing.stderr:
                raise WorktreeError(
                    "task branch is already reserved or cannot be inspected"
                )
            update = self._run_git("update-ref", reference, base_sha, "0" * 40)
            if update.returncode != 0 or update.stdout or update.stderr:
                raise WorktreeError("task branch creation was rejected")
            branch_may_be_owned = True
            self._git(
                "worktree", "add", "--quiet", "--no-checkout",
                str(target), lease.branch,
            )
            entries = tuple(target.iterdir())
            marker = target / ".git"
            if entries != (marker,) or not marker.is_file() or marker.is_symlink():
                raise WorktreeError("reserved worktree is not metadata-only")
            refreshed = self.lease_ledger.heartbeat(
                lease.lease_id, owner=lease.owner, ttl_seconds=300
            )
            refreshed_state = self.state_ledger.current(lease.task_id)
            branch = self._run_git("rev-parse", "--verify", reference)
            if (
                branch.returncode != 0
                or branch.stdout != f"{base_sha}\n".encode()
                or branch.stderr
                or refreshed.lease_id != authoritative_lease.lease_id
                or refreshed_state.revision != expected_revision
                or refreshed_state.base_sha != base_sha
            ):
                raise WorktreeError("dispatch authority changed during reservation")
        except (OSError, WorktreeError, LeaseError, WorkflowStateError) as exc:
            if isinstance(exc, AmbiguousGitError):
                raise
            if branch_may_be_owned:
                self._rollback(target, reference, base_sha)
            try:
                lock.rmdir()
            except OSError as cleanup_error:
                raise WorktreeError("reservation lock cleanup failed") from cleanup_error
            if isinstance(exc, WorktreeError):
                raise
            raise WorktreeError("reservation authority or inspection failed") from exc
        return WorktreeReservation(
            task_id=lease.task_id,
            attempt=attempt,
            branch=lease.branch,
            base_sha=base_sha,
            path=target,
            lease_id=lease.lease_id,
        )

    def _assert_storage_identity(self) -> None:
        parent = self.worktree_root.parent
        if (
            self._directory_identity(parent, allow_sticky=True) != self._parent_identity
            or self._directory_identity(self.worktree_root) != self._root_identity
        ):
            raise WorktreeError("worktree storage identity changed")

    @staticmethod
    def _directory_identity(path: Path, *, allow_sticky: bool = False) -> tuple[int, int]:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise WorktreeError("worktree storage cannot be inspected") from exc
        mode = stat.S_IMODE(metadata.st_mode)
        private = metadata.st_uid == os.getuid() and not mode & 0o077
        sticky = allow_sticky and bool(mode & stat.S_ISVTX)
        if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink() or not (private or sticky):
            raise WorktreeError("worktree storage permissions are unsafe")
        return metadata.st_dev, metadata.st_ino

    def _prepare_root(self) -> None:
        try:
            self.worktree_root.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise WorktreeError("worktree root cannot be created") from exc
        try:
            metadata = self.worktree_root.lstat()
        except OSError as exc:
            raise WorktreeError("worktree root cannot be inspected") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise WorktreeError("worktree root permissions are unsafe")

    def _rollback(self, target: Path, reference: str, base_sha: str) -> None:
        self._assert_storage_identity()
        self._run_git("worktree", "remove", "--force", str(target))
        if target.exists() or target.is_symlink():
            try:
                if target.is_symlink():
                    target.unlink()
                else:
                    shutil.rmtree(target)
            except OSError as exc:
                raise WorktreeError("partial worktree cleanup failed") from exc
        listing = self._run_git("worktree", "list", "--porcelain").stdout
        if f"worktree {target}\n".encode() in listing:
            raise WorktreeError("partial worktree cleanup failed")
        deletion = self._run_git("update-ref", "-d", reference, base_sha)
        if deletion.returncode != 0:
            raise WorktreeError("partial branch cleanup failed")

    def _git(self, *arguments: str) -> None:
        result = self._run_git(*arguments)
        if result.returncode != 0 or result.stdout or result.stderr:
            raise WorktreeError("trusted Git command was rejected")

    def _run_git(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        command = [
            "/usr/bin/git",
            "-c", "core.hooksPath=/dev/null",
            "-c", "core.fsmonitor=false",
            "-c", "protocol.file.allow=never",
            "-C", str(self.repository_root),
            *arguments,
        ]
        environment = {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "HOME": "/dev/null",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
        }
        try:
            return subprocess.run(
                command, check=False, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30, env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise AmbiguousGitError("trusted Git outcome is ambiguous") from exc
        except OSError as exc:
            raise WorktreeError("trusted Git command failed") from exc

"""Register one no-checkout task worktree through live dual capabilities."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from .exact_branch_reservation import ReservedBranch
from .exact_ref_command import GIT, REF, SHA
from .exact_worktree_command import WORKSPACE_DESCRIPTOR_MARKER, build_exact_worktree_command
from .git_ref_outcome import (
    GitRefIndeterminateError, GitRefRejectedError, RefObservation,
    classify_ref_creation, require_created,
)
from .pinned_directory_executor import PinnedDirectoryExecutor, PinnedDirectoryExecutorError
from .review_runtime import ProcessOutput
from .workspace_identity_handle import (
    WorkspaceIdentity, WorkspaceIdentityHandle, WorkspaceIdentityHandleError,
)


class WorktreeRegistrationError(RuntimeError):
    """Raised when no-checkout registration is not confirmed."""


class WorktreeRegistrationRejectedError(WorktreeRegistrationError):
    """Raised when observation confirms no registration was created."""


class WorktreeRegistrationIndeterminateError(WorktreeRegistrationError):
    """Raised when durable registration state requires reconciliation."""


class WorkspaceObservation(Enum):
    ABSENT = "absent"
    EXACT = "exact"
    OTHER = "other"
    UNSAFE = "unsafe"


@dataclass(frozen=True, slots=True)
class RegisteredWorktree:
    ref_name: str
    commit_sha: str
    workspace: WorkspaceIdentity


def register_no_checkout_worktree(
    *, repository: PinnedDirectoryExecutor, workspace: WorkspaceIdentityHandle,
    workspace_descriptor: int, reserved_branch: ReservedBranch,
) -> RegisteredWorktree:
    """Register and directly observe one empty pinned workspace."""
    if (
        type(repository) is not PinnedDirectoryExecutor
        or type(workspace) is not WorkspaceIdentityHandle
        or type(reserved_branch) is not ReservedBranch
    ):
        raise WorktreeRegistrationRejectedError("worktree capability is invalid")
    identity = workspace.identity
    expected_ref = f"refs/heads/agent-{identity.task_id.lower()}-{identity.generation}"
    if (
        reserved_branch.ref_name != expected_ref
        or not REF.fullmatch(reserved_branch.ref_name)
        or not SHA.fullmatch(reserved_branch.commit_sha)
    ):
        raise WorktreeRegistrationRejectedError("reserved branch is invalid")
    command = build_exact_worktree_command(reserved_branch.ref_name)
    try:
        with workspace.hold_identity() as held_identity:
            if held_identity != identity:
                raise WorktreeRegistrationIndeterminateError("workspace identity changed")
            with repository.hold_execution():
                before = _observe_workspace(
                    repository, workspace_descriptor, identity,
                    command.environment, reserved_branch,
                )
                if before is not WorkspaceObservation.ABSENT:
                    raise WorktreeRegistrationIndeterminateError(
                        "workspace requires reconciliation"
                    )
                process_output: ProcessOutput | None = None
                runner_failed = False
                try:
                    process_output = repository.run_with_workspace_descriptor(
                        list(command.argv), environment=command.environment,
                        descriptor=workspace_descriptor,
                        expected_identity=identity, timeout_seconds=30,
                    )
                except PinnedDirectoryExecutorError:
                    runner_failed = True
                after = _observe_workspace(
                    repository, workspace_descriptor, identity,
                    command.environment, reserved_branch,
                )
                observation = RefObservation(after.value)
                outcome = classify_ref_creation(
                    launched=True, process_output=process_output,
                    runner_failed=runner_failed, observation=observation,
                )
                try:
                    require_created(outcome)
                except GitRefRejectedError:
                    raise WorktreeRegistrationRejectedError(
                        "worktree was not registered"
                    ) from None
                except GitRefIndeterminateError:
                    raise WorktreeRegistrationIndeterminateError(
                        "worktree requires reconciliation"
                    ) from None
    except WorktreeRegistrationError:
        try:
            repository.verify()
            workspace.verify()
        except (PinnedDirectoryExecutorError, WorkspaceIdentityHandleError):
            raise WorktreeRegistrationIndeterminateError(
                "worktree capability requires reconciliation"
            ) from None
        raise
    except (PinnedDirectoryExecutorError, WorkspaceIdentityHandleError):
        raise WorktreeRegistrationIndeterminateError(
            "worktree capability requires reconciliation"
        ) from None
    return RegisteredWorktree(
        reserved_branch.ref_name, reserved_branch.commit_sha, identity
    )


def _observe_workspace(
    repository: PinnedDirectoryExecutor, descriptor: int,
    identity: WorkspaceIdentity, environment: Mapping[str, str],
    branch: ReservedBranch,
) -> WorkspaceObservation:
    if isinstance(descriptor, bool) or not isinstance(descriptor, int):
        return WorkspaceObservation.UNSAFE
    try:
        duplicate = os.dup(descriptor)
        metadata = os.fstat(duplicate)
        entries = os.listdir(duplicate)
    except OSError:
        if "duplicate" in locals():
            try:
                os.close(duplicate)
            except OSError:
                pass
        return WorkspaceObservation.UNSAFE

    def finish(state: WorkspaceObservation) -> WorkspaceObservation:
        try:
            os.close(duplicate)
        except OSError:
            return WorkspaceObservation.UNSAFE
        return state

    valid_directory = (
        stat.S_ISDIR(metadata.st_mode)
        and stat.S_IMODE(metadata.st_mode) == 0o700
        and metadata.st_uid == os.geteuid()
        and (metadata.st_dev, metadata.st_ino) == (identity.device, identity.inode)
    )
    try:
        marker = os.stat(".git", dir_fd=duplicate, follow_symlinks=False)
    except FileNotFoundError:
        registration = _branch_registration(
            repository, environment, branch.ref_name, identity
        )
        return finish(
            WorkspaceObservation.ABSENT
            if valid_directory and not entries
            and registration is WorkspaceObservation.ABSENT
            else WorkspaceObservation.OTHER
        )
    except OSError:
        return finish(WorkspaceObservation.UNSAFE)
    if (
        not valid_directory or entries != [".git"]
        or not stat.S_ISREG(marker.st_mode) or marker.st_uid != os.geteuid()
        or not 0 < marker.st_size <= 4096
    ):
        return finish(WorkspaceObservation.OTHER)
    try:
        binding_before = repository.verify_worktree_admin_binding(
            descriptor=duplicate, expected_identity=identity
        )
    except PinnedDirectoryExecutorError:
        return finish(WorkspaceObservation.OTHER)
    registration = _branch_registration(
        repository, environment, branch.ref_name, identity
    )
    if registration is not WorkspaceObservation.EXACT:
        return finish(registration)
    commands = (
        ([GIT, "-c", "core.hooksPath=/dev/null", "-C",
          WORKSPACE_DESCRIPTOR_MARKER, "symbolic-ref", "-q", "HEAD"],
         branch.ref_name.encode("ascii") + b"\n"),
        ([GIT, "-c", "core.hooksPath=/dev/null", "-C",
          WORKSPACE_DESCRIPTOR_MARKER, "rev-parse", "--verify", "HEAD^{commit}"],
         branch.commit_sha.encode("ascii") + b"\n"),
    )
    for argv, expected in commands:
        try:
            output = repository.run_with_workspace_descriptor(
                argv, environment=dict(environment), descriptor=duplicate,
                expected_identity=identity, timeout_seconds=30,
            )
        except PinnedDirectoryExecutorError:
            return finish(WorkspaceObservation.UNSAFE)
        if output.returncode != 0 or output.stderr or output.stdout != expected:
            return finish(WorkspaceObservation.OTHER)
    try:
        binding_after = repository.verify_worktree_admin_binding(
            descriptor=duplicate, expected_identity=identity
        )
    except PinnedDirectoryExecutorError:
        return finish(WorkspaceObservation.UNSAFE)
    if binding_after != binding_before:
        return finish(WorkspaceObservation.OTHER)
    return finish(WorkspaceObservation.EXACT)

def _branch_registration(
    repository: PinnedDirectoryExecutor,
    environment: Mapping[str, str],
    ref_name: str,
    identity: WorkspaceIdentity,
) -> WorkspaceObservation:
    try:
        output = repository.run(
            [
                GIT, "-c", "core.hooksPath=/dev/null", "worktree", "list",
                "--porcelain", "-z",
            ],
            environment=dict(environment), timeout_seconds=30,
        )
    except PinnedDirectoryExecutorError:
        return WorkspaceObservation.UNSAFE
    if output.returncode != 0 or output.stderr:
        return WorkspaceObservation.UNSAFE
    branch_field = b"branch " + ref_name.encode("ascii")
    matches: list[bytes] = []
    for record in output.stdout.split(b"\x00\x00"):
        fields = record.split(b"\x00")
        if branch_field in fields:
            paths = [field[9:] for field in fields if field.startswith(b"worktree ")]
            if len(paths) != 1:
                return WorkspaceObservation.UNSAFE
            matches.append(paths[0])
    if not matches:
        return WorkspaceObservation.ABSENT
    if len(matches) != 1:
        return WorkspaceObservation.UNSAFE
    try:
        descriptor = os.open(
            matches[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        metadata = os.fstat(descriptor)
        os.close(descriptor)
    except OSError:
        return WorkspaceObservation.UNSAFE
    return (
        WorkspaceObservation.EXACT
        if (metadata.st_dev, metadata.st_ino) == (identity.device, identity.inode)
        else WorkspaceObservation.OTHER
    )

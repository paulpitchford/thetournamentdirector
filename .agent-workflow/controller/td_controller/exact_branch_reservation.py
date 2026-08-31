"""Reserve one exact task branch through live workspace and repository capabilities."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .exact_ref_command import GIT, ExactRefCommandError
from .exact_ref_hook_guard import (
    ExactRefHookGuardError,
    guarded_exact_ref_command,
)
from .git_ref_outcome import (
    GitRefIndeterminateError,
    GitRefRejectedError,
    RefObservation,
    classify_ref_creation,
    require_created,
)
from .pinned_directory_executor import (
    PinnedDirectoryExecutor,
    PinnedDirectoryExecutorError,
)
from .review_runtime import ProcessOutput
from .workspace_identity_handle import (
    WorkspaceIdentityHandle,
    WorkspaceIdentityHandleError,
)


class ExactBranchReservationError(RuntimeError):
    """Raised when exact branch reservation is not confirmed."""


class ExactBranchRejectedError(ExactBranchReservationError):
    """Raised when trusted evidence confirms no branch was created."""


class ExactBranchIndeterminateError(ExactBranchReservationError):
    """Raised when durable repository state requires reconciliation."""


@dataclass(frozen=True, slots=True)
class ReservedBranch:
    """Confirmed direct task branch identity."""

    ref_name: str
    commit_sha: str


def reserve_exact_task_branch(
    *,
    repository: PinnedDirectoryExecutor,
    workspace: WorkspaceIdentityHandle,
    commit_sha: str,
) -> ReservedBranch:
    """Atomically create and directly observe one workspace-derived branch."""
    if type(repository) is not PinnedDirectoryExecutor:
        raise ExactBranchRejectedError("repository capability is invalid")
    if type(workspace) is not WorkspaceIdentityHandle:
        raise ExactBranchRejectedError("workspace capability is invalid")
    identity = workspace.identity
    ref_name = (
        f"refs/heads/agent-{identity.task_id.lower()}-{identity.generation}"
    )
    try:
        command = guarded_exact_ref_command(ref_name, commit_sha)
    except (ExactRefCommandError, ExactRefHookGuardError):
        raise ExactBranchRejectedError("exact branch inputs are invalid") from None

    launched = False
    try:
        with workspace.hold_identity() as held_identity:
            if held_identity != identity:
                raise ExactBranchIndeterminateError(
                    "workspace identity changed before reservation"
                )
            with repository.hold_execution():
                process_output: ProcessOutput | None = None
                runner_failed = False
                launched = True
                try:
                    process_output = repository.run(
                        list(command.argv),
                        environment=command.environment,
                        timeout_seconds=30,
                    )
                except PinnedDirectoryExecutorError:
                    runner_failed = True
                observation = _observe_direct_ref(
                    repository, command.environment, ref_name, commit_sha
                )
                outcome = classify_ref_creation(
                    launched=launched,
                    process_output=process_output,
                    runner_failed=runner_failed,
                    observation=observation,
                )
                try:
                    require_created(outcome)
                except GitRefRejectedError:
                    raise ExactBranchRejectedError(
                        "exact branch was not created"
                    ) from None
                except GitRefIndeterminateError:
                    raise ExactBranchIndeterminateError(
                        "exact branch requires reconciliation"
                    ) from None
    except ExactBranchReservationError:
        try:
            repository.verify()
        except PinnedDirectoryExecutorError:
            raise ExactBranchIndeterminateError(
                "branch capability requires reconciliation"
            ) from None
        raise
    except (PinnedDirectoryExecutorError, WorkspaceIdentityHandleError):
        raise ExactBranchIndeterminateError(
            "branch capability requires reconciliation"
        ) from None
    return ReservedBranch(ref_name, commit_sha)


def _observe_direct_ref(
    repository: PinnedDirectoryExecutor,
    environment: Mapping[str, str],
    ref_name: str,
    commit_sha: str,
) -> RefObservation:
    """Observe exact object and symbolic-target fields without ref dereference ambiguity."""
    command = [
        GIT,
        "-c",
        "core.hooksPath=/dev/null",
        "for-each-ref",
        "--format=%(objectname)%00%(symref)",
        ref_name,
    ]
    try:
        output = repository.run(
            command,
            environment=dict(environment),
            timeout_seconds=30,
        )
    except PinnedDirectoryExecutorError:
        return RefObservation.UNSAFE
    if output.returncode != 0 or output.stderr:
        return RefObservation.UNSAFE
    if output.stdout == b"":
        return RefObservation.ABSENT
    if output.stdout == commit_sha.encode("ascii") + b"\x00\n":
        return RefObservation.EXACT
    return RefObservation.OTHER

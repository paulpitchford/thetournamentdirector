"""Load one exact verified Git blob through a pinned repository capability."""

from __future__ import annotations

from .exact_git_blob import (
    ExactGitBlobError,
    VerifiedGitBlob,
    build_exact_git_blob_command,
    verify_exact_git_blob,
)
from .git_tree_manifest import GitTreeEntry
from .pinned_directory_executor import (
    PinnedDirectoryExecutor,
    PinnedDirectoryExecutorError,
)


class RepositoryBlobLoadError(RuntimeError):
    """Raised when exact repository blob bytes cannot be confirmed."""


def load_verified_repository_blob(
    repository: PinnedDirectoryExecutor,
    entry: GitTreeEntry,
) -> VerifiedGitBlob:
    """Run only the reviewed cat-file command and verify its object digest."""
    if type(repository) is not PinnedDirectoryExecutor:
        raise RepositoryBlobLoadError("repository blob capability is invalid")
    try:
        command = build_exact_git_blob_command(entry)
    except ExactGitBlobError:
        raise RepositoryBlobLoadError("repository blob input is invalid") from None
    try:
        with repository.hold_execution():
            output = repository.run(
                list(command.argv), environment=command.environment,
                timeout_seconds=30,
            )
            if output.returncode != 0 or output.stderr:
                raise RepositoryBlobLoadError(
                    "repository blob command was not exact"
                )
            verified = verify_exact_git_blob(entry, output.stdout)
    except RepositoryBlobLoadError:
        raise
    except (ExactGitBlobError, PinnedDirectoryExecutorError):
        raise RepositoryBlobLoadError(
            "repository blob could not be confirmed"
        ) from None
    return verified

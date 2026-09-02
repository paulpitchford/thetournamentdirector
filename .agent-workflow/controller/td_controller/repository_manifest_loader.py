"""Load one exact Git tree manifest through a pinned repository capability."""

from __future__ import annotations

from dataclasses import dataclass

from .git_tree_manifest import (
    GitTreeEntry,
    GitTreeManifestError,
    build_git_tree_command,
    parse_git_tree_manifest,
)
from .pinned_directory_executor import (
    PinnedDirectoryExecutor,
    PinnedDirectoryExecutorError,
)


class RepositoryManifestLoadError(RuntimeError):
    """Raised when an exact commit manifest cannot be confirmed."""


@dataclass(frozen=True, slots=True)
class VerifiedRepositoryManifest:
    commit_sha: str
    entries: tuple[GitTreeEntry, ...]


def load_verified_repository_manifest(
    repository: PinnedDirectoryExecutor,
    commit_sha: str,
) -> VerifiedRepositoryManifest:
    """Run only the reviewed recursive ls-tree command and parse exact output."""
    if type(repository) is not PinnedDirectoryExecutor:
        raise RepositoryManifestLoadError("repository manifest capability is invalid")
    try:
        command = build_git_tree_command(commit_sha)
    except GitTreeManifestError:
        raise RepositoryManifestLoadError(
            "repository manifest input is invalid"
        ) from None
    try:
        with repository.hold_execution():
            output = repository.run(
                list(command.argv), environment=command.environment,
                timeout_seconds=30,
            )
            if output.returncode != 0 or output.stderr:
                raise RepositoryManifestLoadError(
                    "repository manifest command was not exact"
                )
            entries = parse_git_tree_manifest(output.stdout)
    except RepositoryManifestLoadError:
        raise
    except (GitTreeManifestError, PinnedDirectoryExecutorError):
        raise RepositoryManifestLoadError(
            "repository manifest could not be confirmed"
        ) from None
    return VerifiedRepositoryManifest(commit_sha, entries)

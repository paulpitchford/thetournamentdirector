"""Materialize one complete allowed Git manifest under a live workspace hold."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from .git_tree_manifest import DENIED_PARTS, MAX_ENTRIES, GitTreeEntry
from .pinned_directory_executor import PinnedDirectoryExecutor
from .repository_blob_loader import (
    RepositoryBlobLoadError,
    load_verified_repository_blob,
)
from .workspace_blob_writer import (
    WorkspaceBlobIndeterminateError,
    WorkspaceBlobRejectedError,
    _write_workspace_blob,
)
from .workspace_identity_handle import (
    WorkspaceIdentityHandle,
    WorkspaceIdentityHandleError,
)

SHA = re.compile(r"[0-9a-f]{40}")
SAFE_PATH = re.compile(r"[A-Za-z0-9._/-]{1,512}")


class WorkspaceTreeMaterializerError(RuntimeError):
    """Raised when a complete allowed tree is not confirmed."""


class WorkspaceTreeRejectedError(WorkspaceTreeMaterializerError):
    """Raised when no workspace effect occurred."""


class WorkspaceTreeIndeterminateError(WorkspaceTreeMaterializerError):
    """Raised when a partial tree or cleanup uncertainty needs reconciliation."""


@dataclass(frozen=True, slots=True)
class MaterializedWorkspaceTree:
    written_paths: tuple[PurePosixPath, ...]
    skipped_paths: tuple[PurePosixPath, ...]


def materialize_workspace_tree(
    *,
    repository: PinnedDirectoryExecutor,
    workspace: WorkspaceIdentityHandle,
    descriptor: int,
    manifest: tuple[GitTreeEntry, ...],
) -> MaterializedWorkspaceTree:
    """Load and publish every allowed entry while excluding controller peers."""
    if (
        type(repository) is not PinnedDirectoryExecutor
        or type(workspace) is not WorkspaceIdentityHandle
        or not _valid_manifest(manifest)
    ):
        raise WorkspaceTreeRejectedError("workspace tree input is invalid")
    written: list[PurePosixPath] = []
    skipped = tuple(entry.path for entry in manifest if not entry.materialize)
    hold_entered = False
    try:
        with workspace.hold_identity() as identity:
            hold_entered = True
            for entry in manifest:
                if not entry.materialize:
                    continue
                blob = load_verified_repository_blob(repository, entry)
                _write_workspace_blob(
                    descriptor=descriptor, expected_identity=identity,
                    entry=entry, blob=blob,
                )
                written.append(entry.path)
    except WorkspaceIdentityHandleError:
        if not hold_entered and not written:
            raise WorkspaceTreeRejectedError(
                "workspace tree hold is unavailable"
            ) from None
        raise WorkspaceTreeIndeterminateError(
            "workspace tree hold requires reconciliation"
        ) from None
    except (RepositoryBlobLoadError, WorkspaceBlobRejectedError):
        if not written:
            raise WorkspaceTreeRejectedError(
                "workspace tree was not materialized"
            ) from None
        raise WorkspaceTreeIndeterminateError(
            "partial workspace tree requires reconciliation"
        ) from None
    except WorkspaceBlobIndeterminateError:
        raise WorkspaceTreeIndeterminateError(
            "workspace tree requires reconciliation"
        ) from None
    return MaterializedWorkspaceTree(tuple(written), skipped)


def _valid_manifest(manifest: object) -> bool:
    if type(manifest) is not tuple or len(manifest) > MAX_ENTRIES:
        return False
    previous: PurePosixPath | None = None
    seen: set[PurePosixPath] = set()
    for entry in manifest:
        if (
            type(entry) is not GitTreeEntry
            or type(entry.path) is not PurePosixPath
            or type(entry.executable) is not bool
            or type(entry.materialize) is not bool
            or not isinstance(entry.blob_sha, str)
            or SHA.fullmatch(entry.blob_sha) is None
            or SAFE_PATH.fullmatch(entry.path.as_posix()) is None
            or not entry.path.parts
            or entry.path.is_absolute()
            or ".." in entry.path.parts
            or any(parent in seen for parent in entry.path.parents)
            or entry.materialize
            == any(part in DENIED_PARTS for part in entry.path.parts)
            or (
                entry.path.name.startswith(".td-")
                and entry.path.name.endswith(".partial")
            )
            or (previous is not None and entry.path.as_posix() <= previous.as_posix())
        ):
            return False
        seen.add(entry.path)
        previous = entry.path
    return True

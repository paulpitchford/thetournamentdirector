"""Verify an exact materialized tree beneath a held workspace descriptor."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import PurePosixPath

from .bounded_fd_read import BoundedFdReadError, MAX_FD_READ_BYTES, read_exact_fd
from .exact_git_blob import ExactGitBlobError, verify_exact_git_blob
from .git_tree_manifest import GitTreeEntry
from .pinned_directory_executor import (
    PinnedDirectoryExecutor,
    PinnedDirectoryExecutorError,
    WorktreeAdminBinding,
)
from .workspace_identity_handle import (
    WorkspaceIdentity,
    WorkspaceIdentityHandle,
    WorkspaceIdentityHandleError,
)
from .workspace_tree_materializer import _valid_manifest


class WorkspaceTreeVerificationError(RuntimeError):
    """Raised unless the exact allowed manifest is confirmed in the workspace."""


@dataclass(frozen=True, slots=True)
class VerifiedWorkspaceTree:
    file_count: int
    directory_count: int


def verify_workspace_tree(
    *,
    repository: PinnedDirectoryExecutor,
    workspace: WorkspaceIdentityHandle,
    descriptor: int,
    manifest: tuple[GitTreeEntry, ...],
) -> VerifiedWorkspaceTree:
    """Verify binding, modes, bytes and no extras under controller exclusion."""
    if (
        type(repository) is not PinnedDirectoryExecutor
        or type(workspace) is not WorkspaceIdentityHandle
        or isinstance(descriptor, bool)
        or not isinstance(descriptor, int)
        or descriptor < 0
        or not _valid_manifest(manifest)
    ):
        raise WorkspaceTreeVerificationError("workspace tree input is invalid")
    files = {entry.path: entry for entry in manifest if entry.materialize}
    directories = {
        parent
        for path in files
        for parent in path.parents
        if parent != PurePosixPath(".")
    }
    try:
        with workspace.hold_identity() as identity:
            binding = repository.verify_worktree_admin_binding(
                descriptor=descriptor, expected_identity=identity
            )
            seen_files: set[PurePosixPath] = set()
            seen_directories: set[PurePosixPath] = set()
            _verify_directory(
                descriptor, identity, binding, PurePosixPath("."),
                files, directories, seen_files, seen_directories,
                is_root=True,
            )
            if seen_files != set(files) or seen_directories != directories:
                raise OSError("workspace tree is incomplete")
    except (
        BoundedFdReadError,
        ExactGitBlobError,
        OSError,
        PinnedDirectoryExecutorError,
        WorkspaceIdentityHandleError,
    ):
        raise WorkspaceTreeVerificationError(
            "workspace tree could not be confirmed"
        ) from None
    return VerifiedWorkspaceTree(len(seen_files), len(seen_directories))


def _verify_directory(
    descriptor: int,
    identity: WorkspaceIdentity,
    binding: WorktreeAdminBinding,
    relative: PurePosixPath,
    files: dict[PurePosixPath, GitTreeEntry],
    directories: set[PurePosixPath],
    seen_files: set[PurePosixPath],
    seen_directories: set[PurePosixPath],
    *,
    is_root: bool,
) -> None:
    directory_fd = os.dup(descriptor)
    failed = False
    try:
        metadata = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or (
                is_root
                and (metadata.st_dev, metadata.st_ino)
                != (identity.device, identity.inode)
            )
        ):
            raise OSError("workspace directory metadata is invalid")
        names = os.listdir(directory_fd)
        if len(names) > len(files) + len(directories) + 1:
            raise OSError("workspace directory listing is oversized")
        marker_seen = False
        for name in sorted(names):
            path = PurePosixPath(name) if is_root else relative / name
            if is_root and name == ".git":
                marker_seen = True
                marker_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory_fd,
                )
                try:
                    _require_marker(marker_fd, binding)
                finally:
                    os.close(marker_fd)
            elif path in directories:
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory_fd,
                )
                try:
                    seen_directories.add(path)
                    _verify_directory(
                        child_fd, identity, binding, path, files, directories,
                        seen_files, seen_directories, is_root=False,
                    )
                finally:
                    os.close(child_fd)
            elif path in files:
                file_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory_fd,
                )
                try:
                    _verify_file(file_fd, files[path])
                    seen_files.add(path)
                finally:
                    os.close(file_fd)
            else:
                raise OSError("workspace tree has an unexpected path")
        if is_root and not marker_seen:
            raise OSError("workspace marker is absent")
    except (BoundedFdReadError, ExactGitBlobError, OSError):
        failed = True
    try:
        os.close(directory_fd)
    except OSError:
        failed = True
    if failed:
        raise OSError("workspace directory verification failed")


def _verify_file(descriptor: int, entry: GitTreeEntry) -> None:
    metadata = os.fstat(descriptor)
    expected_mode = 0o755 if entry.executable else 0o644
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != expected_mode
        or metadata.st_size > MAX_FD_READ_BYTES
    ):
        raise OSError("workspace file metadata is invalid")
    payload = read_exact_fd(descriptor, metadata.st_size)
    verify_exact_git_blob(entry, payload)


def _require_marker(
    descriptor: int, binding: WorktreeAdminBinding
) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or not 1 <= metadata.st_size <= 4096
        or (metadata.st_dev, metadata.st_ino)
        != (binding.marker_device, binding.marker_inode)
    ):
        raise OSError("workspace marker metadata is invalid")

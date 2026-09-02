"""Atomically publish one verified Git blob beneath a pinned workspace fd."""

from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass
from pathlib import PurePosixPath

from .bounded_fd_read import BoundedFdReadError, read_exact_fd
from .exact_git_blob import (
    ExactGitBlobError,
    VerifiedGitBlob,
    verify_exact_git_blob,
)
from .git_tree_manifest import DENIED_PARTS, GitTreeEntry
from .workspace_identity_handle import (
    WorkspaceIdentity,
    WorkspaceIdentityHandle,
    WorkspaceIdentityHandleError,
)


class WorkspaceBlobWriterError(RuntimeError):
    """Raised when a verified blob is not confirmed at its exact path."""


class WorkspaceBlobRejectedError(WorkspaceBlobWriterError):
    """Raised when validation rejects before any filesystem effect."""


class WorkspaceBlobIndeterminateError(WorkspaceBlobWriterError):
    """Raised after any effect or cleanup uncertainty."""


@dataclass(frozen=True, slots=True)
class WrittenWorkspaceBlob:
    path: PurePosixPath
    blob_sha: str
    executable: bool
    device: int
    inode: int


def write_workspace_blob(
    *,
    workspace: WorkspaceIdentityHandle,
    descriptor: int,
    entry: GitTreeEntry,
    blob: VerifiedGitBlob,
) -> WrittenWorkspaceBlob:
    """Publish while the live workspace hold excludes controller peers."""
    if type(workspace) is not WorkspaceIdentityHandle:
        raise WorkspaceBlobRejectedError("workspace blob input is invalid")
    hold_entered = False
    try:
        with workspace.hold_identity() as identity:
            hold_entered = True
            result = _write_workspace_blob(
                descriptor=descriptor, expected_identity=identity,
                entry=entry, blob=blob,
            )
    except WorkspaceIdentityHandleError:
        if hold_entered:
            raise WorkspaceBlobIndeterminateError(
                "workspace hold release requires reconciliation"
            ) from None
        raise WorkspaceBlobRejectedError("workspace hold is unavailable") from None
    return result


def _write_workspace_blob(
    *, descriptor: int, expected_identity: WorkspaceIdentity,
    entry: GitTreeEntry, blob: VerifiedGitBlob,
) -> WrittenWorkspaceBlob:
    if not _valid_inputs(descriptor, expected_identity, entry, blob):
        raise WorkspaceBlobRejectedError("workspace blob input is invalid")
    try:
        verified = verify_exact_git_blob(entry, blob.payload)
    except ExactGitBlobError:
        raise WorkspaceBlobRejectedError("workspace blob input is invalid") from None
    if verified != blob:
        raise WorkspaceBlobRejectedError("workspace blob input is invalid")
    opened: list[int] = []
    any_effect = False
    temporary_created = False
    temporary = f".td-{entry.blob_sha}.partial"
    parent_fd = -1
    published = False
    result: WrittenWorkspaceBlob | None = None
    try:
        root_fd = os.dup(descriptor)
        opened.append(root_fd)
        _require_directory(root_fd, expected_identity)
        parent_fd = root_fd
        for component in entry.path.parts[:-1]:
            try:
                os.mkdir(component, mode=0o700, dir_fd=parent_fd)
                any_effect = True
            except FileExistsError:
                pass
            child_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
            opened.append(child_fd)
            _require_owned_directory(child_fd)
            parent_fd = child_fd
        name = entry.path.name
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(errno.EEXIST, "workspace target exists")
        file_fd = os.open(
            temporary,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_fd,
        )
        opened.append(file_fd)
        any_effect = True
        temporary_created = True
        pending = memoryview(blob.payload)
        while pending:
            written = os.write(file_fd, pending)
            if written <= 0:
                raise OSError("workspace blob write made no progress")
            pending = pending[written:]
        final_mode = 0o755 if entry.executable else 0o644
        os.fchmod(file_fd, final_mode)
        file_stat = os.fstat(file_fd)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.geteuid()
            or stat.S_IMODE(file_stat.st_mode) != final_mode
            or file_stat.st_size != len(blob.payload)
        ):
            raise OSError("workspace blob metadata is invalid")
        os.fsync(file_fd)
        os.link(
            temporary, name,
            src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
        published = True
        os.unlink(temporary, dir_fd=parent_fd)
        temporary_created = False
        os.lseek(file_fd, 0, os.SEEK_SET)
        disk_payload = read_exact_fd(file_fd, len(blob.payload))
        if verify_exact_git_blob(entry, disk_payload) != blob:
            raise OSError("published workspace blob changed")
        file_stat = os.fstat(file_fd)
        if file_stat.st_nlink != 1:
            raise OSError("published workspace link count is invalid")
        for directory_fd in reversed(opened[:-1]):
            os.fsync(directory_fd)
        # Opening the final path under the controller-wide workspace hold is the
        # publication linearization point. Exact bytes are read through that fd.
        published_fd, published_stat = _open_published(root_fd, entry.path)
        opened.append(published_fd)
        if (published_stat.st_dev, published_stat.st_ino) != (
            file_stat.st_dev, file_stat.st_ino
        ):
            raise OSError("published workspace path changed")
        final_payload = read_exact_fd(published_fd, len(blob.payload))
        if verify_exact_git_blob(entry, final_payload) != blob:
            raise OSError("published workspace blob changed")
        result = WrittenWorkspaceBlob(
            entry.path, entry.blob_sha, entry.executable,
            published_stat.st_dev, published_stat.st_ino,
        )
    except (BoundedFdReadError, ExactGitBlobError, OSError):
        if temporary_created and parent_fd >= 0:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
                temporary_created = False
            except OSError:
                pass
    cleanup_failed = False
    for opened_fd in reversed(opened):
        try:
            os.close(opened_fd)
        except OSError:
            cleanup_failed = True
    if result is not None and not cleanup_failed and not temporary_created:
        return result
    if any_effect or published or cleanup_failed or temporary_created:
        raise WorkspaceBlobIndeterminateError(
            "workspace blob requires reconciliation"
        ) from None
    raise WorkspaceBlobRejectedError("workspace blob was not written") from None


def _valid_inputs(
    descriptor: int,
    identity: WorkspaceIdentity,
    entry: GitTreeEntry,
    blob: VerifiedGitBlob,
) -> bool:
    return (
        not isinstance(descriptor, bool)
        and isinstance(descriptor, int)
        and descriptor >= 0
        and type(identity) is WorkspaceIdentity
        and type(entry) is GitTreeEntry
        and type(blob) is VerifiedGitBlob
        and entry.materialize
        and entry.path.parts
        and not entry.path.is_absolute()
        and ".." not in entry.path.parts
        and not any(part in DENIED_PARTS for part in entry.path.parts)
        and not (
            entry.path.name.startswith(".td-")
            and entry.path.name.endswith(".partial")
        )
        and blob.blob_sha == entry.blob_sha
        and blob.executable is entry.executable
    )


def _open_published(
    root_descriptor: int, path: PurePosixPath
) -> tuple[int, os.stat_result]:
    directories: list[int] = []
    file_fd = -1
    try:
        parent_fd = os.dup(root_descriptor)
        directories.append(parent_fd)
        for component in path.parts[:-1]:
            parent_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
            directories.append(parent_fd)
        file_fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        metadata = os.fstat(file_fd)
    except OSError:
        metadata = None
    cleanup_failed = False
    for directory_fd in reversed(directories):
        try:
            os.close(directory_fd)
        except OSError:
            cleanup_failed = True
    if metadata is None or cleanup_failed:
        if file_fd >= 0:
            try:
                os.close(file_fd)
            except OSError:
                pass
        raise OSError("published workspace path is unavailable")
    return file_fd, metadata


def _require_directory(descriptor: int, identity: WorkspaceIdentity) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
        or (metadata.st_dev, metadata.st_ino) != (identity.device, identity.inode)
    ):
        raise OSError("workspace root identity is invalid")


def _require_owned_directory(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise OSError("workspace directory is invalid")

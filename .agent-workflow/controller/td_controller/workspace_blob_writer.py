"""Atomically publish one verified Git blob beneath a pinned workspace fd."""

from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass
from pathlib import PurePosixPath

from .exact_git_blob import (
    ExactGitBlobError,
    VerifiedGitBlob,
    verify_exact_git_blob,
)
from .git_tree_manifest import DENIED_PARTS, GitTreeEntry
from .workspace_identity_handle import WorkspaceIdentity


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
    descriptor: int,
    expected_identity: WorkspaceIdentity,
    entry: GitTreeEntry,
    blob: VerifiedGitBlob,
) -> WrittenWorkspaceBlob:
    """Publish through descriptor-relative no-follow directories and hard-link commit."""
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
        disk_payload = os.read(file_fd, len(blob.payload) + 1)
        if verify_exact_git_blob(entry, disk_payload) != blob:
            raise OSError("published workspace blob changed")
        file_stat = os.fstat(file_fd)
        if file_stat.st_nlink != 1:
            raise OSError("published workspace link count is invalid")
        resolved_stat = _resolve_published(root_fd, entry.path)
        if (resolved_stat.st_dev, resolved_stat.st_ino) != (
            file_stat.st_dev, file_stat.st_ino
        ):
            raise OSError("published workspace path changed")
        os.lseek(file_fd, 0, os.SEEK_SET)
        final_payload = os.read(file_fd, len(blob.payload) + 1)
        if verify_exact_git_blob(entry, final_payload) != blob:
            raise OSError("published workspace blob changed")
        file_stat = os.fstat(file_fd)
        for directory_fd in reversed(opened[:-1]):
            os.fsync(directory_fd)
        result = WrittenWorkspaceBlob(
            entry.path, entry.blob_sha, entry.executable,
            file_stat.st_dev, file_stat.st_ino,
        )
    except (ExactGitBlobError, OSError):
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


def _resolve_published(
    root_descriptor: int, path: PurePosixPath
) -> os.stat_result:
    opened: list[int] = []
    try:
        parent_fd = os.dup(root_descriptor)
        opened.append(parent_fd)
        for component in path.parts[:-1]:
            parent_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
            opened.append(parent_fd)
        file_fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        opened.append(file_fd)
        metadata = os.fstat(file_fd)
    except OSError:
        metadata = None
    cleanup_failed = False
    for opened_fd in reversed(opened):
        try:
            os.close(opened_fd)
        except OSError:
            cleanup_failed = True
    if metadata is None or cleanup_failed:
        raise OSError("published workspace path is unavailable")
    return metadata


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

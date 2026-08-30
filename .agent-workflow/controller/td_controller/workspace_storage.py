"""Descriptor-anchored private storage for isolated task workspaces."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .task_contract import TASK_ID_PATTERN

ROOT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
GENERATION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")


class WorkspaceStorageError(RuntimeError):
    """Raised when private workspace storage cannot be used safely."""


@dataclass(frozen=True)
class WorkspaceAnchor:
    """Identity of one descriptor-anchored empty task directory."""

    task_id: str
    attempt: int
    name: str
    lock_name: str
    generation: str
    device: int
    inode: int


class WorkspaceStorage:
    """Own workspace directories relative to pinned parent and root descriptors."""

    def __init__(self, root: Path, *, generation_factory: Callable[[], str]) -> None:
        if not root.is_absolute() or not ROOT_NAME_PATTERN.fullmatch(root.name):
            raise WorkspaceStorageError("workspace root name is invalid")
        parent_fd: int | None = None
        root_fd: int | None = None
        try:
            parent_fd = os.open(
                root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            self._validate_parent(os.fstat(parent_fd))
            try:
                os.mkdir(root.name, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            root_fd = os.open(
                root.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            self._validate_owned_private_directory(os.fstat(root_fd))
        except Exception as exc:
            if root_fd is not None:
                os.close(root_fd)
            if parent_fd is not None:
                os.close(parent_fd)
            if isinstance(exc, OSError):
                raise WorkspaceStorageError("workspace storage is unavailable") from exc
            raise
        self.root = root
        self._generation_factory = generation_factory
        self._parent_fd = parent_fd
        self._root_fd = root_fd
        self._root_identity = self._identity(os.fstat(root_fd))

    def close(self) -> None:
        """Close pinned descriptors exactly once."""
        if self._root_fd is None or self._parent_fd is None:
            return
        os.close(self._root_fd)
        os.close(self._parent_fd)
        self._root_fd = None
        self._parent_fd = None

    def __enter__(self) -> WorkspaceStorage:
        self._require_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def reserve(self, task_id: str, *, attempt: int) -> WorkspaceAnchor:
        """Atomically reserve one empty direct-child task directory."""
        self._validate_task(task_id, attempt)
        root_fd = self._require_open()
        logical_name = f"{task_id.lower()}-attempt-{attempt}"
        generation = self._generation_factory()
        if not isinstance(generation, str) or not GENERATION_PATTERN.fullmatch(generation):
            raise WorkspaceStorageError("workspace generation is invalid")
        lock_name = f".{logical_name}.lock"
        name = f"{logical_name}-{generation}"
        try:
            os.mkdir(lock_name, mode=0o700, dir_fd=root_fd)
            os.mkdir(name, mode=0o700, dir_fd=root_fd)
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
        except FileExistsError as exc:
            raise WorkspaceStorageError("workspace is already reserved") from exc
        except OSError as exc:
            raise WorkspaceStorageError("workspace reservation failed") from exc
        try:
            metadata = os.fstat(descriptor)
            self._validate_owned_private_directory(metadata)
            if os.listdir(descriptor):
                raise WorkspaceStorageError("reserved workspace is not empty")
            device, inode = self._identity(metadata)
        except Exception:
            os.close(descriptor)
            raise WorkspaceStorageError("workspace inspection failed")
        os.close(descriptor)
        return WorkspaceAnchor(
            task_id, attempt, name, lock_name, generation, device, inode
        )

    def open_anchor(self, anchor: WorkspaceAnchor) -> int:
        """Open an existing anchor by no-follow relative lookup; caller closes it."""
        self._validate_anchor(anchor)
        root_fd = self._require_open()
        try:
            descriptor = os.open(
                anchor.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
            metadata = os.fstat(descriptor)
        except OSError as exc:
            raise WorkspaceStorageError("workspace anchor is unavailable") from exc
        if self._identity(metadata) != (anchor.device, anchor.inode):
            os.close(descriptor)
            raise WorkspaceStorageError("workspace anchor identity changed")
        return descriptor

    def duplicate_root(self) -> int:
        """Return a duplicate pinned root descriptor for a bounded child process."""
        try:
            return os.dup(self._require_open())
        except OSError as exc:
            raise WorkspaceStorageError("workspace root duplication failed") from exc

    def release_empty(self, anchor: WorkspaceAnchor) -> None:
        """Quarantine, reverify, and remove the exact empty anchored directory."""
        descriptor = self.open_anchor(anchor)
        root_fd = self._require_open()
        quarantine = f".release-{anchor.generation}"
        quarantined: int | None = None
        try:
            os.rename(
                anchor.name, quarantine, src_dir_fd=root_fd, dst_dir_fd=root_fd
            )
            quarantined = os.open(
                quarantine,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
            metadata = os.fstat(quarantined)
            if self._identity(metadata) != (anchor.device, anchor.inode):
                raise WorkspaceStorageError("workspace release identity changed")
            if os.listdir(quarantined):
                raise WorkspaceStorageError("workspace release requires an empty anchor")
            os.close(quarantined)
            quarantined = None
            os.rmdir(quarantine, dir_fd=root_fd)
            os.rmdir(anchor.lock_name, dir_fd=root_fd)
        except OSError as exc:
            raise WorkspaceStorageError("workspace release failed") from exc
        finally:
            if quarantined is not None:
                os.close(quarantined)
            os.close(descriptor)

    def _require_open(self) -> int:
        if self._root_fd is None or self._parent_fd is None:
            raise WorkspaceStorageError("workspace storage is closed")
        if self._identity(os.fstat(self._root_fd)) != self._root_identity:
            raise WorkspaceStorageError("workspace root identity changed")
        return self._root_fd

    @staticmethod
    def _validate_parent(metadata: os.stat_result) -> None:
        mode = stat.S_IMODE(metadata.st_mode)
        private = metadata.st_uid == os.getuid() and not mode & 0o077
        root_sticky = metadata.st_uid == 0 and bool(mode & stat.S_ISVTX)
        if not stat.S_ISDIR(metadata.st_mode) or not (private or root_sticky):
            raise WorkspaceStorageError("workspace parent permissions are unsafe")

    @staticmethod
    def _validate_owned_private_directory(metadata: os.stat_result) -> None:
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise WorkspaceStorageError("workspace directory permissions are unsafe")

    @staticmethod
    def _identity(metadata: os.stat_result) -> tuple[int, int]:
        return metadata.st_dev, metadata.st_ino

    @staticmethod
    def _validate_task(task_id: str, attempt: int) -> None:
        if (
            not isinstance(task_id, str)
            or len(task_id) > 128
            or not TASK_ID_PATTERN.fullmatch(task_id)
        ):
            raise WorkspaceStorageError("workspace task ID is invalid")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 5:
            raise WorkspaceStorageError("workspace attempt is invalid")

    @staticmethod
    def _validate_anchor(anchor: WorkspaceAnchor) -> None:
        if not isinstance(anchor, WorkspaceAnchor):
            raise WorkspaceStorageError("workspace anchor is invalid")
        WorkspaceStorage._validate_task(anchor.task_id, anchor.attempt)
        logical = f"{anchor.task_id.lower()}-attempt-{anchor.attempt}"
        if (
            not GENERATION_PATTERN.fullmatch(anchor.generation)
            or anchor.name != f"{logical}-{anchor.generation}"
            or anchor.lock_name != f".{logical}.lock"
            or anchor.device < 0
            or anchor.inode <= 0
        ):
            raise WorkspaceStorageError("workspace anchor is invalid")

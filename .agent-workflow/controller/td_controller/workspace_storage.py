"""Pinned capabilities for controller-owned task workspace storage."""

from __future__ import annotations

import fcntl
import os
import re
import secrets
import stat
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

TASK_PATTERN = re.compile(r"[A-Z][A-Z0-9-]{2,63}")
ROOT_PATTERN = re.compile(r"[a-z][a-z0-9-]{2,63}")
GENERATION_PATTERN = re.compile(r"[0-9a-f]{32}")

class WorkspaceStorageError(RuntimeError):
    """Raised when workspace storage cannot preserve its security invariants."""


@dataclass(frozen=True, slots=True)
class WorkspaceAnchor:
    task_id: str
    attempt: int
    name: str
    lock_name: str
    generation: str
    device: int
    inode: int
    lock_device: int
    lock_inode: int


@dataclass(slots=True)
class _PinnedAnchor:
    workspace_fd: int
    lock_fd: int


class WorkspaceStorage:
    def __init__(
        self,
        parent: Path,
        root_name: str,
        *,
        generation_factory: Callable[[], str] | None = None,
    ) -> None:
        if not parent.is_absolute() or parent.resolve(strict=True) != parent:
            raise WorkspaceStorageError("workspace parent must be canonical")
        if not ROOT_PATTERN.fullmatch(root_name):
            raise WorkspaceStorageError("workspace root name is invalid")
        self._generation_factory = generation_factory or (
            lambda: secrets.token_hex(16)
        )
        self._mutex = threading.RLock()
        self._anchors: dict[WorkspaceAnchor, _PinnedAnchor] = {}
        self._parent_fd = -1
        self._root_fd = -1
        try:
            expected_parent = self._identity(os.stat(parent, follow_symlinks=False))
            self._parent_fd = os.open(
                parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            parent_metadata = os.fstat(self._parent_fd)
            self._validate_private(parent_metadata)
            if self._identity(parent_metadata) != expected_parent:
                raise WorkspaceStorageError("workspace parent identity changed")
            try:
                os.mkdir(root_name, mode=0o700, dir_fd=self._parent_fd)
            except FileExistsError:
                pass
            self._root_fd = os.open(
                root_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=self._parent_fd,
            )
            root_metadata = os.fstat(self._root_fd)
            self._validate_private(root_metadata)
            self._root_identity = self._identity(root_metadata)
        except Exception as exc:
            self._close_descriptors()
            if isinstance(exc, WorkspaceStorageError):
                raise
            raise WorkspaceStorageError("workspace storage initialization failed") from exc

    def __enter__(self) -> WorkspaceStorage:
        return self
    def __exit__(self, *_: object) -> None:
        self.close()
    def reserve(self, task_id: str, *, attempt: int) -> WorkspaceAnchor:
        self._validate_task(task_id, attempt)
        with self._serialized():
            generation = self._generation_factory()
            if not isinstance(generation, str) or not GENERATION_PATTERN.fullmatch(
                generation
            ):
                raise WorkspaceStorageError("workspace generation is invalid")
            logical_name = f"{task_id.lower()}-attempt-{attempt}"
            if any(anchor.lock_name == f".{logical_name}.lock" for anchor in self._anchors):
                raise WorkspaceStorageError("workspace is already reserved")
            lock_name = f".{logical_name}.lock"
            lock_fd = self._open_lock(lock_name)
            name = f"{logical_name}-{generation}"
            workspace_fd = -1
            created = False
            try:
                os.mkdir(name, mode=0o700, dir_fd=self._root_fd)
                created = True
                workspace_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=self._root_fd,
                )
                workspace_metadata = os.fstat(workspace_fd)
                lock_metadata = os.fstat(lock_fd)
                self._validate_private(workspace_metadata)
                self._validate_lock(lock_metadata)
                if os.listdir(workspace_fd):
                    raise WorkspaceStorageError("reserved workspace is not empty")
                workspace_device, workspace_inode = self._identity(workspace_metadata)
                lock_device, lock_inode = self._identity(lock_metadata)
            except Exception as exc:
                cleanup_error: Exception | None = None
                try:
                    if created:
                        self._quarantine(name, f".failed-{name}")
                except Exception as cleanup:
                    cleanup_error = cleanup
                if workspace_fd >= 0:
                    os.close(workspace_fd)
                self._unlock_close(lock_fd)
                if cleanup_error is not None:
                    raise cleanup_error
                if isinstance(exc, WorkspaceStorageError):
                    raise
                raise WorkspaceStorageError("workspace reservation failed") from exc
            anchor = WorkspaceAnchor(
                task_id,
                attempt,
                name,
                lock_name,
                generation,
                workspace_device,
                workspace_inode,
                lock_device,
                lock_inode,
            )
            self._anchors[anchor] = _PinnedAnchor(workspace_fd, lock_fd)
            return anchor
    def open_anchor(self, anchor: WorkspaceAnchor) -> int:
        with self._mutex:
            self._require_open()
            pinned = self._anchors.get(anchor)
            if pinned is None:
                raise WorkspaceStorageError("workspace anchor is unavailable")
            self._validate_pinned(anchor, pinned)
            try:
                return os.dup(pinned.workspace_fd)
            except OSError as exc:
                raise WorkspaceStorageError("workspace duplication failed") from exc
    def release_empty(self, anchor: WorkspaceAnchor) -> None:
        with self._serialized():
            pinned = self._anchors.get(anchor)
            if pinned is None:
                raise WorkspaceStorageError("workspace anchor is unavailable")
            self._validate_pinned(anchor, pinned)
            if os.listdir(pinned.workspace_fd):
                raise WorkspaceStorageError("workspace release requires an empty directory")
            self._verify_entry(anchor.name, anchor.device, anchor.inode, directory=True)
            self._verify_entry(
                anchor.lock_name, anchor.lock_device, anchor.lock_inode, directory=False
            )
            released_name = f".released-{anchor.name}"
            self._require_absent(released_name)
            try:
                os.rename(
                    anchor.name,
                    released_name,
                    src_dir_fd=self._root_fd,
                    dst_dir_fd=self._root_fd,
                )
            except OSError as exc:
                raise WorkspaceStorageError("workspace release failed") from exc
            self._verify_entry(released_name, anchor.device, anchor.inode, directory=True)
            del self._anchors[anchor]
            os.close(pinned.workspace_fd)
            self._unlock_close(pinned.lock_fd)
    def close(self) -> None:
        with self._mutex:
            if self._anchors:
                raise WorkspaceStorageError("active workspace anchors prevent close")
            self._close_descriptors()
    def _open_lock(self, lock_name: str) -> int:
        try:
            descriptor = os.open(
                lock_name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._root_fd,
            )
            self._validate_lock(os.fstat(descriptor))
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except OSError as exc:
            if "descriptor" in locals():
                os.close(descriptor)
            raise WorkspaceStorageError("workspace is already reserved") from exc
        except WorkspaceStorageError:
            if "descriptor" in locals():
                os.close(descriptor)
            raise
    def _validate_pinned(self, anchor: WorkspaceAnchor, pinned: _PinnedAnchor) -> None:
        workspace_metadata = os.fstat(pinned.workspace_fd)
        lock_metadata = os.fstat(pinned.lock_fd)
        self._validate_private(workspace_metadata)
        self._validate_lock(lock_metadata)
        if self._identity(workspace_metadata) != (anchor.device, anchor.inode):
            raise WorkspaceStorageError("workspace anchor identity changed")
        if self._identity(lock_metadata) != (anchor.lock_device, anchor.lock_inode):
            raise WorkspaceStorageError("workspace lock identity changed")

    def _verify_entry(
        self, name: str, device: int, inode: int, *, directory: bool
    ) -> None:
        flags = os.O_RDONLY | os.O_NOFOLLOW
        if directory:
            flags |= os.O_DIRECTORY
        try:
            descriptor = os.open(name, flags, dir_fd=self._root_fd)
            metadata = os.fstat(descriptor)
        except OSError as exc:
            raise WorkspaceStorageError("workspace entry is unavailable") from exc
        finally:
            if "descriptor" in locals():
                os.close(descriptor)
        if self._identity(metadata) != (device, inode):
            raise WorkspaceStorageError("workspace entry identity changed")

    def _quarantine(self, source: str, destination: str) -> None:
        self._require_absent(destination)
        try:
            os.rename(
                source,
                destination,
                src_dir_fd=self._root_fd,
                dst_dir_fd=self._root_fd,
            )
        except OSError as exc:
            raise WorkspaceStorageError("workspace quarantine failed") from exc

    def _require_absent(self, name: str) -> None:
        try:
            os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise WorkspaceStorageError("workspace entry cannot be inspected") from exc
        raise WorkspaceStorageError("workspace quarantine already exists")

    @contextmanager
    def _serialized(self):
        self._mutex.acquire()
        try:
            self._require_open()
            fcntl.flock(self._root_fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(self._root_fd, fcntl.LOCK_UN)
        finally:
            self._mutex.release()

    def _require_open(self) -> None:
        if self._root_fd < 0 or self._parent_fd < 0:
            raise WorkspaceStorageError("workspace storage is closed")
        metadata = os.fstat(self._root_fd)
        self._validate_private(metadata)
        if self._identity(metadata) != self._root_identity:
            raise WorkspaceStorageError("workspace root identity changed")

    def _close_descriptors(self) -> None:
        for attribute in ("_root_fd", "_parent_fd"):
            descriptor = getattr(self, attribute, -1)
            if descriptor >= 0:
                os.close(descriptor)
                setattr(self, attribute, -1)

    @staticmethod
    def _unlock_close(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    @staticmethod
    def _identity(metadata: os.stat_result) -> tuple[int, int]:
        return metadata.st_dev, metadata.st_ino

    @staticmethod
    def _validate_private(metadata: os.stat_result) -> None:
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise WorkspaceStorageError("workspace directory ownership is unsafe")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise WorkspaceStorageError("workspace directory permissions are unsafe")

    @staticmethod
    def _validate_lock(metadata: os.stat_result) -> None:
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise WorkspaceStorageError("workspace lock ownership is unsafe")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise WorkspaceStorageError("workspace lock permissions are unsafe")

    @staticmethod
    def _validate_task(task_id: str, attempt: int) -> None:
        if not isinstance(task_id, str) or not TASK_PATTERN.fullmatch(task_id):
            raise WorkspaceStorageError("workspace task id is invalid")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 99:
            raise WorkspaceStorageError("workspace attempt is invalid")

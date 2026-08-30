"""Descriptor-pinned capabilities for controller-owned task workspaces."""

from __future__ import annotations

import fcntl
import os
import re
import secrets
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

TASK_PATTERN = re.compile(r"[A-Z][A-Z0-9-]{2,63}")
GENERATION_PATTERN = re.compile(r"[0-9a-f]{32}")


class WorkspaceStorageError(RuntimeError):
    """Raised when a workspace capability cannot be preserved safely."""


@dataclass(frozen=True, slots=True)
class WorkspaceAnchor:
    task_id: str
    attempt: int
    name: str
    generation: str
    device: int
    inode: int


@dataclass(slots=True)
class _PinnedAnchor:
    descriptor: int
    borrowers: int = 0


class WorkspaceStorage:
    """Manage child capabilities beneath a controller-supplied pinned root."""

    def __init__(
        self,
        root_descriptor: int,
        *,
        generation_factory: Callable[[], str] | None = None,
    ) -> None:
        if isinstance(root_descriptor, bool) or not isinstance(root_descriptor, int):
            raise WorkspaceStorageError("workspace root descriptor is invalid")
        self._root_fd = -1
        self._mutex = threading.RLock()
        self._anchors: dict[WorkspaceAnchor, _PinnedAnchor] = {}
        self._generation_factory = generation_factory or (
            lambda: secrets.token_hex(16)
        )
        try:
            self._root_fd = os.dup(root_descriptor)
            metadata = os.fstat(self._root_fd)
            self._validate_private_directory(metadata)
            self._root_identity = self._identity(metadata)
        except Exception as exc:
            if self._root_fd >= 0:
                os.close(self._root_fd)
                self._root_fd = -1
            if isinstance(exc, WorkspaceStorageError):
                raise
            raise WorkspaceStorageError("workspace root capability is invalid") from exc

    def __enter__(self) -> WorkspaceStorage:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def reserve(self, task_id: str, *, attempt: int) -> WorkspaceAnchor:
        """Create and pin a generation-qualified direct child."""
        self._validate_task(task_id, attempt)
        with self._serialized():
            logical_name = f"{task_id.lower()}-attempt-{attempt}"
            if any(
                anchor.task_id == task_id and anchor.attempt == attempt
                for anchor in self._anchors
            ):
                raise WorkspaceStorageError("workspace is already active")
            generation = self._generation_factory()
            if not isinstance(generation, str) or not GENERATION_PATTERN.fullmatch(
                generation
            ):
                raise WorkspaceStorageError("workspace generation is invalid")
            name = f"{logical_name}-{generation}"
            descriptor = -1
            try:
                os.mkdir(name, mode=0o700, dir_fd=self._root_fd)
                descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=self._root_fd,
                )
                metadata = os.fstat(descriptor)
                self._validate_private_directory(metadata)
                if os.listdir(descriptor):
                    raise WorkspaceStorageError("reserved workspace is not empty")
            except Exception as exc:
                if descriptor >= 0:
                    os.close(descriptor)
                if isinstance(exc, WorkspaceStorageError):
                    raise
                raise WorkspaceStorageError("workspace reservation failed") from exc
            device, inode = self._identity(metadata)
            anchor = WorkspaceAnchor(
                task_id, attempt, name, generation, device, inode
            )
            self._anchors[anchor] = _PinnedAnchor(descriptor)
            return anchor

    @contextmanager
    def borrow(self, anchor: WorkspaceAnchor) -> Iterator[int]:
        """Yield a tracked duplicate; retirement waits for its context to end."""
        with self._mutex:
            self._require_root()
            pinned = self._require_anchor(anchor)
            self._validate_pinned(anchor, pinned)
            try:
                descriptor = os.dup(pinned.descriptor)
            except OSError as exc:
                raise WorkspaceStorageError("workspace borrow failed") from exc
            pinned.borrowers += 1
        try:
            yield descriptor
        finally:
            try:
                os.close(descriptor)
            except OSError as exc:
                raise WorkspaceStorageError("borrowed descriptor was changed") from exc
            finally:
                with self._mutex:
                    current = self._anchors.get(anchor)
                    if current is pinned:
                        pinned.borrowers -= 1

    def retire(self, anchor: WorkspaceAnchor) -> None:
        """End one capability lifetime without deleting or reopening its pathname."""
        with self._mutex:
            self._require_root()
            pinned = self._require_anchor(anchor)
            self._validate_pinned(anchor, pinned)
            if pinned.borrowers:
                raise WorkspaceStorageError("workspace capability is still borrowed")
            del self._anchors[anchor]
            os.close(pinned.descriptor)

    def close(self) -> None:
        """Close only after every pinned child has been explicitly retired."""
        with self._mutex:
            if self._anchors:
                raise WorkspaceStorageError("active workspace anchors prevent close")
            if self._root_fd >= 0:
                os.close(self._root_fd)
                self._root_fd = -1

    @contextmanager
    def _serialized(self) -> Iterator[None]:
        self._mutex.acquire()
        locked = False
        try:
            self._require_root()
            fcntl.flock(self._root_fd, fcntl.LOCK_EX)
            locked = True
            yield
        finally:
            try:
                if locked:
                    fcntl.flock(self._root_fd, fcntl.LOCK_UN)
            finally:
                self._mutex.release()

    def _require_root(self) -> None:
        if self._root_fd < 0:
            raise WorkspaceStorageError("workspace storage is closed")
        try:
            metadata = os.fstat(self._root_fd)
            self._validate_private_directory(metadata)
        except OSError as exc:
            raise WorkspaceStorageError("workspace root capability failed") from exc
        if self._identity(metadata) != self._root_identity:
            raise WorkspaceStorageError("workspace root identity changed")

    def _require_anchor(self, anchor: WorkspaceAnchor) -> _PinnedAnchor:
        pinned = self._anchors.get(anchor)
        if pinned is None:
            raise WorkspaceStorageError("workspace anchor is unavailable")
        return pinned

    def _validate_pinned(
        self, anchor: WorkspaceAnchor, pinned: _PinnedAnchor
    ) -> None:
        try:
            metadata = os.fstat(pinned.descriptor)
            self._validate_private_directory(metadata)
        except OSError as exc:
            raise WorkspaceStorageError("workspace anchor capability failed") from exc
        if self._identity(metadata) != (anchor.device, anchor.inode):
            raise WorkspaceStorageError("workspace anchor identity changed")

    @staticmethod
    def _identity(metadata: os.stat_result) -> tuple[int, int]:
        return metadata.st_dev, metadata.st_ino

    @staticmethod
    def _validate_private_directory(metadata: os.stat_result) -> None:
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise WorkspaceStorageError("workspace directory ownership is unsafe")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise WorkspaceStorageError("workspace directory permissions are unsafe")

    @staticmethod
    def _validate_task(task_id: str, attempt: int) -> None:
        if not isinstance(task_id, str) or not TASK_PATTERN.fullmatch(task_id):
            raise WorkspaceStorageError("workspace task id is invalid")
        if (
            isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or not 1 <= attempt <= 99
        ):
            raise WorkspaceStorageError("workspace attempt is invalid")

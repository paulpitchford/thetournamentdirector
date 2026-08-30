"""One-to-one ownership of trusted workspace directory descriptors."""

from __future__ import annotations

import os
import re
import stat
import threading
from dataclasses import dataclass

TASK_PATTERN = re.compile(r"[A-Z][A-Z0-9-]{2,63}")
GENERATION_PATTERN = re.compile(r"[0-9a-f]{32}")


class WorkspaceOwnershipError(RuntimeError):
    """Raised when workspace descriptor ownership fails closed."""


@dataclass(frozen=True, slots=True)
class OwnedWorkspace:
    task_id: str
    attempt: int
    generation: str
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class WorkspaceState:
    device: int
    inode: int
    entry_count: int


class WorkspaceOwnership:
    """Own each physical workspace inode for at most one logical task attempt."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._descriptors: dict[OwnedWorkspace, int] = {}
        self._physical: dict[tuple[int, int], OwnedWorkspace] = {}
        self._closed = False
        self._cleanup_failed = False

    def register(
        self,
        task_id: str,
        *,
        attempt: int,
        generation: str,
        descriptor: int,
    ) -> OwnedWorkspace:
        self._validate_inputs(task_id, attempt, generation, descriptor)
        with self._lock:
            self._require_open()
            if any(
                item.task_id == task_id and item.attempt == attempt
                for item in self._descriptors
            ):
                raise WorkspaceOwnershipError("logical workspace is already owned")
            duplicate = -1
            try:
                duplicate = os.dup(descriptor)
                metadata = os.fstat(duplicate)
                self._validate_directory(metadata)
                identity = (metadata.st_dev, metadata.st_ino)
                if identity in self._physical:
                    raise WorkspaceOwnershipError("physical workspace is already owned")
            except Exception as exc:
                if duplicate >= 0:
                    os.close(duplicate)
                if isinstance(exc, WorkspaceOwnershipError):
                    raise
                raise WorkspaceOwnershipError("workspace registration failed") from exc
            owned = OwnedWorkspace(
                task_id, attempt, generation, identity[0], identity[1]
            )
            self._descriptors[owned] = duplicate
            self._physical[identity] = owned
            return owned

    def inspect(self, owned: OwnedWorkspace) -> WorkspaceState:
        with self._lock:
            self._require_open()
            descriptor = self._require_owned(owned)
            metadata = self._validate_owned(owned, descriptor)
            try:
                count = len(os.listdir(descriptor))
            except OSError as exc:
                raise WorkspaceOwnershipError("workspace inspection failed") from exc
            return WorkspaceState(metadata.st_dev, metadata.st_ino, count)

    def retire(self, owned: OwnedWorkspace) -> None:
        with self._lock:
            self._require_open()
            descriptor = self._require_owned(owned)
            self._validate_owned(owned, descriptor)
            close_error: OSError | None = None
            try:
                os.close(descriptor)
            except OSError as exc:
                close_error = exc
            finally:
                del self._descriptors[owned]
                del self._physical[(owned.device, owned.inode)]
            if close_error is not None:
                raise WorkspaceOwnershipError("workspace retirement failed") from close_error

    def close(self) -> None:
        """Synchronously close all owned descriptors and replay cleanup failure."""
        with self._lock:
            if self._closed:
                if self._cleanup_failed:
                    raise WorkspaceOwnershipError("workspace cleanup failed")
                return
            self._closed = True
            failed = False
            for descriptor in self._descriptors.values():
                try:
                    os.close(descriptor)
                except OSError:
                    failed = True
            self._descriptors.clear()
            self._physical.clear()
            self._cleanup_failed = failed
            if failed:
                raise WorkspaceOwnershipError("workspace cleanup failed")

    def _require_open(self) -> None:
        if self._closed:
            raise WorkspaceOwnershipError("workspace ownership is closed")

    def _require_owned(self, owned: OwnedWorkspace) -> int:
        descriptor = self._descriptors.get(owned)
        if descriptor is None:
            raise WorkspaceOwnershipError("workspace is not owned")
        return descriptor

    def _validate_owned(
        self, owned: OwnedWorkspace, descriptor: int
    ) -> os.stat_result:
        try:
            metadata = os.fstat(descriptor)
            self._validate_directory(metadata)
        except OSError as exc:
            raise WorkspaceOwnershipError("workspace validation failed") from exc
        if (metadata.st_dev, metadata.st_ino) != (owned.device, owned.inode):
            raise WorkspaceOwnershipError("workspace identity changed")
        return metadata

    @staticmethod
    def _validate_directory(metadata: os.stat_result) -> None:
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise WorkspaceOwnershipError("workspace ownership is unsafe")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise WorkspaceOwnershipError("workspace permissions are unsafe")

    @staticmethod
    def _validate_inputs(
        task_id: str, attempt: int, generation: str, descriptor: int
    ) -> None:
        if not isinstance(task_id, str) or not TASK_PATTERN.fullmatch(task_id):
            raise WorkspaceOwnershipError("workspace task id is invalid")
        if (
            isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or not 1 <= attempt <= 99
        ):
            raise WorkspaceOwnershipError("workspace attempt is invalid")
        if not isinstance(generation, str) or not GENERATION_PATTERN.fullmatch(
            generation
        ):
            raise WorkspaceOwnershipError("workspace generation is invalid")
        if isinstance(descriptor, bool) or not isinstance(descriptor, int):
            raise WorkspaceOwnershipError("workspace descriptor is invalid")

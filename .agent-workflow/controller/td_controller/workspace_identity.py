"""Bounded identity ownership for trusted workspace directory descriptors."""

from __future__ import annotations

import os
import re
import stat
import threading
from dataclasses import dataclass

TASK_PATTERN = re.compile(r"[A-Z][A-Z0-9-]{2,63}")
GENERATION_PATTERN = re.compile(r"[0-9a-f]{32}")


class WorkspaceIdentityError(RuntimeError):
    """Raised when workspace identity ownership fails closed."""


@dataclass(frozen=True, slots=True)
class WorkspaceIdentity:
    task_id: str
    attempt: int
    generation: str
    device: int
    inode: int


class WorkspaceIdentityRegistry:
    """Own each supplied physical directory identity exactly once."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._descriptors: dict[WorkspaceIdentity, int] = {}
        self._physical: dict[tuple[int, int], WorkspaceIdentity] = {}
        self._closed = False
        self._cleanup_failed = False

    def register(
        self,
        task_id: str,
        *,
        attempt: int,
        generation: str,
        descriptor: int,
    ) -> WorkspaceIdentity:
        self._validate_inputs(task_id, attempt, generation, descriptor)
        with self._lock:
            self._require_open()
            if any(
                item.task_id == task_id and item.attempt == attempt
                for item in self._descriptors
            ):
                raise WorkspaceIdentityError("logical workspace is already owned")
            duplicate = -1
            try:
                duplicate = os.dup(descriptor)
                metadata = os.fstat(duplicate)
                self._validate_directory(metadata)
                physical = (metadata.st_dev, metadata.st_ino)
                if physical in self._physical:
                    raise WorkspaceIdentityError("physical workspace is already owned")
            except Exception as exc:
                if duplicate >= 0:
                    os.close(duplicate)
                if isinstance(exc, WorkspaceIdentityError):
                    raise
                raise WorkspaceIdentityError("workspace registration failed") from exc
            identity = WorkspaceIdentity(
                task_id, attempt, generation, physical[0], physical[1]
            )
            self._descriptors[identity] = duplicate
            self._physical[physical] = identity
            return identity

    def verify(self, identity: WorkspaceIdentity) -> None:
        """Verify bounded inode metadata without reading directory entries."""
        with self._lock:
            self._require_open()
            descriptor = self._require_identity(identity)
            self._validate_owned(identity, descriptor)

    def retire(self, identity: WorkspaceIdentity) -> None:
        """Make a close attempt terminal for this identity."""
        with self._lock:
            self._require_open()
            descriptor = self._require_identity(identity)
            self._validate_owned(identity, descriptor)
            close_error: OSError | None = None
            try:
                os.close(descriptor)
            except OSError as exc:
                close_error = exc
            finally:
                del self._descriptors[identity]
                del self._physical[(identity.device, identity.inode)]
            if close_error is not None:
                raise WorkspaceIdentityError("workspace retirement failed") from close_error

    def close(self) -> None:
        """Synchronously close all identities and replay cleanup failure."""
        with self._lock:
            if self._closed:
                if self._cleanup_failed:
                    raise WorkspaceIdentityError("workspace cleanup failed")
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
                raise WorkspaceIdentityError("workspace cleanup failed")

    def _require_open(self) -> None:
        if self._closed:
            raise WorkspaceIdentityError("workspace identity registry is closed")

    def _require_identity(self, identity: WorkspaceIdentity) -> int:
        descriptor = self._descriptors.get(identity)
        if descriptor is None:
            raise WorkspaceIdentityError("workspace identity is unavailable")
        return descriptor

    def _validate_owned(
        self, identity: WorkspaceIdentity, descriptor: int
    ) -> None:
        try:
            metadata = os.fstat(descriptor)
            self._validate_directory(metadata)
        except OSError as exc:
            raise WorkspaceIdentityError("workspace validation failed") from exc
        if (metadata.st_dev, metadata.st_ino) != (identity.device, identity.inode):
            raise WorkspaceIdentityError("workspace identity changed")

    @staticmethod
    def _validate_directory(metadata: os.stat_result) -> None:
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise WorkspaceIdentityError("workspace ownership is unsafe")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise WorkspaceIdentityError("workspace permissions are unsafe")

    @staticmethod
    def _validate_inputs(
        task_id: str, attempt: int, generation: str, descriptor: int
    ) -> None:
        if not isinstance(task_id, str) or not TASK_PATTERN.fullmatch(task_id):
            raise WorkspaceIdentityError("workspace task id is invalid")
        if (
            isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or not 1 <= attempt <= 99
        ):
            raise WorkspaceIdentityError("workspace attempt is invalid")
        if not isinstance(generation, str) or not GENERATION_PATTERN.fullmatch(
            generation
        ):
            raise WorkspaceIdentityError("workspace generation is invalid")
        if isinstance(descriptor, bool) or not isinstance(descriptor, int):
            raise WorkspaceIdentityError("workspace descriptor is invalid")

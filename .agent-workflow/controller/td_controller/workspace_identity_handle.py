"""Single-owner handle for one trusted workspace directory descriptor."""

from __future__ import annotations

import os
import re
import stat
import threading
from dataclasses import dataclass

TASK_PATTERN = re.compile(r"[A-Z][A-Z0-9-]{2,63}")
GENERATION_PATTERN = re.compile(r"[0-9a-f]{32}")


class WorkspaceIdentityHandleError(RuntimeError):
    """Raised when one workspace identity handle fails closed."""


@dataclass(frozen=True, slots=True)
class WorkspaceIdentity:
    task_id: str
    attempt: int
    generation: str
    device: int
    inode: int


class WorkspaceIdentityHandle:
    """Own one internal duplicate without pathname or content access."""

    def __init__(
        self,
        task_id: str,
        *,
        attempt: int,
        generation: str,
        descriptor: int,
    ) -> None:
        self._validate_inputs(task_id, attempt, generation, descriptor)
        self._lock = threading.RLock()
        self._descriptor = -1
        self._closed = False
        self._cleanup_failed = False
        try:
            self._descriptor = os.dup(descriptor)
            metadata = os.fstat(self._descriptor)
            self._validate_directory(metadata)
        except Exception as exc:
            if self._descriptor >= 0:
                try:
                    os.close(self._descriptor)
                except OSError as cleanup_error:
                    raise WorkspaceIdentityHandleError(
                        "workspace handle cleanup failed"
                    ) from cleanup_error
                finally:
                    self._descriptor = -1
            if isinstance(exc, WorkspaceIdentityHandleError):
                raise
            raise WorkspaceIdentityHandleError(
                "workspace handle initialization failed"
            ) from exc
        self._identity = WorkspaceIdentity(
            task_id,
            attempt,
            generation,
            metadata.st_dev,
            metadata.st_ino,
        )

    @property
    def identity(self) -> WorkspaceIdentity:
        return self._identity

    def verify(self) -> None:
        """Verify bounded inode metadata without reading a path or directory."""
        with self._lock:
            self._require_open()
            try:
                metadata = os.fstat(self._descriptor)
                self._validate_directory(metadata)
            except OSError as exc:
                raise WorkspaceIdentityHandleError(
                    "workspace handle verification failed"
                ) from exc
            if (metadata.st_dev, metadata.st_ino) != (
                self._identity.device,
                self._identity.inode,
            ):
                raise WorkspaceIdentityHandleError(
                    "workspace handle identity changed"
                )

    def close(self) -> None:
        """Make one synchronized close attempt and replay its result."""
        with self._lock:
            if self._closed:
                if self._cleanup_failed:
                    raise WorkspaceIdentityHandleError(
                        "workspace handle cleanup failed"
                    )
                return
            self._closed = True
            try:
                os.close(self._descriptor)
            except OSError as exc:
                self._cleanup_failed = True
                raise WorkspaceIdentityHandleError(
                    "workspace handle cleanup failed"
                ) from exc
            finally:
                self._descriptor = -1

    def _require_open(self) -> None:
        if self._closed:
            raise WorkspaceIdentityHandleError("workspace handle is closed")

    @staticmethod
    def _validate_directory(metadata: os.stat_result) -> None:
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise WorkspaceIdentityHandleError("workspace handle ownership is unsafe")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise WorkspaceIdentityHandleError("workspace handle permissions are unsafe")

    @staticmethod
    def _validate_inputs(
        task_id: str, attempt: int, generation: str, descriptor: int
    ) -> None:
        if not isinstance(task_id, str) or not TASK_PATTERN.fullmatch(task_id):
            raise WorkspaceIdentityHandleError("workspace task id is invalid")
        if (
            isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or not 1 <= attempt <= 99
        ):
            raise WorkspaceIdentityHandleError("workspace attempt is invalid")
        if not isinstance(generation, str) or not GENERATION_PATTERN.fullmatch(
            generation
        ):
            raise WorkspaceIdentityHandleError("workspace generation is invalid")
        if isinstance(descriptor, bool) or not isinstance(descriptor, int):
            raise WorkspaceIdentityHandleError("workspace descriptor is invalid")

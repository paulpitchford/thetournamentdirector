"""Internal ownership of pre-provisioned task workspace descriptors."""

from __future__ import annotations

import os
import re
import stat
import threading
from dataclasses import dataclass

TASK_PATTERN = re.compile(r"[A-Z][A-Z0-9-]{2,63}")
GENERATION_PATTERN = re.compile(r"[0-9a-f]{32}")


class WorkspaceStorageError(RuntimeError):
    """Raised when an internal workspace pin fails closed."""


@dataclass(frozen=True, slots=True)
class WorkspaceAnchor:
    task_id: str
    attempt: int
    name: str
    generation: str
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    device: int
    inode: int
    entry_count: int


class WorkspaceStorage:
    """Pin trusted pre-provisioned child descriptors without pathname access."""

    def __init__(self) -> None:
        self._mutex = threading.RLock()
        self._pins: dict[WorkspaceAnchor, int] = {}
        self._closed = False

    def __enter__(self) -> WorkspaceStorage:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def register(
        self,
        task_id: str,
        *,
        attempt: int,
        generation: str,
        descriptor: int,
    ) -> WorkspaceAnchor:
        """Duplicate and pin one descriptor supplied by a trusted provisioner."""
        self._validate_identity(task_id, attempt, generation, descriptor)
        with self._mutex:
            self._require_open()
            if any(
                anchor.task_id == task_id and anchor.attempt == attempt
                for anchor in self._pins
            ):
                raise WorkspaceStorageError("workspace is already active")
            pinned = -1
            try:
                pinned = os.dup(descriptor)
                metadata = os.fstat(pinned)
                self._validate_private_directory(metadata)
                if os.listdir(pinned):
                    raise WorkspaceStorageError("registered workspace is not empty")
            except Exception as exc:
                if pinned >= 0:
                    os.close(pinned)
                if isinstance(exc, WorkspaceStorageError):
                    raise
                raise WorkspaceStorageError("workspace registration failed") from exc
            name = f"{task_id.lower()}-attempt-{attempt}-{generation}"
            device, inode = self._identity(metadata)
            anchor = WorkspaceAnchor(
                task_id, attempt, name, generation, device, inode
            )
            self._pins[anchor] = pinned
            return anchor

    def inspect(self, anchor: WorkspaceAnchor) -> WorkspaceSnapshot:
        """Inspect the pinned inode without resolving a filesystem pathname."""
        with self._mutex:
            self._require_open()
            descriptor = self._require_pin(anchor)
            metadata = self._validate_pin(anchor, descriptor)
            try:
                entry_count = len(os.listdir(descriptor))
            except OSError as exc:
                raise WorkspaceStorageError("workspace inspection failed") from exc
            return WorkspaceSnapshot(
                metadata.st_dev, metadata.st_ino, entry_count
            )

    def retire(self, anchor: WorkspaceAnchor) -> None:
        """End internal ownership without mutating the retained workspace."""
        with self._mutex:
            self._require_open()
            descriptor = self._require_pin(anchor)
            self._validate_pin(anchor, descriptor)
            os.close(descriptor)
            del self._pins[anchor]

    def close(self) -> None:
        """Close only after all pinned workspaces are retired."""
        with self._mutex:
            if self._pins:
                raise WorkspaceStorageError("active workspace anchors prevent close")
            self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise WorkspaceStorageError("workspace storage is closed")

    def _require_pin(self, anchor: WorkspaceAnchor) -> int:
        descriptor = self._pins.get(anchor)
        if descriptor is None:
            raise WorkspaceStorageError("workspace anchor is unavailable")
        return descriptor

    def _validate_pin(
        self, anchor: WorkspaceAnchor, descriptor: int
    ) -> os.stat_result:
        try:
            metadata = os.fstat(descriptor)
            self._validate_private_directory(metadata)
        except OSError as exc:
            raise WorkspaceStorageError("workspace pin failed") from exc
        if self._identity(metadata) != (anchor.device, anchor.inode):
            raise WorkspaceStorageError("workspace anchor identity changed")
        return metadata

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
    def _validate_identity(
        task_id: str, attempt: int, generation: str, descriptor: int
    ) -> None:
        if not isinstance(task_id, str) or not TASK_PATTERN.fullmatch(task_id):
            raise WorkspaceStorageError("workspace task id is invalid")
        if (
            isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or not 1 <= attempt <= 99
        ):
            raise WorkspaceStorageError("workspace attempt is invalid")
        if not isinstance(generation, str) or not GENERATION_PATTERN.fullmatch(
            generation
        ):
            raise WorkspaceStorageError("workspace generation is invalid")
        if isinstance(descriptor, bool) or not isinstance(descriptor, int):
            raise WorkspaceStorageError("workspace descriptor is invalid")

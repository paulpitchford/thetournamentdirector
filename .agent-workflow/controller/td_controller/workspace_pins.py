"""Internal ownership registry for pre-provisioned workspace descriptors."""

from __future__ import annotations

import os
import re
import stat
import threading
from dataclasses import dataclass

TASK_PATTERN = re.compile(r"[A-Z][A-Z0-9-]{2,63}")
GENERATION_PATTERN = re.compile(r"[0-9a-f]{32}")


class WorkspacePinError(RuntimeError):
    """Raised when workspace descriptor ownership fails closed."""


@dataclass(frozen=True, slots=True)
class PinnedWorkspace:
    task_id: str
    attempt: int
    generation: str
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class WorkspacePinSnapshot:
    device: int
    inode: int
    entry_count: int


class WorkspacePinRegistry:
    """Own duplicate descriptors without creating paths or exporting pins."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._descriptors: dict[PinnedWorkspace, int] = {}
        self._closed = False
        self._close_failed = False

    def register(
        self,
        task_id: str,
        *,
        attempt: int,
        generation: str,
        descriptor: int,
    ) -> PinnedWorkspace:
        self._validate_inputs(task_id, attempt, generation, descriptor)
        with self._lock:
            self._require_open()
            if any(
                pin.task_id == task_id and pin.attempt == attempt
                for pin in self._descriptors
            ):
                raise WorkspacePinError("workspace pin is already active")
            duplicate = -1
            try:
                duplicate = os.dup(descriptor)
                metadata = os.fstat(duplicate)
                self._validate_directory(metadata)
                if os.listdir(duplicate):
                    raise WorkspacePinError("workspace pin must start empty")
            except Exception as exc:
                if duplicate >= 0:
                    os.close(duplicate)
                if isinstance(exc, WorkspacePinError):
                    raise
                raise WorkspacePinError("workspace pin registration failed") from exc
            device, inode = metadata.st_dev, metadata.st_ino
            pin = PinnedWorkspace(
                task_id, attempt, generation, device, inode
            )
            self._descriptors[pin] = duplicate
            return pin

    def inspect(self, pin: PinnedWorkspace) -> WorkspacePinSnapshot:
        with self._lock:
            self._require_open()
            descriptor = self._require_pin(pin)
            metadata = self._validate_pin(pin, descriptor)
            try:
                count = len(os.listdir(descriptor))
            except OSError as exc:
                raise WorkspacePinError("workspace pin inspection failed") from exc
            return WorkspacePinSnapshot(metadata.st_dev, metadata.st_ino, count)

    def retire(self, pin: PinnedWorkspace) -> None:
        with self._lock:
            self._require_open()
            descriptor = self._require_pin(pin)
            self._validate_pin(pin, descriptor)
            try:
                os.close(descriptor)
            except OSError as exc:
                raise WorkspacePinError("workspace pin retirement failed") from exc
            del self._descriptors[pin]

    def close(self) -> None:
        """Close every owned descriptor; safe to call repeatedly."""
        with self._lock:
            if self._closed:
                if self._close_failed:
                    raise WorkspacePinError("workspace pin cleanup failed")
                return
            self._closed = True
            failed = False
            for descriptor in self._descriptors.values():
                try:
                    os.close(descriptor)
                except OSError:
                    failed = True
            self._descriptors.clear()
            self._close_failed = failed
            if failed:
                raise WorkspacePinError("workspace pin cleanup failed")

    def _require_open(self) -> None:
        if self._closed:
            raise WorkspacePinError("workspace pin registry is closed")

    def _require_pin(self, pin: PinnedWorkspace) -> int:
        descriptor = self._descriptors.get(pin)
        if descriptor is None:
            raise WorkspacePinError("workspace pin is unavailable")
        return descriptor

    def _validate_pin(
        self, pin: PinnedWorkspace, descriptor: int
    ) -> os.stat_result:
        try:
            metadata = os.fstat(descriptor)
            self._validate_directory(metadata)
        except OSError as exc:
            raise WorkspacePinError("workspace pin validation failed") from exc
        if (metadata.st_dev, metadata.st_ino) != (pin.device, pin.inode):
            raise WorkspacePinError("workspace pin identity changed")
        return metadata

    @staticmethod
    def _validate_directory(metadata: os.stat_result) -> None:
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise WorkspacePinError("workspace pin ownership is unsafe")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise WorkspacePinError("workspace pin permissions are unsafe")

    @staticmethod
    def _validate_inputs(
        task_id: str, attempt: int, generation: str, descriptor: int
    ) -> None:
        if not isinstance(task_id, str) or not TASK_PATTERN.fullmatch(task_id):
            raise WorkspacePinError("workspace task id is invalid")
        if (
            isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or not 1 <= attempt <= 99
        ):
            raise WorkspacePinError("workspace attempt is invalid")
        if not isinstance(generation, str) or not GENERATION_PATTERN.fullmatch(
            generation
        ):
            raise WorkspacePinError("workspace generation is invalid")
        if isinstance(descriptor, bool) or not isinstance(descriptor, int):
            raise WorkspacePinError("workspace descriptor is invalid")

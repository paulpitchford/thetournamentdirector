"""Bounded command execution rooted at a pinned directory descriptor."""

from __future__ import annotations

import os
import re
import stat
import threading
from collections.abc import Mapping
from pathlib import Path

from .effective_identity import (
    EffectiveIdentity,
    EffectiveIdentityError,
    validate_effective_identity,
)
from .review_contract import CodexReviewError
from .review_runtime import MAX_INPUT_BYTES, ProcessOutput, SubprocessExecutor

ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


class PinnedDirectoryExecutorError(RuntimeError):
    """Raised when pinned directory command authority is unavailable."""


class PinnedDirectoryExecutor:
    """Own one descriptor and use only its procfd alias as subprocess cwd."""

    def __init__(self, *, descriptor: int) -> None:
        if isinstance(descriptor, bool) or not isinstance(descriptor, int):
            raise PinnedDirectoryExecutorError("directory descriptor is invalid")
        self._lock = threading.RLock()
        self._descriptor = -1
        self._closed = False
        self._cleanup_failed = False
        process_identity = self._current_process_identity()
        try:
            duplicate = os.dup(descriptor)
            metadata = os.fstat(duplicate)
            self._validate_directory(metadata, process_identity.uid)
        except (OSError, PinnedDirectoryExecutorError):
            if "duplicate" in locals():
                try:
                    os.close(duplicate)
                except OSError:
                    raise PinnedDirectoryExecutorError(
                        "directory initialization cleanup failed"
                    ) from None
            raise PinnedDirectoryExecutorError(
                "directory executor initialization failed"
            ) from None
        self._descriptor = duplicate
        self._directory_identity = (metadata.st_dev, metadata.st_ino)
        self._process_identity = process_identity

    def run(
        self,
        command: list[str],
        *,
        environment: Mapping[str, str],
        input_bytes: bytes = b"",
        timeout_seconds: int = 30,
    ) -> ProcessOutput:
        """Run while the descriptor remains live; export no path or descriptor."""
        clean_environment = self._validate_environment(environment)
        if (
            not isinstance(command, list)
            or not 1 <= len(command) <= 64
            or not isinstance(command[0], str)
            or not Path(command[0]).is_absolute()
            or any(
                not isinstance(argument, str)
                or not argument.isascii()
                or "\x00" in argument
                or len(argument) > 4096
                for argument in command
            )
            or not isinstance(input_bytes, bytes)
            or len(input_bytes) > MAX_INPUT_BYTES
            or isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 1 <= timeout_seconds <= 600
        ):
            raise PinnedDirectoryExecutorError("directory command is invalid")
        with self._lock:
            self._verify_locked()
            cwd = Path(f"/proc/self/fd/{self._descriptor}")
            executor = SubprocessExecutor(lambda _: clean_environment)
            try:
                output = executor.run(
                    command, input_bytes=input_bytes, cwd=cwd,
                    timeout_seconds=timeout_seconds,
                )
            except (CodexReviewError, OSError):
                raise PinnedDirectoryExecutorError(
                    "directory command process failed"
                ) from None
            self._verify_locked()
            return output

    def verify(self) -> None:
        """Verify the pinned descriptor without pathname lookup."""
        with self._lock:
            self._verify_locked()

    def close(self) -> None:
        """Synchronously and idempotently release command authority."""
        with self._lock:
            if self._closed:
                if self._cleanup_failed:
                    raise PinnedDirectoryExecutorError(
                        "directory executor cleanup failed"
                    )
                return
            self._closed = True
            try:
                os.close(self._descriptor)
            except OSError:
                self._cleanup_failed = True
                raise PinnedDirectoryExecutorError(
                    "directory executor cleanup failed"
                ) from None
            finally:
                self._descriptor = -1

    def _verify_locked(self) -> None:
        if self._closed:
            raise PinnedDirectoryExecutorError("directory executor is closed")
        process_identity = self._current_process_identity()
        if process_identity != self._process_identity:
            raise PinnedDirectoryExecutorError("process identity changed")
        try:
            metadata = os.fstat(self._descriptor)
            self._validate_directory(metadata, process_identity.uid)
        except (OSError, PinnedDirectoryExecutorError):
            raise PinnedDirectoryExecutorError(
                "directory executor verification failed"
            ) from None
        if (metadata.st_dev, metadata.st_ino) != self._directory_identity:
            raise PinnedDirectoryExecutorError("directory identity changed")

    @staticmethod
    def _current_process_identity() -> EffectiveIdentity:
        identity_failed = False
        try:
            identity = validate_effective_identity()
        except EffectiveIdentityError:
            identity_failed = True
            identity = None
        if identity_failed or identity is None:
            raise PinnedDirectoryExecutorError(
                "process identity verification failed"
            ) from None
        return identity

    @staticmethod
    def _validate_directory(metadata: os.stat_result, expected_uid: int) -> None:
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise PinnedDirectoryExecutorError("directory ownership is unsafe")

    @staticmethod
    def _validate_environment(environment: Mapping[str, str]) -> dict[str, str]:
        if not isinstance(environment, Mapping) or len(environment) > 32:
            raise PinnedDirectoryExecutorError("directory environment is invalid")
        clean: dict[str, str] = {}
        for key, value in environment.items():
            if (
                not isinstance(key, str)
                or not ENV_KEY.fullmatch(key)
                or not isinstance(value, str)
                or "\x00" in value
            ):
                raise PinnedDirectoryExecutorError(
                    "directory environment is invalid"
                )
            try:
                encoded = value.encode("utf-8", errors="strict")
            except UnicodeError:
                raise PinnedDirectoryExecutorError(
                    "directory environment is invalid"
                ) from None
            if len(encoded) > 4096:
                raise PinnedDirectoryExecutorError(
                    "directory environment is invalid"
                )
            clean[key] = value
        return clean

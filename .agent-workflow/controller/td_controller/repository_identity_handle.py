"""Pinned controller authority for one canonical repository root."""

from __future__ import annotations

import os
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


class RepositoryIdentityHandleError(RuntimeError):
    """Raised when trusted repository identity is unavailable or changed."""


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    """Stable physical identity of the controller repository root."""

    device: int
    inode: int


class RepositoryIdentityHandle:
    """Own one root descriptor and bind it to one canonical host pathname."""

    def __init__(self, repository_root: Path, *, descriptor: int) -> None:
        if not isinstance(repository_root, Path) or not repository_root.is_absolute():
            raise RepositoryIdentityHandleError("repository root is invalid")
        if isinstance(descriptor, bool) or not isinstance(descriptor, int):
            raise RepositoryIdentityHandleError("repository descriptor is invalid")
        self._lock = threading.RLock()
        self._descriptor = -1
        self._closed = False
        self._cleanup_failed = False
        self._hold_owner: int | None = None
        try:
            root = repository_root.resolve(strict=True)
            if root != repository_root or repository_root.is_symlink():
                raise RepositoryIdentityHandleError("repository root is not canonical")
            duplicate = os.dup(descriptor)
            descriptor_metadata = os.fstat(duplicate)
            path_metadata = root.stat(follow_symlinks=False)
            self._validate_directory(descriptor_metadata)
            self._validate_directory(path_metadata)
            if (descriptor_metadata.st_dev, descriptor_metadata.st_ino) != (
                path_metadata.st_dev, path_metadata.st_ino,
            ):
                raise RepositoryIdentityHandleError(
                    "repository descriptor does not match its root"
                )
        except RepositoryIdentityHandleError:
            if "duplicate" in locals():
                try:
                    os.close(duplicate)
                except OSError:
                    raise RepositoryIdentityHandleError(
                        "repository initialization cleanup failed"
                    ) from None
            raise
        except OSError:
            if "duplicate" in locals():
                try:
                    os.close(duplicate)
                except OSError:
                    raise RepositoryIdentityHandleError(
                        "repository initialization cleanup failed"
                    ) from None
            raise RepositoryIdentityHandleError(
                "repository handle initialization failed"
            ) from None
        self._root = root
        self._descriptor = duplicate
        self._identity = RepositoryIdentity(
            descriptor_metadata.st_dev, descriptor_metadata.st_ino
        )

    @property
    def identity(self) -> RepositoryIdentity:
        return self._identity

    def verify(self) -> None:
        """Verify both the pinned descriptor and canonical path identity."""
        with self._lock:
            self._verify_locked()

    @contextmanager
    def hold_path(self) -> Iterator[Path]:
        """Keep repository authority live throughout one synchronous operation."""
        self._lock.acquire()
        try:
            if self._hold_owner is not None:
                raise RepositoryIdentityHandleError(
                    "repository identity is already held"
                )
            self._verify_locked()
            self._hold_owner = threading.get_ident()
            try:
                yield self._root
            finally:
                try:
                    self._verify_locked()
                finally:
                    self._hold_owner = None
        finally:
            self._lock.release()

    def close(self) -> None:
        """Synchronously and idempotently release repository authority."""
        with self._lock:
            if self._hold_owner == threading.get_ident():
                raise RepositoryIdentityHandleError(
                    "repository identity hold is active"
                )
            if self._closed:
                if self._cleanup_failed:
                    raise RepositoryIdentityHandleError(
                        "repository handle cleanup failed"
                    )
                return
            self._closed = True
            try:
                os.close(self._descriptor)
            except OSError:
                self._cleanup_failed = True
                raise RepositoryIdentityHandleError(
                    "repository handle cleanup failed"
                ) from None
            finally:
                self._descriptor = -1

    def _verify_locked(self) -> None:
        if self._closed:
            raise RepositoryIdentityHandleError("repository handle is closed")
        try:
            descriptor_metadata = os.fstat(self._descriptor)
            path_metadata = self._root.stat(follow_symlinks=False)
            self._validate_directory(descriptor_metadata)
            self._validate_directory(path_metadata)
        except (OSError, RepositoryIdentityHandleError):
            raise RepositoryIdentityHandleError(
                "repository identity verification failed"
            ) from None
        expected = (self._identity.device, self._identity.inode)
        if (
            (descriptor_metadata.st_dev, descriptor_metadata.st_ino) != expected
            or (path_metadata.st_dev, path_metadata.st_ino) != expected
            or self._root.is_symlink()
        ):
            raise RepositoryIdentityHandleError("repository identity changed")

    @staticmethod
    def _validate_directory(metadata: os.stat_result) -> None:
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise RepositoryIdentityHandleError("repository ownership is unsafe")

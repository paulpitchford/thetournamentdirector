"""Bounded command execution rooted at a pinned directory descriptor."""

from __future__ import annotations

import os
import re
import stat
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .effective_identity import (
    EffectiveIdentity,
    EffectiveIdentityError,
    validate_effective_identity,
)
from .review_contract import CodexReviewError
from .review_runtime import MAX_INPUT_BYTES, ProcessOutput, SubprocessExecutor
from .workspace_identity_handle import WorkspaceIdentity
from .worktree_marker_contract import (
    WorktreeMarkerContractError,
    parse_worktree_marker,
)

ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
ENV_KEYS = frozenset({
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_NO_REPLACE_OBJECTS",
    "HOME", "LANG", "LC_ALL", "PATH",
})


class PinnedDirectoryExecutorError(RuntimeError):
    """Raised when pinned directory command authority is unavailable."""


@dataclass(frozen=True, slots=True)
class PinnedDirectoryIdentity:
    """Non-authoritative identity evidence for capability composition."""

    device: int
    inode: int
    uid: int


@dataclass(frozen=True, slots=True)
class WorktreeAdminBinding:
    """Confirmed marker/admin identity without pathname authority."""

    admin_name: str
    admin_device: int
    admin_inode: int
    marker_device: int
    marker_inode: int


class PinnedDirectoryExecutor:
    """Run trusted calls from procfd; callers enforce a reviewed allowlist."""

    def __init__(self, *, descriptor: int) -> None:
        if isinstance(descriptor, bool) or not isinstance(descriptor, int):
            raise PinnedDirectoryExecutorError("directory descriptor is invalid")
        self._lock = threading.RLock()
        self._descriptor = -1
        self._closed = False
        self._cleanup_failed = False
        self._poisoned = False
        self._hold_owner: int | None = None
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
        self._directory_identity = PinnedDirectoryIdentity(
            metadata.st_dev, metadata.st_ino, process_identity.uid
        )
        self._process_identity = process_identity

    @property
    def identity(self) -> PinnedDirectoryIdentity:
        """Return immutable identity evidence without pathname or descriptor."""
        return self._directory_identity

    def run(
        self,
        command: list[str],
        *,
        environment: Mapping[str, str],
        input_bytes: bytes = b"",
        timeout_seconds: int = 30,
    ) -> ProcessOutput:
        """Run while the descriptor remains live; export no path or descriptor."""
        command_snapshot = self._validated_command(
            command, input_bytes, timeout_seconds
        )
        clean_environment = self._validate_environment(environment)
        with self._lock:
            self._verify_locked()
            cwd = Path(f"/proc/self/fd/{self._descriptor}")
            executor = SubprocessExecutor(
                lambda _: clean_environment,
                process_identity=self._process_identity,
            )
            try:
                output = executor.run(
                    list(command_snapshot), input_bytes=input_bytes, cwd=cwd,
                    timeout_seconds=timeout_seconds,
                )
            except (CodexReviewError, OSError):
                raise PinnedDirectoryExecutorError(
                    "directory command process failed"
                ) from None
            self._verify_locked()
            return output

    def run_with_workspace_descriptor(
        self,
        command: list[str],
        *,
        environment: Mapping[str, str],
        descriptor: int,
        expected_identity: WorkspaceIdentity,
        input_bytes: bytes = b"",
        timeout_seconds: int = 30,
    ) -> ProcessOutput:
        """Run a trusted call with one internally substituted workspace fd."""
        command_snapshot = self._validated_command(
            command, input_bytes, timeout_seconds
        )
        clean_environment = self._validate_environment(environment)
        with self._lock:
            self._verify_locked()
            cwd = Path(f"/proc/self/fd/{self._descriptor}")
            executor = SubprocessExecutor(
                lambda _: clean_environment,
                process_identity=self._process_identity,
            )
            process_failed = False
            try:
                output = executor.run_with_workspace_descriptor(
                    list(command_snapshot), descriptor=descriptor,
                    expected_identity=expected_identity,
                    input_bytes=input_bytes, cwd=cwd,
                    timeout_seconds=timeout_seconds,
                )
            except (CodexReviewError, OSError):
                process_failed = True
                output = None
            if process_failed or output is None:
                raise PinnedDirectoryExecutorError(
                    "workspace argument process failed"
                ) from None
            self._verify_locked()
            return output

    def verify_worktree_admin_binding(
        self, *, descriptor: int, expected_identity: WorkspaceIdentity
    ) -> WorktreeAdminBinding:
        """Bind a pinned workspace marker to this repository's admin entry."""
        if (
            isinstance(descriptor, bool)
            or not isinstance(descriptor, int)
            or type(expected_identity) is not WorkspaceIdentity
        ):
            raise PinnedDirectoryExecutorError("worktree binding input is invalid")
        with self._lock:
            self._verify_locked()
            opened: list[int] = []
            failed = False
            try:
                workspace_fd = os.dup(descriptor)
                opened.append(workspace_fd)
                workspace_stat = os.fstat(workspace_fd)
                if (
                    not stat.S_ISDIR(workspace_stat.st_mode)
                    or stat.S_IMODE(workspace_stat.st_mode) != 0o700
                    or workspace_stat.st_uid != self._process_identity.uid
                    or (workspace_stat.st_dev, workspace_stat.st_ino)
                    != (expected_identity.device, expected_identity.inode)
                ):
                    raise OSError("workspace identity mismatch")
                marker_fd = os.open(
                    ".git", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=workspace_fd
                )
                opened.append(marker_fd)
                marker_stat = os.fstat(marker_fd)
                marker_payload = os.read(marker_fd, 4097)
                if (
                    not stat.S_ISREG(marker_stat.st_mode)
                    or marker_stat.st_uid != self._process_identity.uid
                    or marker_stat.st_size != len(marker_payload)
                ):
                    raise OSError("workspace marker is unsafe")
                target = parse_worktree_marker(marker_payload)
                git_fd = os.open(
                    ".git", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=self._descriptor,
                )
                opened.append(git_fd)
                worktrees_fd = os.open(
                    "worktrees", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=git_fd,
                )
                opened.append(worktrees_fd)
                admin_fd = os.open(
                    target.admin_name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=worktrees_fd,
                )
                opened.append(admin_fd)
                target_fd = os.open(
                    target.admin_path,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                )
                opened.append(target_fd)
                admin_stat = os.fstat(admin_fd)
                if (admin_stat.st_dev, admin_stat.st_ino) != (
                    os.fstat(target_fd).st_dev, os.fstat(target_fd).st_ino
                ):
                    raise OSError("admin target mismatch")
                backlink_fd = os.open(
                    "gitdir", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=admin_fd
                )
                opened.append(backlink_fd)
                backlink_stat = os.fstat(backlink_fd)
                backlink = os.read(backlink_fd, 4097)
                parent = self._parse_worktree_backlink(backlink)
                if (
                    not stat.S_ISREG(backlink_stat.st_mode)
                    or backlink_stat.st_uid != self._process_identity.uid
                    or backlink_stat.st_size != len(backlink)
                ):
                    raise OSError("admin backlink is unsafe")
                parent_fd = os.open(
                    parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                )
                opened.append(parent_fd)
                parent_stat = os.fstat(parent_fd)
                if (parent_stat.st_dev, parent_stat.st_ino) != (
                    expected_identity.device, expected_identity.inode
                ):
                    raise OSError("admin backlink mismatch")
                marker_after = os.fstat(marker_fd)
                if (
                    marker_after.st_dev, marker_after.st_ino,
                    marker_after.st_size, marker_after.st_mtime_ns,
                ) != (
                    marker_stat.st_dev, marker_stat.st_ino,
                    marker_stat.st_size, marker_stat.st_mtime_ns,
                ):
                    raise OSError("workspace marker changed")
                binding = WorktreeAdminBinding(
                    target.admin_name, admin_stat.st_dev, admin_stat.st_ino,
                    marker_stat.st_dev, marker_stat.st_ino,
                )
            except (OSError, WorktreeMarkerContractError):
                failed = True
                binding = None
            cleanup_failed = False
            for opened_fd in reversed(opened):
                try:
                    os.close(opened_fd)
                except OSError:
                    cleanup_failed = True
            if failed or cleanup_failed or binding is None:
                raise PinnedDirectoryExecutorError(
                    "worktree admin binding failed"
                ) from None
            self._verify_locked()
            return binding

    def verify(self) -> None:
        """Verify the pinned descriptor without pathname lookup."""
        with self._lock:
            self._verify_locked()

    @contextmanager
    def hold_execution(self) -> Iterator[None]:
        """Serialize a trusted multi-command operation without exporting authority."""
        self._lock.acquire()
        try:
            if self._hold_owner is not None:
                raise PinnedDirectoryExecutorError(
                    "directory execution is already held"
                )
            self._verify_locked()
            self._hold_owner = threading.get_ident()
            operation_error: BaseException | None = None
            verification_failed = False
            try:
                try:
                    yield
                except BaseException as error:
                    operation_error = error
                try:
                    self._verify_locked()
                except PinnedDirectoryExecutorError:
                    verification_failed = True
            finally:
                self._hold_owner = None
            if verification_failed:
                self._poisoned = True
                if operation_error is None:
                    raise PinnedDirectoryExecutorError(
                        "directory hold verification failed"
                    ) from None
            if operation_error is not None:
                raise operation_error
        finally:
            self._lock.release()

    def close(self) -> None:
        """Synchronously and idempotently release command authority."""
        with self._lock:
            if self._hold_owner == threading.get_ident():
                raise PinnedDirectoryExecutorError(
                    "directory execution hold is active"
                )
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
        if self._poisoned:
            raise PinnedDirectoryExecutorError("directory executor is poisoned")
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
        if (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_uid,
        ) != (
            self._directory_identity.device,
            self._directory_identity.inode,
            self._directory_identity.uid,
        ):
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
    def _parse_worktree_backlink(payload: bytes) -> PurePosixPath:
        if (
            not isinstance(payload, bytes)
            or not 7 <= len(payload) <= 4096
            or not payload.endswith(b"/.git\n")
            or not payload.startswith(b"/")
            or not payload[:-1].isascii()
        ):
            raise OSError("admin backlink is invalid")
        raw_parent = payload[:-6]
        if not re.fullmatch(rb"/[A-Za-z0-9._/+:-]{1,4000}", raw_parent):
            raise OSError("admin backlink path is invalid")
        parent = PurePosixPath(raw_parent.decode("ascii"))
        if parent.as_posix().encode("ascii") != raw_parent or ".." in parent.parts:
            raise OSError("admin backlink is not canonical")
        return parent

    @staticmethod
    def _validated_command(
        command: list[str], input_bytes: bytes, timeout_seconds: int
    ) -> tuple[str, ...]:
        if type(command) is not list:
            raise PinnedDirectoryExecutorError("directory command is invalid")
        snapshot = tuple(command)
        if (
            not 1 <= len(snapshot) <= 64
            or not isinstance(snapshot[0], str)
            or not Path(snapshot[0]).is_absolute()
            or any(
                not isinstance(argument, str)
                or not argument.isascii()
                or "\x00" in argument
                or len(argument) > 4096
                for argument in snapshot
            )
            or not isinstance(input_bytes, bytes)
            or len(input_bytes) > MAX_INPUT_BYTES
            or isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 1 <= timeout_seconds <= 600
        ):
            raise PinnedDirectoryExecutorError("directory command is invalid")
        return snapshot

    @staticmethod
    def _validate_environment(environment: Mapping[str, str]) -> dict[str, str]:
        if not isinstance(environment, Mapping) or len(environment) > 32:
            raise PinnedDirectoryExecutorError("directory environment is invalid")
        clean: dict[str, str] = {}
        for key, value in environment.items():
            if (
                not isinstance(key, str)
                or not ENV_KEY.fullmatch(key)
                or key not in ENV_KEYS
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

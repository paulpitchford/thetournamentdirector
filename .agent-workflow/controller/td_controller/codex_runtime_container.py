"""Read-only pinned Codex runtime attestation inside the worker container."""

from __future__ import annotations

import hashlib
import os
import pwd
import re
import shutil
import stat
import tempfile
from pathlib import Path

from .podman_mount_policy import (
    IMAGE,
    PODMAN,
    MountPolicyFixture,
    PodmanMountPolicyError,
    PodmanMountPolicyProbe,
)
from .review_contract import CodexReviewError
from .review_runtime import (
    PINNED_CODEX_SHA256,
    PINNED_CODEX_SIZE,
    PINNED_CODEX_VERSION,
    PINNED_RUNTIME_RELATIVE_DIR,
    CommandExecutor,
    ProcessOutput,
    SubprocessExecutor,
)
from .workspace_identity_handle import (
    WorkspaceIdentity,
    WorkspaceIdentityHandle,
    WorkspaceIdentityHandleError,
)

CONTAINER_CODEX = "/opt/td-runtime/codex"
VERIFIED_CODEX = "/home/codex/codex-verified"
CONTAINER_PATH = (
    "/home/codex/.local/bin:/home/codex:/opt/td-runtime:/usr/local/sbin:/usr/local/bin:"
    "/usr/sbin:/usr/bin:/sbin:/bin"
)
SAFE_PATH = re.compile(r"/[A-Za-z0-9._/-]{1,1023}")
RUNTIME_LAUNCHER = f"""
cp {CONTAINER_CODEX} {VERIFIED_CODEX} 2>/dev/null || exit 45
chmod 500 {VERIFIED_CODEX} 2>/dev/null || exit 45
actual_size=$(stat -c '%s' {VERIFIED_CODEX} 2>/dev/null) || exit 45
if test "$actual_size" != "{PINNED_CODEX_SIZE}"; then exit 45; fi
actual_digest=$(sha256sum {VERIFIED_CODEX} 2>/dev/null) || exit 46
if test "${{actual_digest%% *}}" != "{PINNED_CODEX_SHA256}"; then exit 46; fi
test "$(id -u)" != 0
awk '
  index($0, "td-mount-policy-") {{
    seen = 1
    if ($5 != "/workspace") exit 41
  }}
  END {{ if (!seen) exit 42 }}
' /proc/self/mountinfo
actual_workspace=$(stat -c '%d:%i' /workspace)
if test "$actual_workspace" != "$EXPECTED_WORKSPACE_IDENTITY"; then exit 43; fi
exec {VERIFIED_CODEX} --version
""".strip()


class CodexRuntimeContainerError(RuntimeError):
    """Raised when the mounted Codex runtime fails exact attestation."""


class CodexRuntimeContainerProbe:
    """Attest and execute only the pinned Codex version command."""

    def __init__(self, *, executor: CommandExecutor | None = None) -> None:
        if executor is not None and not isinstance(executor, SubprocessExecutor):
            raise CodexRuntimeContainerError("bounded runtime executor is required")
        self._executor = executor or SubprocessExecutor(
            PodmanMountPolicyProbe.environment
        )

    def run(
        self,
        fixture: MountPolicyFixture,
        handle: WorkspaceIdentityHandle,
        *,
        runtime_path: Path | None = None,
    ) -> None:
        if not isinstance(handle, WorkspaceIdentityHandle):
            raise CodexRuntimeContainerError("workspace handle is invalid")
        runtime = runtime_path or self.trusted_runtime_path()
        self._validate_runtime(runtime)
        try:
            with handle.hold_identity() as identity:
                result = self._execute(fixture, identity, runtime)
        except WorkspaceIdentityHandleError as exc:
            raise CodexRuntimeContainerError("workspace handle is unavailable") from exc
        if (
            result.returncode != 0
            or result.stdout != f"{PINNED_CODEX_VERSION}\n".encode()
            or result.stderr
        ):
            raise CodexRuntimeContainerError("Codex container proof failed")

    def prove_replacement_rejected(
        self,
        fixture: MountPolicyFixture,
        handle: WorkspaceIdentityHandle,
        runtime_path: Path,
    ) -> None:
        if runtime_path.parent != fixture.root or runtime_path.name != "codex-staged":
            raise CodexRuntimeContainerError("replacement fixture path is invalid")
        self._validate_runtime(runtime_path)
        held = runtime_path.with_name("codex-held")
        if held.exists():
            raise CodexRuntimeContainerError("replacement fixture is not empty")
        try:
            with handle.hold_identity() as identity:
                runtime_path.rename(held)
                runtime_path.write_bytes(b"replacement")
                runtime_path.chmod(0o700)
                result = self._execute(fixture, identity, runtime_path)
        except WorkspaceIdentityHandleError as exc:
            raise CodexRuntimeContainerError("workspace handle is unavailable") from exc
        finally:
            if runtime_path.exists():
                runtime_path.unlink()
            if held.exists():
                held.rename(runtime_path)
        if result.returncode != 45 or result.stdout or result.stderr:
            raise CodexRuntimeContainerError("runtime replacement was not rejected")

    def _execute(
        self,
        fixture: MountPolicyFixture,
        identity: WorkspaceIdentity,
        runtime_path: Path,
    ) -> ProcessOutput:
        name = f"td-codex-runtime-{identity.generation}"
        command = list(self._command(fixture, identity, runtime_path))
        runtime_started = False
        try:
            rootless = self._executor.run(
                [str(PODMAN), "info", "--format", "{{.Host.Security.Rootless}}"],
                input_bytes=b"", cwd=fixture.root, timeout_seconds=30,
            )
            if (
                rootless.returncode != 0
                or rootless.stdout != b"true\n"
                or rootless.stderr
            ):
                raise CodexRuntimeContainerError(
                    "rootless runtime attestation failed"
                )
            runtime_started = True
            return self._executor.run(
                command, input_bytes=b"", cwd=fixture.root, timeout_seconds=30,
            )
        except (CodexReviewError, OSError) as exc:
            if runtime_started:
                self._cleanup_container(fixture.root, name)
            raise CodexRuntimeContainerError("Codex container process failed") from exc

    def _command(
        self,
        fixture: MountPolicyFixture,
        identity: WorkspaceIdentity,
        runtime_path: Path,
    ) -> tuple[str, ...]:
        try:
            command = list(
                PodmanMountPolicyProbe(executor=self._executor).command(fixture)
            )
        except PodmanMountPolicyError as exc:
            raise CodexRuntimeContainerError("workspace fixture is invalid") from exc
        home_index = command.index("HOME=/tmp")
        command[home_index] = "HOME=/home/codex"
        command[2:2] = ["--name", f"td-codex-runtime-{identity.generation}"]
        image_index = command.index(IMAGE)
        memory_index = command.index("--memory=64m")
        command[memory_index] = "--memory=384m"
        command[image_index:image_index] = [
            "--env", f"PATH={CONTAINER_PATH}",
            "--tmpfs", "/home/codex:rw,nosuid,nodev,size=300m",
            "--mount", f"type=bind,src={runtime_path},dst={CONTAINER_CODEX},ro",
            "--env",
            f"EXPECTED_WORKSPACE_IDENTITY={identity.device}:{identity.inode}",
        ]
        image_index = command.index(IMAGE)
        command[image_index:] = [
            IMAGE, "/bin/sh", "-eu", "-c", RUNTIME_LAUNCHER,
        ]
        return tuple(command)

    def _cleanup_container(self, cwd: Path, name: str) -> None:
        try:
            self._executor.run(
                [str(PODMAN), "rm", "-f", "--time=1", name],
                input_bytes=b"", cwd=cwd, timeout_seconds=30,
            )
            remaining = self._executor.run(
                [str(PODMAN), "container", "exists", name],
                input_bytes=b"", cwd=cwd, timeout_seconds=30,
            )
        except (CodexReviewError, OSError) as exc:
            raise CodexRuntimeContainerError("Codex container cleanup failed") from exc
        if remaining.returncode != 1 or remaining.stdout or remaining.stderr:
            raise CodexRuntimeContainerError("Codex container cleanup failed")

    @staticmethod
    def trusted_runtime_path() -> Path:
        try:
            home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
            path = (home / PINNED_RUNTIME_RELATIVE_DIR / "codex").resolve(strict=True)
        except (KeyError, OSError, RuntimeError) as exc:
            raise CodexRuntimeContainerError("trusted Codex path is unavailable") from exc
        return path

    @staticmethod
    def _validate_runtime(path: Path) -> None:
        if not isinstance(path, Path) or not SAFE_PATH.fullmatch(str(path)):
            raise CodexRuntimeContainerError("Codex runtime path is invalid")
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise CodexRuntimeContainerError("Codex runtime path is not canonical")
        metadata = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_size != PINNED_CODEX_SIZE
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise CodexRuntimeContainerError("Codex runtime metadata is invalid")
        digest = hashlib.sha256()
        with path.open("rb") as runtime:
            while chunk := runtime.read(1_048_576):
                digest.update(chunk)
        if digest.hexdigest() != PINNED_CODEX_SHA256:
            raise CodexRuntimeContainerError("Codex runtime digest is invalid")


def run_local_probe() -> None:
    with tempfile.TemporaryDirectory(
        prefix="td-mount-policy-", dir="/var/tmp"
    ) as temporary:
        root = Path(temporary)
        task = root / "task"
        sibling = root / "sibling"
        task.mkdir(mode=0o700)
        sibling.mkdir(mode=0o700)
        descriptor = os.open(task, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            handle = WorkspaceIdentityHandle(
                "ORCH-003D1B0E", attempt=1, generation="0" * 32,
                descriptor=descriptor,
            )
        finally:
            os.close(descriptor)
        fixture = MountPolicyFixture(root, task, sibling)
        probe = CodexRuntimeContainerProbe()
        staged = root / "codex-staged"
        try:
            probe.run(fixture, handle)
            shutil.copyfile(probe.trusted_runtime_path(), staged)
            staged.chmod(0o700)
            probe.prove_replacement_rejected(fixture, handle, staged)
            handle.verify()
        finally:
            handle.close()


if __name__ == "__main__":
    run_local_probe()
    print("Codex runtime container proof passed.")

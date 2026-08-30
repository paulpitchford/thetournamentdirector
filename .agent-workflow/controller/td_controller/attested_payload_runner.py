"""Bounded argv payload execution after same-container workspace checks."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .podman_mount_policy import (
    IMAGE,
    PODMAN,
    MountPolicyFixture,
    PodmanMountPolicyError,
    PodmanMountPolicyProbe,
)
from .review_contract import CodexReviewError
from .review_runtime import CommandExecutor, ProcessOutput, SubprocessExecutor
from .workspace_identity_handle import (
    WorkspaceIdentity,
    WorkspaceIdentityHandle,
    WorkspaceIdentityHandleError,
)

MAX_ARGUMENTS = 64
MAX_ARGUMENT_BYTES = 65_536
MAX_STDIN_BYTES = 512_000
EXECUTABLE_PATTERN = re.compile(
    r"/(?:bin|usr/bin|opt/td-runtime)/[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
)
LAUNCHER_SCRIPT = r"""
test "$(id -u)" != 0
test -d /workspace
awk '
  index($0, "td-mount-policy-") {
    seen = 1
    if ($5 != "/workspace") exit 41
  }
  END { if (!seen) exit 42 }
' /proc/self/mountinfo
actual=$(stat -c '%d:%i' /workspace)
if test "$actual" != "$EXPECTED_WORKSPACE_IDENTITY"; then
  exit 43
fi
exec "$@"
""".strip()


class AttestedPayloadRunnerError(RuntimeError):
    """Raised when a bounded payload cannot be dispatched safely."""


@dataclass(frozen=True, slots=True)
class PayloadCommand:
    executable: str
    arguments: tuple[str, ...] = ()


class AttestedPayloadRunner:
    """Hold one live identity through checks and an argv-preserving payload."""

    def __init__(self, *, executor: CommandExecutor | None = None) -> None:
        if executor is not None and not isinstance(executor, SubprocessExecutor):
            raise AttestedPayloadRunnerError("bounded payload executor is required")
        self._executor = executor or SubprocessExecutor(
            PodmanMountPolicyProbe.environment
        )

    def run(
        self,
        fixture: MountPolicyFixture,
        handle: WorkspaceIdentityHandle,
        payload: PayloadCommand,
        *,
        input_bytes: bytes = b"",
    ) -> ProcessOutput:
        self._validate_payload(payload, input_bytes)
        if not isinstance(handle, WorkspaceIdentityHandle):
            raise AttestedPayloadRunnerError("workspace handle is invalid")
        try:
            with handle.hold_identity() as identity:
                return self._run_held(
                    fixture, identity, payload, input_bytes=input_bytes
                )
        except WorkspaceIdentityHandleError as exc:
            raise AttestedPayloadRunnerError("workspace handle is unavailable") from exc

    def _run_held(
        self,
        fixture: MountPolicyFixture,
        identity: WorkspaceIdentity,
        payload: PayloadCommand,
        *,
        input_bytes: bytes,
    ) -> ProcessOutput:
        name = f"td-payload-{identity.generation}"
        command = list(self._command(fixture, identity, payload))
        payload_started = False
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
                raise AttestedPayloadRunnerError(
                    "rootless runtime attestation failed"
                )
            payload_started = True
            return self._executor.run(
                command,
                input_bytes=input_bytes,
                cwd=fixture.root,
                timeout_seconds=60,
            )
        except (CodexReviewError, OSError) as exc:
            if payload_started:
                self._cleanup_container(fixture.root, name)
            raise AttestedPayloadRunnerError("payload process failed") from exc

    def _command(
        self,
        fixture: MountPolicyFixture,
        identity: WorkspaceIdentity,
        payload: PayloadCommand,
    ) -> tuple[str, ...]:
        try:
            command = list(
                PodmanMountPolicyProbe(executor=self._executor).command(fixture)
            )
        except PodmanMountPolicyError as exc:
            raise AttestedPayloadRunnerError("workspace fixture is invalid") from exc
        expected = f"{identity.device}:{identity.inode}"
        command[2:2] = ["--name", f"td-payload-{identity.generation}"]
        image_index = command.index(IMAGE)
        command[image_index:image_index] = [
            "--interactive",
            "--env",
            f"EXPECTED_WORKSPACE_IDENTITY={expected}",
        ]
        command[image_index + 3:] = [
            IMAGE,
            "/bin/sh",
            "-eu",
            "-c",
            LAUNCHER_SCRIPT,
            "td-workspace-launcher",
            payload.executable,
            *payload.arguments,
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
            raise AttestedPayloadRunnerError("payload cleanup failed") from exc
        if remaining.returncode != 1 or remaining.stdout or remaining.stderr:
            raise AttestedPayloadRunnerError("payload cleanup failed")

    @staticmethod
    def _validate_payload(payload: PayloadCommand, input_bytes: bytes) -> None:
        if not isinstance(payload, PayloadCommand):
            raise AttestedPayloadRunnerError("payload command is invalid")
        if not EXECUTABLE_PATTERN.fullmatch(payload.executable):
            raise AttestedPayloadRunnerError("payload executable is invalid")
        if not isinstance(payload.arguments, tuple) or len(payload.arguments) > MAX_ARGUMENTS:
            raise AttestedPayloadRunnerError("payload arguments are invalid")
        total = len(payload.executable.encode("utf-8"))
        for argument in payload.arguments:
            if not isinstance(argument, str) or not argument.isascii():
                raise AttestedPayloadRunnerError("payload argument is invalid")
            encoded = argument.encode("ascii")
            if b"\x00" in encoded or len(encoded) > 4_096:
                raise AttestedPayloadRunnerError("payload argument is invalid")
            total += len(encoded)
        if total > MAX_ARGUMENT_BYTES:
            raise AttestedPayloadRunnerError("payload arguments exceed the limit")
        if not isinstance(input_bytes, bytes) or len(input_bytes) > MAX_STDIN_BYTES:
            raise AttestedPayloadRunnerError("payload input exceeds the limit")


def run_local_probe() -> None:
    with tempfile.TemporaryDirectory(
        prefix="td-mount-policy-", dir="/var/tmp"
    ) as temporary:
        root = Path(temporary)
        task = root / "task"
        sibling = root / "sibling"
        task.mkdir(mode=0o700)
        sibling.mkdir(mode=0o700)
        descriptor = os.open(
            task, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            handle = WorkspaceIdentityHandle(
                "ORCH-003D1B0D", attempt=1, generation="0" * 32,
                descriptor=descriptor,
            )
        finally:
            os.close(descriptor)
        fixture = MountPolicyFixture(root, task, sibling)
        payload = PayloadCommand(
            "/bin/busybox",
            ("printf", "%s\n", "$(touch /workspace/not-run)"),
        )
        runner = AttestedPayloadRunner()
        held = root / "held"
        try:
            task.rename(held)
            task.mkdir(mode=0o700)
            rejected = runner.run(fixture, handle, payload)
            if rejected.returncode != 43 or rejected.stdout or rejected.stderr:
                raise AttestedPayloadRunnerError("replacement mount was not rejected")
            task.rmdir()
            held.rename(task)
            accepted = runner.run(fixture, handle, payload)
            if (
                accepted.returncode != 0
                or accepted.stdout != b"$(touch /workspace/not-run)\n"
                or accepted.stderr
            ):
                raise AttestedPayloadRunnerError("argv payload proof failed")
            if (task / "not-run").exists() or tuple(sibling.iterdir()):
                raise AttestedPayloadRunnerError("payload argument was interpreted")
            try:
                runner.run(
                    fixture, handle,
                    PayloadCommand("/bin/busybox", ("yes",)),
                )
            except AttestedPayloadRunnerError:
                pass
            else:
                raise AttestedPayloadRunnerError("payload output was not bounded")
            handle.verify()
        finally:
            handle.close()


if __name__ == "__main__":
    run_local_probe()
    print("Attested payload argv proof passed.")

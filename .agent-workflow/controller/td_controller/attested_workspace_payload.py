"""Run one fixed proof payload after same-container workspace attestation."""

from __future__ import annotations

import os
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
from .review_runtime import CommandExecutor, ProcessOutput, SubprocessExecutor
from .workspace_identity_handle import (
    WorkspaceIdentity,
    WorkspaceIdentityHandle,
    WorkspaceIdentityHandleError,
)

ATTESTED_PAYLOAD_SCRIPT = r"""
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
if ! mkdir /workspace/.payload-proof 2>/dev/null; then
  exit 44
fi
printf 'same-container-payload\n' > /workspace/.payload-proof/result
printf 'attested-payload-ok\n'
""".strip()


class AttestedWorkspacePayloadError(RuntimeError):
    """Raised when attestation does not authorize the fixed proof payload."""


class AttestedWorkspacePayload:
    """Hold one live identity through mount attestation and payload completion."""

    def __init__(self, *, executor: CommandExecutor | None = None) -> None:
        self._executor = executor or SubprocessExecutor(
            PodmanMountPolicyProbe.environment
        )

    def run(
        self, fixture: MountPolicyFixture, handle: WorkspaceIdentityHandle
    ) -> None:
        if not isinstance(handle, WorkspaceIdentityHandle):
            raise AttestedWorkspacePayloadError("workspace handle is invalid")
        try:
            with handle.hold_identity() as identity:
                result = self._run_held(fixture, identity)
        except WorkspaceIdentityHandleError as exc:
            raise AttestedWorkspacePayloadError("workspace handle is unavailable") from exc
        if (
            result.returncode != 0
            or result.stdout != b"attested-payload-ok\n"
            or result.stderr
        ):
            raise AttestedWorkspacePayloadError("attested payload failed")
        try:
            marker = (fixture.task / ".payload-proof" / "result").read_bytes()
        except OSError as exc:
            raise AttestedWorkspacePayloadError("payload marker is unavailable") from exc
        if marker != b"same-container-payload\n":
            raise AttestedWorkspacePayloadError("payload marker is invalid")

    def verify_rejected(
        self,
        fixture: MountPolicyFixture,
        handle: WorkspaceIdentityHandle,
        *,
        expected_exit: int,
    ) -> None:
        if expected_exit not in {41, 43, 44}:
            raise AttestedWorkspacePayloadError("rejection exit is invalid")
        if not isinstance(handle, WorkspaceIdentityHandle):
            raise AttestedWorkspacePayloadError("workspace handle is invalid")
        marker = fixture.task / ".payload-proof"
        marker_existed = os.path.lexists(marker)
        try:
            with handle.hold_identity() as identity:
                result = self._run_held(fixture, identity)
        except WorkspaceIdentityHandleError as exc:
            raise AttestedWorkspacePayloadError("workspace handle is unavailable") from exc
        if result.returncode != expected_exit or result.stdout or result.stderr:
            raise AttestedWorkspacePayloadError("workspace rejection was not exact")
        if not marker_existed and os.path.lexists(marker):
            raise AttestedWorkspacePayloadError("rejected workspace was modified")

    def _run_held(
        self, fixture: MountPolicyFixture, identity: WorkspaceIdentity
    ) -> ProcessOutput:
        command = list(self._command(fixture, identity))
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
                raise AttestedWorkspacePayloadError(
                    "rootless runtime attestation failed"
                )
            return self._executor.run(
                command, input_bytes=b"", cwd=fixture.root, timeout_seconds=60,
            )
        except (CodexReviewError, OSError) as exc:
            raise AttestedWorkspacePayloadError("payload process failed") from exc

    def _command(
        self, fixture: MountPolicyFixture, identity: WorkspaceIdentity
    ) -> tuple[str, ...]:
        try:
            command = list(
                PodmanMountPolicyProbe(executor=self._executor).command(fixture)
            )
        except PodmanMountPolicyError as exc:
            raise AttestedWorkspacePayloadError("workspace fixture is invalid") from exc
        image_index = command.index(IMAGE)
        expected = f"{identity.device}:{identity.inode}"
        command[image_index:image_index] = [
            "--env", f"EXPECTED_WORKSPACE_IDENTITY={expected}"
        ]
        command[-1] = ATTESTED_PAYLOAD_SCRIPT
        return tuple(command)


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
                "ORCH-003D1B0C", attempt=1, generation="0" * 32,
                descriptor=descriptor,
            )
        finally:
            os.close(descriptor)
        fixture = MountPolicyFixture(root, task, sibling)
        held = root / "held"
        payload = AttestedWorkspacePayload()
        try:
            task.rename(held)
            task.mkdir(mode=0o700)
            payload.verify_rejected(fixture, handle, expected_exit=43)
            task.rmdir()
            held.rename(task)
            marker = task / ".payload-proof"
            sentinel = task / "sentinel"
            sentinel.write_text("retain")
            marker.symlink_to("sentinel")
            payload.verify_rejected(fixture, handle, expected_exit=44)
            if sentinel.read_text() != "retain":
                raise AttestedWorkspacePayloadError("symlink target was modified")
            marker.unlink()
            payload.run(fixture, handle)
            handle.verify()
        finally:
            handle.close()


if __name__ == "__main__":
    run_local_probe()
    print("Attested workspace payload proof passed.")

"""Pre-payload inode attestation for a Podman task workspace mount."""

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
from .workspace_identity_handle import WorkspaceIdentity, WorkspaceIdentityHandle

ATTESTED_WORKER_SCRIPT = r"""
actual=$(stat -c '%d:%i' /workspace)
if test "$actual" != "$EXPECTED_WORKSPACE_IDENTITY"; then
  exit 43
fi
printf 'attested-workspace-write\n' > /workspace/attested-proof.txt
printf 'workspace-mount-attested\n'
""".strip()


class WorkspaceMountAttestationError(RuntimeError):
    """Raised when the mounted inode is not the controller-pinned identity."""


class WorkspaceMountAttestation:
    """Run a fixed identity check before the first workspace write."""

    def __init__(self, *, executor: CommandExecutor | None = None) -> None:
        self._executor = executor or SubprocessExecutor(
            PodmanMountPolicyProbe.environment
        )

    def command(
        self, fixture: MountPolicyFixture, identity: WorkspaceIdentity
    ) -> tuple[str, ...]:
        if not isinstance(identity, WorkspaceIdentity):
            raise WorkspaceMountAttestationError("workspace identity is invalid")
        try:
            command = list(
                PodmanMountPolicyProbe(executor=self._executor).command(fixture)
            )
        except PodmanMountPolicyError as exc:
            raise WorkspaceMountAttestationError("workspace fixture is invalid") from exc
        image_index = command.index(IMAGE)
        expected = f"{identity.device}:{identity.inode}"
        command[image_index:image_index] = [
            "--env", f"EXPECTED_WORKSPACE_IDENTITY={expected}"
        ]
        command[-1] = ATTESTED_WORKER_SCRIPT
        return tuple(command)

    def verify(
        self, fixture: MountPolicyFixture, identity: WorkspaceIdentity
    ) -> None:
        result = self._run_container(fixture, identity)
        if (
            result.returncode != 0
            or result.stdout != b"workspace-mount-attested\n"
            or result.stderr
        ):
            raise WorkspaceMountAttestationError("workspace mount attestation failed")
        try:
            marker = (fixture.task / "attested-proof.txt").read_bytes()
        except OSError as exc:
            raise WorkspaceMountAttestationError("attested write is unavailable") from exc
        if marker != b"attested-workspace-write\n":
            raise WorkspaceMountAttestationError("attested write is invalid")

    def verify_replacement_rejected(
        self, fixture: MountPolicyFixture, identity: WorkspaceIdentity
    ) -> None:
        result = self._run_container(fixture, identity)
        if result.returncode != 43 or result.stdout or result.stderr:
            raise WorkspaceMountAttestationError("replacement mount was not rejected")
        if (fixture.task / "attested-proof.txt").exists():
            raise WorkspaceMountAttestationError("replacement workspace was modified")

    def _run_container(
        self, fixture: MountPolicyFixture, identity: WorkspaceIdentity
    ) -> ProcessOutput:
        command = list(self.command(fixture, identity))
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
                raise WorkspaceMountAttestationError(
                    "rootless runtime attestation failed"
                )
            return self._executor.run(
                command, input_bytes=b"", cwd=fixture.root, timeout_seconds=60,
            )
        except (CodexReviewError, OSError) as exc:
            raise WorkspaceMountAttestationError(
                "workspace mount process failed"
            ) from exc


def run_local_probe() -> None:
    """Prove a mounted replacement is rejected before the positive write."""
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
                "ORCH-003D1B0B", attempt=1, generation="0" * 32,
                descriptor=descriptor,
            )
        finally:
            os.close(descriptor)
        fixture = MountPolicyFixture(root, task, sibling)
        held = root / "held"
        probe = WorkspaceMountAttestation()
        try:
            task.rename(held)
            task.mkdir(mode=0o700)
            probe.verify_replacement_rejected(fixture, handle.identity)
            task.rmdir()
            held.rename(task)
            probe.verify(fixture, handle.identity)
            handle.verify()
        finally:
            handle.close()


if __name__ == "__main__":
    run_local_probe()
    print("Workspace mount attestation proof passed.")

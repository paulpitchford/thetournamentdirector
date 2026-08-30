"""Real rootless-container proof for direct-child workspace isolation."""

from __future__ import annotations

import os
import pwd
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .review_contract import CodexReviewError
from .review_runtime import CommandExecutor, SubprocessExecutor
from .workspace_identity_handle import WorkspaceIdentityHandle

PODMAN = Path("/usr/bin/podman")
IMAGE = (
    "docker.io/library/alpine@sha256:"
    "7c8cb692ae09657cbc4a3f3cbd0e8d5a2690ba38386aaaf252dbb060bf5eb2e6"
)
SAFE_PATH = re.compile(r"/[A-Za-z0-9._/-]{1,1023}")
WORKER_SCRIPT = r"""
test "$(id -u)" != 0
test -d /workspace
test ! -e /workspace/../sibling
for fd in /proc/self/fd/*; do
  target=$(readlink "$fd" 2>/dev/null || true)
  case "$target" in *td-workspace-proof-*) exit 20;; esac
done
printf 'contained-worker-write\n' > /workspace/proof.txt
printf 'workspace-isolation-ok\n'
""".strip()


class WorkspaceIsolationError(RuntimeError):
    """Raised when the real containment proof does not pass exactly."""


@dataclass(frozen=True, slots=True)
class IsolationPaths:
    root: Path
    task: Path
    sibling: Path


class WorkspaceIsolationProbe:
    """Build and execute one fixed, no-network rootless Podman probe."""

    def __init__(self, *, executor: CommandExecutor | None = None) -> None:
        self._executor = executor or SubprocessExecutor(self._podman_environment)

    def command(self, paths: IsolationPaths) -> tuple[str, ...]:
        uid = os.getuid()
        gid = os.getgid()
        if uid == 0:
            raise WorkspaceIsolationError("root host execution is forbidden")
        self._validate_paths(paths)
        return (
            str(PODMAN), "run", "--rm", "--pull=never",
            "--network=none", "--read-only", "--cap-drop=all",
            "--security-opt=no-new-privileges", "--pids-limit=32",
            "--memory=64m", "--cpus=0.25", "--userns=keep-id",
            "--user", f"{uid}:{gid}", "--env", "HOME=/tmp",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=8m",
            "--mount", f"type=bind,src={paths.task},dst=/workspace,rw",
            IMAGE, "/bin/sh", "-eu", "-c", WORKER_SCRIPT,
        )

    def run(self, paths: IsolationPaths) -> None:
        command = self.command(paths)
        try:
            rootless = self._executor.run(
                [str(PODMAN), "info", "--format", "{{.Host.Security.Rootless}}"],
                input_bytes=b"", cwd=paths.root, timeout_seconds=30,
            )
            if (
                rootless.returncode != 0
                or rootless.stdout != b"true\n"
                or rootless.stderr
            ):
                raise WorkspaceIsolationError("rootless runtime attestation failed")
            result = self._executor.run(
                list(command), input_bytes=b"", cwd=paths.root, timeout_seconds=60,
            )
        except (CodexReviewError, OSError) as exc:
            raise WorkspaceIsolationError("workspace isolation probe failed") from exc
        if (
            result.returncode != 0
            or result.stdout != b"workspace-isolation-ok\n"
            or result.stderr
        ):
            raise WorkspaceIsolationError("contained workspace proof failed")
        marker = paths.task / "proof.txt"
        try:
            marker_bytes = marker.read_bytes()
        except OSError as exc:
            raise WorkspaceIsolationError("workspace write proof is invalid") from exc
        if marker_bytes != b"contained-worker-write\n":
            raise WorkspaceIsolationError("workspace write proof is invalid")
        if tuple(paths.sibling.iterdir()):
            raise WorkspaceIsolationError("sibling workspace was modified")

    @staticmethod
    def _podman_environment(_: str) -> dict[str, str]:
        uid = os.getuid()
        try:
            home = Path(pwd.getpwuid(uid).pw_dir).resolve(strict=True)
        except (KeyError, OSError, RuntimeError) as exc:
            raise WorkspaceIsolationError("trusted home is unavailable") from exc
        if home.stat().st_uid != uid:
            raise WorkspaceIsolationError("trusted home ownership is invalid")
        return {
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        }

    @staticmethod
    def _validate_paths(paths: IsolationPaths) -> None:
        if not isinstance(paths, IsolationPaths):
            raise WorkspaceIsolationError("workspace paths are invalid")
        for path in (paths.root, paths.task, paths.sibling):
            if not SAFE_PATH.fullmatch(str(path)):
                raise WorkspaceIsolationError("workspace path syntax is unsafe")
            if not path.is_absolute() or path.resolve(strict=True) != path:
                raise WorkspaceIsolationError("workspace path is not canonical")
            metadata = path.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise WorkspaceIsolationError("workspace path is not private")
        if paths.task.parent != paths.root or paths.sibling.parent != paths.root:
            raise WorkspaceIsolationError("workspace must be a direct child")
        if paths.task == paths.sibling:
            raise WorkspaceIsolationError("workspace sibling is not distinct")


def run_local_probe() -> None:
    """Provision local fixtures, pin the task identity, and run the real probe."""
    with tempfile.TemporaryDirectory(prefix="td-workspace-proof-", dir="/var/tmp") as temp:
        root = Path(temp)
        task = root / "task"
        sibling = root / "sibling"
        task.mkdir(mode=0o700)
        sibling.mkdir(mode=0o700)
        descriptor = os.open(
            task, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            handle = WorkspaceIdentityHandle(
                "ORCH-003D1B0", attempt=1, generation="0" * 32,
                descriptor=descriptor,
            )
        finally:
            os.close(descriptor)
        try:
            WorkspaceIsolationProbe().run(IsolationPaths(root, task, sibling))
            handle.verify()
        finally:
            handle.close()


if __name__ == "__main__":
    run_local_probe()
    print("Workspace isolation proof passed.")

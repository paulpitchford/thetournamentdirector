"""Controlled Podman configuration and workspace mount-table proof."""

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

PODMAN = Path("/usr/bin/podman")
IMAGE = (
    "docker.io/library/alpine@sha256:"
    "7c8cb692ae09657cbc4a3f3cbd0e8d5a2690ba38386aaaf252dbb060bf5eb2e6"
)
SAFE_PATH = re.compile(r"/[A-Za-z0-9._/-]{1,1023}")
FIXTURE_PREFIX = "td-mount-policy-"
WORKER_SCRIPT = r"""
test "$(id -u)" != 0
test -d /workspace
test ! -e /workspace/../sibling
awk '
  index($0, "td-mount-policy-") {
    seen = 1
    if ($5 != "/workspace") exit 41
  }
  END { if (!seen) exit 42 }
' /proc/self/mountinfo
printf 'mount-policy-write\n' > /workspace/proof.txt
printf 'podman-mount-policy-ok\n'
""".strip()


class PodmanMountPolicyError(RuntimeError):
    """Raised when controlled mount containment is not proven exactly."""


@dataclass(frozen=True, slots=True)
class MountPolicyFixture:
    root: Path
    task: Path
    sibling: Path


class PodmanMountPolicyProbe:
    """Prove controlled config and reject an injected workspace-root mount."""

    def __init__(self, *, executor: CommandExecutor | None = None) -> None:
        self._executor = executor or SubprocessExecutor(self.environment)

    @staticmethod
    def environment(_: str) -> dict[str, str]:
        uid = os.getuid()
        try:
            home = Path(pwd.getpwuid(uid).pw_dir).resolve(strict=True)
        except (KeyError, OSError, RuntimeError) as exc:
            raise PodmanMountPolicyError("trusted home is unavailable") from exc
        if home.stat().st_uid != uid:
            raise PodmanMountPolicyError("trusted home ownership is invalid")
        return {
            "CONTAINERS_CONF": "/dev/null",
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        }

    def command(
        self, fixture: MountPolicyFixture, *, inject_root: bool = False
    ) -> tuple[str, ...]:
        self._validate_fixture(fixture)
        uid = os.getuid()
        if uid == 0:
            raise PodmanMountPolicyError("root host execution is forbidden")
        command = [
            str(PODMAN), "run", "--rm", "--pull=never",
            "--network=none", "--read-only", "--cap-drop=all",
            "--security-opt=no-new-privileges", "--pids-limit=32",
            "--memory=64m", "--cpus=0.25", "--userns=keep-id",
            "--user", f"{uid}:{os.getgid()}", "--env", "HOME=/tmp",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=8m",
            "--mount", f"type=bind,src={fixture.task},dst=/workspace,rw",
        ]
        if inject_root:
            command.extend(
                [
                    "--mount",
                    f"type=bind,src={fixture.root},dst=/unexpected,ro",
                ]
            )
        command.extend([IMAGE, "/bin/sh", "-eu", "-c", WORKER_SCRIPT])
        return tuple(command)

    def run(self, fixture: MountPolicyFixture) -> None:
        normal = list(self.command(fixture))
        adversarial = list(self.command(fixture, inject_root=True))
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
                raise PodmanMountPolicyError("rootless runtime attestation failed")
            accepted = self._executor.run(
                normal, input_bytes=b"", cwd=fixture.root, timeout_seconds=60,
            )
            rejected = self._executor.run(
                adversarial, input_bytes=b"", cwd=fixture.root,
                timeout_seconds=60,
            )
        except (CodexReviewError, OSError) as exc:
            raise PodmanMountPolicyError("Podman mount policy probe failed") from exc
        if (
            accepted.returncode != 0
            or accepted.stdout != b"podman-mount-policy-ok\n"
            or accepted.stderr
        ):
            raise PodmanMountPolicyError("direct task mount was not accepted")
        if rejected.returncode != 41 or rejected.stdout or rejected.stderr:
            raise PodmanMountPolicyError("injected root mount was not rejected")
        try:
            marker = (fixture.task / "proof.txt").read_bytes()
        except OSError as exc:
            raise PodmanMountPolicyError("task write proof is unavailable") from exc
        if marker != b"mount-policy-write\n":
            raise PodmanMountPolicyError("task write proof is invalid")
        if tuple(fixture.sibling.iterdir()):
            raise PodmanMountPolicyError("sibling workspace was modified")

    @staticmethod
    def _validate_fixture(fixture: MountPolicyFixture) -> None:
        if not isinstance(fixture, MountPolicyFixture):
            raise PodmanMountPolicyError("mount fixture is invalid")
        if not fixture.root.name.startswith(FIXTURE_PREFIX):
            raise PodmanMountPolicyError("mount fixture prefix is invalid")
        for path in (fixture.root, fixture.task, fixture.sibling):
            if not SAFE_PATH.fullmatch(str(path)):
                raise PodmanMountPolicyError("mount fixture path is unsafe")
            if not path.is_absolute() or path.resolve(strict=True) != path:
                raise PodmanMountPolicyError("mount fixture is not canonical")
            metadata = path.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise PodmanMountPolicyError("mount fixture is not private")
        if fixture.task.parent != fixture.root or fixture.sibling.parent != fixture.root:
            raise PodmanMountPolicyError("mount fixture child is not direct")
        if fixture.task == fixture.sibling:
            raise PodmanMountPolicyError("mount fixture sibling is not distinct")


def run_local_probe() -> None:
    with tempfile.TemporaryDirectory(prefix=FIXTURE_PREFIX, dir="/var/tmp") as temp:
        root = Path(temp)
        task = root / "task"
        sibling = root / "sibling"
        task.mkdir(mode=0o700)
        sibling.mkdir(mode=0o700)
        PodmanMountPolicyProbe().run(MountPolicyFixture(root, task, sibling))


if __name__ == "__main__":
    run_local_probe()
    print("Podman mount policy proof passed.")

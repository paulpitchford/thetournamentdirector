"""Pinned Codex runtime and bounded systemd-cgroup execution."""

from __future__ import annotations

import hashlib
import os
import pwd
import secrets
import signal
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .review_contract import CodexReviewError

CGROUP_ROOT = Path("/sys/fs/cgroup")
MAX_INPUT_BYTES = 512_000
MAX_OUTPUT_BYTES = 2_000_000
PINNED_CODEX_VERSION = "codex-cli 0.150.1"
PINNED_CODEX_SIZE = 268_330_432
PINNED_CODE_MODE_HOST_SIZE = 57_886_648
PINNED_RUNTIME_RELATIVE_DIR = Path(".local/share/mise/installs/codex/0.150.1/bin")
PINNED_CODEX_SHA256 = "abf1bb1643a79f73aa78ee627e111e02d4f8c98f25813a0cf6ce277709664386"
PINNED_CODE_MODE_HOST_SHA256 = (
    "b3d633427c8c75057fba11dad6051714d44886440305e86ba9d2c0366f4dd63b"
)

@dataclass(frozen=True)
class ProcessOutput:
    returncode: int
    stdout: bytes
    stderr: bytes


def _trusted_home() -> Path:
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
    except (KeyError, OSError, RuntimeError) as exc:
        raise CodexReviewError("trusted account home is unavailable") from exc
    if not home.is_dir() or home.stat().st_uid != os.getuid():
        raise CodexReviewError("trusted account home is invalid")
    return home


def _minimal_codex_environment(executable: str) -> dict[str, str]:
    home = _trusted_home()
    return {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.pathsep.join((str(Path(executable).parent), "/usr/bin", "/bin")),
    }


def _stage_file(
    source: Path, destination: Path, expected_sha256: str, expected_size: int
) -> None:
    temporary = destination.with_name(f".{destination.name}.partial")
    try:
        source_fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(source_fd, "rb") as input_file:
            source_stat = os.fstat(input_file.fileno())
            if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size != expected_size:
                raise CodexReviewError("Codex runtime size does not match the reviewed pin")
            digest = hashlib.sha256()
            with temporary.open("xb") as output_file:
                copied = 0
                while chunk := input_file.read(min(1024 * 1024, expected_size - copied)):
                    copied += len(chunk)
                    if copied > expected_size:
                        raise CodexReviewError("Codex runtime exceeded its size bound")
                    digest.update(chunk)
                    output_file.write(chunk)
                if input_file.read(1) or copied != expected_size:
                    raise CodexReviewError("Codex runtime exceeded its size bound")
                if digest.hexdigest() != expected_sha256:
                    raise CodexReviewError("Codex runtime hash does not match the reviewed pin")
                output_file.flush()
                os.fsync(output_file.fileno())
            temporary.chmod(0o500)
            os.replace(temporary, destination)
    except CodexReviewError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise CodexReviewError("Codex runtime staging failed") from exc


def _minimal_systemd_environment() -> dict[str, str]:
    runtime_dir = f"/run/user/{os.getuid()}"
    return {
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime_dir}/bus",
        "HOME": str(_trusted_home()),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "XDG_RUNTIME_DIR": runtime_dir,
    }


def _pinned_runtime_sources() -> tuple[Path, Path]:
    runtime_dir = _trusted_home() / PINNED_RUNTIME_RELATIVE_DIR
    return runtime_dir / "codex", runtime_dir / "codex-code-mode-host"


def _attest_codex_runtime(destination_dir: Path) -> str:
    destination_dir = destination_dir.resolve(strict=False)
    source, host_source = _pinned_runtime_sources()
    destination_dir.mkdir(mode=0o700)
    staged = destination_dir / "codex"
    _stage_file(source, staged, PINNED_CODEX_SHA256, PINNED_CODEX_SIZE)
    _stage_file(
        host_source,
        destination_dir / "codex-code-mode-host",
        PINNED_CODE_MODE_HOST_SHA256,
        PINNED_CODE_MODE_HOST_SIZE,
    )
    version = subprocess.run(
        [str(staged), "--version"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
        env=_minimal_codex_environment(str(staged)),
    )
    if version.returncode != 0 or version.stdout.decode(errors="replace").strip() != (
        PINNED_CODEX_VERSION
    ):
        raise CodexReviewError("Codex runtime version does not match the reviewed pin")
    return str(staged)


class CommandExecutor(Protocol):
    def run(
        self,
        command: list[str],
        *,
        input_bytes: bytes,
        cwd: Path,
        timeout_seconds: int,
    ) -> ProcessOutput:
        ...


class SubprocessExecutor:
    def __init__(
        self,
        environment_factory: Callable[[str], dict[str, str]] | None = None,
    ) -> None:
        self._environment_factory = environment_factory or _minimal_codex_environment

    def run(
        self,
        command: list[str],
        *,
        input_bytes: bytes,
        cwd: Path,
        timeout_seconds: int,
    ) -> ProcessOutput:
        if len(input_bytes) > MAX_INPUT_BYTES:
            raise CodexReviewError("local Codex review exceeded the input limit")
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=self._environment_factory(command[0]),
            start_new_session=True,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            raise CodexReviewError("failed to create bounded process streams")

        stdout = bytearray()
        stderr = bytearray()
        overflow = threading.Event()
        output_lock = threading.Lock()
        captured_bytes = 0
        thread_errors: list[Exception] = []

        def kill_process_group() -> None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        def read_bounded(stream: Any, buffer: bytearray) -> None:
            nonlocal captured_bytes
            try:
                while chunk := stream.read(65_536):
                    with output_lock:
                        remaining = MAX_OUTPUT_BYTES - captured_bytes
                        accepted = min(len(chunk), remaining)
                        buffer.extend(chunk[:accepted])
                        captured_bytes += accepted
                        if accepted < len(chunk):
                            overflow.set()
                    if overflow.is_set():
                        kill_process_group()
                        return
            except Exception as exc:
                thread_errors.append(exc)
                kill_process_group()
            finally:
                stream.close()

        def write_prompt() -> None:
            try:
                process.stdin.write(input_bytes)
                process.stdin.close()
            except BrokenPipeError:
                pass
            except Exception as exc:
                thread_errors.append(exc)
                kill_process_group()

        threads = [
            threading.Thread(
                target=read_bounded,
                args=(process.stdout, stdout),
                daemon=True,
            ),
            threading.Thread(
                target=read_bounded,
                args=(process.stderr, stderr),
                daemon=True,
            ),
            threading.Thread(target=write_prompt, daemon=True),
        ]
        for thread in threads:
            thread.start()

        timed_out = False
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_process_group()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise CodexReviewError("local Codex process could not be reaped") from exc
        stream_deadline = time.monotonic() + 5
        for thread in threads:
            thread.join(timeout=max(0.0, stream_deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            kill_process_group()
            for thread in threads:
                thread.join(timeout=max(0.0, stream_deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            raise CodexReviewError("local Codex stream worker did not terminate")
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()
        if timed_out:
            raise CodexReviewError("local Codex review timed out")
        if thread_errors:
            raise CodexReviewError("local Codex stream worker failed") from thread_errors[0]
        if overflow.is_set():
            raise CodexReviewError("local Codex review exceeded the output limit")
        return ProcessOutput(
            returncode=process.returncode,
            stdout=bytes(stdout),
            stderr=bytes(stderr),
        )


class SystemdCgroupExecutor:
    def __init__(
        self,
        *,
        delegate: CommandExecutor | None = None,
        unit_name_factory: Callable[[], str] | None = None,
    ) -> None:
        self._delegate = delegate or SubprocessExecutor(
            environment_factory=lambda _: _minimal_systemd_environment()
        )
        self._unit_name_factory = unit_name_factory or (
            lambda: f"td-codex-review-{secrets.token_hex(8)}"
        )

    def run(
        self,
        command: list[str],
        *,
        input_bytes: bytes,
        cwd: Path,
        timeout_seconds: int,
    ) -> ProcessOutput:
        unit = self._unit_name_factory()
        if not unit.startswith("td-codex-review-") or not unit.removeprefix(
            "td-codex-review-"
        ).isalnum():
            raise CodexReviewError("invalid transient review unit name")
        service_environment = _minimal_codex_environment(command[0])
        wrapped = [
            "/usr/bin/systemd-run",
            "--user",
            "--pipe",
            "--wait",
            "--collect",
            "--quiet",
            "--service-type=exec",
            "--unit",
            unit,
            "--working-directory",
            str(cwd),
            "--property=KillMode=control-group",
            "--property=SendSIGKILL=yes",
            "--property=FinalKillSignal=SIGKILL",
            "--property=TimeoutStopSec=2s",
            f"--property=RuntimeMaxSec={max(1, timeout_seconds)}s",
            "--property=TasksMax=64",
            "--property=NoNewPrivileges=yes",
            (
                "--property=InaccessiblePaths="
                f"/run/user/{os.getuid()}/bus /run/user/{os.getuid()}/systemd"
            ),
            "--property=UMask=0077",
            "/usr/bin/env",
            "-i",
            *(f"{key}={value}" for key, value in sorted(service_environment.items())),
            *command,
        ]
        try:
            return self._delegate.run(
                wrapped,
                input_bytes=input_bytes,
                cwd=cwd,
                timeout_seconds=timeout_seconds + 10,
            )
        finally:
            self._kill_transient_unit(unit)

    @staticmethod
    def _kill_transient_unit(unit: str) -> None:
        environment = _minimal_systemd_environment()
        for action in (
            ["stop", f"{unit}.service"],
            ["kill", "--kill-whom=all", "--signal=SIGKILL", f"{unit}.service"],
        ):
            try:
                subprocess.run(
                    ["/usr/bin/systemctl", "--user", *action],
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    env=environment,
                )
            except subprocess.TimeoutExpired:
                continue
        SystemdCgroupExecutor._verify_transient_unit(unit, environment)
        subprocess.run(
            ["/usr/bin/systemctl", "--user", "reset-failed", f"{unit}.service"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            env=environment,
        )

    @staticmethod
    def _verify_transient_unit(unit: str, environment: dict[str, str]) -> None:
        try:
            status = subprocess.run(
                [
                    "/usr/bin/systemctl",
                    "--user",
                    "show",
                    "--property=LoadState",
                    "--property=ActiveState",
                    "--property=SubState",
                    "--property=ControlGroup",
                    f"{unit}.service",
                ],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise CodexReviewError("transient review unit verification timed out") from exc
        if status.returncode != 0 or len(status.stdout) > 4_096:
            raise CodexReviewError("could not verify transient review unit cleanup")
        try:
            properties: dict[str, str] = {}
            for line in status.stdout.decode("utf-8", errors="strict").splitlines():
                key, value = line.split("=", 1)
                if key in properties:
                    raise ValueError("duplicate property")
                properties[key] = value
        except (UnicodeDecodeError, ValueError) as exc:
            raise CodexReviewError("invalid transient review unit state") from exc
        if set(properties) != {"LoadState", "ActiveState", "SubState", "ControlGroup"}:
            raise CodexReviewError("incomplete transient review unit state")
        if properties["LoadState"] not in {"loaded", "not-found"}:
            raise CodexReviewError("invalid transient review unit state")
        if properties.get("ActiveState") not in {"inactive", "failed"}:
            raise CodexReviewError("transient review unit remained active after cleanup")
        if properties.get("SubState") not in {"dead", "failed"}:
            raise CodexReviewError("transient review unit cleanup remained transitional")
        control_group = properties["ControlGroup"]
        if control_group:
            try:
                relative = Path(control_group).relative_to("/")
                cgroup = (CGROUP_ROOT / relative).resolve()
                if not cgroup.is_relative_to(CGROUP_ROOT.resolve()):
                    raise ValueError("escaping cgroup")
                events = cgroup / "cgroup.events"
                populated = events.read_text().splitlines() if cgroup.exists() else []
            except (OSError, ValueError) as exc:
                raise CodexReviewError("invalid transient review cgroup state") from exc
            if cgroup.exists() and "populated 0" not in populated:
                raise CodexReviewError("transient review cgroup remained populated")


def execute_attested_codex(
    destination_dir: Path, arguments: list[str], *, input_bytes: bytes, cwd: Path,
    timeout_seconds: int, executor: CommandExecutor | None = None,
) -> ProcessOutput:
    executable = _attest_codex_runtime(destination_dir)
    boundary = executor or SystemdCgroupExecutor()
    return boundary.run(
        [executable, *arguments], input_bytes=input_bytes, cwd=cwd,
        timeout_seconds=timeout_seconds,
    )

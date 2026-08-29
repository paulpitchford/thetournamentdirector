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
CLEAN_ENV_LAUNCHER = Path(__file__).parent.parent / "bin" / "td-clean-env"
CLEAN_ENV_LAUNCHER_SHA256 = "9aecffdb6bd418fe9f41fc3744f5b388b3bd93e663d4271ba9dacac891c2a7ec"
CLEAN_ENV_LAUNCHER_SIZE = 8_536
MAX_INPUT_BYTES = 512_000
MAX_OUTPUT_BYTES = 2_000_000

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


def _absolute_command(command: list[str]) -> list[str]:
    if not command or not Path(command[0]).is_absolute():
        raise CodexReviewError("review executable path must be absolute")
    return [str(Path(command[0]).resolve(strict=False)), *command[1:]]


def _minimal_codex_environment(executable: str) -> dict[str, str]:
    home = _trusted_home()
    return {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.pathsep.join((str(Path(executable).parent), "/usr/bin", "/bin")),
    }


def _clean_environment_launcher() -> str:
    try:
        descriptor = os.open(CLEAN_ENV_LAUNCHER, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as launcher:
            launcher_stat = os.fstat(launcher.fileno())
            payload = launcher.read(CLEAN_ENV_LAUNCHER_SIZE + 1)
    except OSError as exc:
        raise CodexReviewError("clean environment launcher is unavailable") from exc
    if (
        not stat.S_ISREG(launcher_stat.st_mode)
        or len(payload) != CLEAN_ENV_LAUNCHER_SIZE
        or hashlib.sha256(payload).hexdigest() != CLEAN_ENV_LAUNCHER_SHA256
    ):
        raise CodexReviewError("clean environment launcher does not match its pin")
    return str(CLEAN_ENV_LAUNCHER.resolve(strict=True))


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
        command = _absolute_command(command)
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
        stop_workers = threading.Event()
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
                descriptor = stream.fileno()
                os.set_blocking(descriptor, False)
                while not stop_workers.is_set():
                    try:
                        chunk = os.read(descriptor, 65_536)
                    except BlockingIOError:
                        stop_workers.wait(0.01)
                        continue
                    if not chunk:
                        return
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
                descriptor = process.stdin.fileno()
                os.set_blocking(descriptor, False)
                pending = memoryview(input_bytes)
                while pending and not stop_workers.is_set():
                    try:
                        pending = pending[os.write(descriptor, pending) :]
                    except BlockingIOError:
                        stop_workers.wait(0.01)
            except BrokenPipeError:
                pass
            except Exception as exc:
                thread_errors.append(exc)
                kill_process_group()
            finally:
                process.stdin.close()

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
        reap_error: subprocess.TimeoutExpired | None = None
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_process_group()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                reap_error = exc
        stream_deadline = time.monotonic() + 5
        for thread in threads:
            thread.join(timeout=max(0.0, stream_deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            kill_process_group()
            stop_workers.set()
            stream_deadline = time.monotonic() + 5
            for thread in threads:
                thread.join(timeout=max(0.0, stream_deadline - time.monotonic()))
        workers_alive = any(thread.is_alive() for thread in threads)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except Exception:
                pass
        if workers_alive:
            raise CodexReviewError("local Codex stream worker did not terminate")
        if reap_error is not None:
            raise CodexReviewError("local Codex process could not be reaped") from reap_error
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
        command = _absolute_command(command)
        unit = self._unit_name_factory()
        if not unit.startswith("td-codex-review-") or not unit.removeprefix(
            "td-codex-review-"
        ).isalnum():
            raise CodexReviewError("invalid transient review unit name")
        service_environment = _minimal_codex_environment(command[0])
        clean_launcher = _clean_environment_launcher()
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
            "--property=MemoryMax=2G",
            "--property=MemorySwapMax=0",
            "--property=CPUQuota=200%",
            "--property=NoNewPrivileges=yes",
            "--property=ProtectControlGroups=yes",
            "--property=PrivatePIDs=yes",
            f"--property=InaccessiblePaths=/run/user/{os.getuid()}",
            "--property=UMask=0077",
            clean_launcher,
            *(f"{key}={value}" for key, value in sorted(service_environment.items())),
            "--",
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


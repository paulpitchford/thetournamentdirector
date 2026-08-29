"""Pinned Codex runtime and bounded systemd-cgroup execution."""

from __future__ import annotations

import hashlib
import os
import pwd
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .review_contract import CodexReviewError

CGROUP_ROOT = Path("/sys/fs/cgroup")
CLEAN_ENV_LAUNCHER = Path(__file__).parent.parent / "bin" / "td-clean-env"
CLEAN_ENV_LAUNCHER_SHA256 = "346657cf479acf47f38ba3b68982d3b838a750b5ab224ddc02c17f430e1aa621"
CLEAN_ENV_LAUNCHER_SIZE = 4_392
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


def _absolute_command(command: list[str]) -> list[str]:
    if not command or not Path(command[0]).is_absolute():
        raise CodexReviewError("review executable path must be absolute")
    return [str(Path(command[0]).resolve(strict=False)), *command[1:]]


def _systemd_property_path(path: Path, *, require_exists: bool = True) -> Path:
    safe_characters = frozenset(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._/+:-"
    )
    if not isinstance(path, Path) or not path.is_absolute():
        raise CodexReviewError("systemd containment path is invalid")
    try:
        resolved = path.resolve(strict=require_exists)
    except OSError as exc:
        raise CodexReviewError("systemd containment path is invalid") from exc
    if any(character not in safe_characters for character in str(resolved)):
        raise CodexReviewError("systemd containment path is invalid")
    return resolved


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


def _verify_staged_file(
    path: Path, expected_sha256: str, expected_size: int
) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as staged_file:
            file_stat = os.fstat(staged_file.fileno())
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size != expected_size:
                raise CodexReviewError("staged Codex runtime size changed")
            digest = hashlib.sha256()
            copied = 0
            while chunk := staged_file.read(min(1024 * 1024, expected_size - copied)):
                copied += len(chunk)
                digest.update(chunk)
            if staged_file.read(1) or copied != expected_size:
                raise CodexReviewError("staged Codex runtime size changed")
            if digest.hexdigest() != expected_sha256:
                raise CodexReviewError("staged Codex runtime hash changed")
    except CodexReviewError:
        raise
    except OSError as exc:
        raise CodexReviewError("staged Codex runtime verification failed") from exc


def _clean_environment_launcher_payload() -> bytes:
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
    return payload


def _clean_environment_launcher() -> str:
    _clean_environment_launcher_payload()
    return str(CLEAN_ENV_LAUNCHER.resolve(strict=True))


def _stage_clean_environment_launcher() -> Path:
    try:
        staging_root = Path(
            tempfile.mkdtemp(prefix="td-controller-launcher-", dir="/var/tmp")
        )
    except OSError as exc:
        raise CodexReviewError("clean environment launcher staging failed") from exc
    destination = staging_root / "td-clean-env"
    try:
        with destination.open("xb") as staged:
            staged.write(_clean_environment_launcher_payload())
            staged.flush()
            os.fsync(staged.fileno())
        destination.chmod(0o500)
        staging_root.chmod(0o500)
        _verify_clean_environment_launcher(destination)
    except CodexReviewError:
        _remove_launcher_staging(staging_root)
        raise
    except OSError as exc:
        _remove_launcher_staging(staging_root)
        raise CodexReviewError("clean environment launcher staging failed") from exc
    return destination


def _remove_launcher_staging(staging_root: Path) -> None:
    try:
        staging_root.chmod(0o700)
    except OSError:
        pass
    shutil.rmtree(staging_root, ignore_errors=True)


def _verify_clean_environment_launcher(path: Path) -> None:
    try:
        root_stat = path.parent.stat(follow_symlinks=False)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as launcher:
            launcher_stat = os.fstat(launcher.fileno())
            payload = launcher.read(CLEAN_ENV_LAUNCHER_SIZE + 1)
    except OSError as exc:
        raise CodexReviewError("staged clean environment launcher is invalid") from exc
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != os.getuid()
        or stat.S_IMODE(root_stat.st_mode) != 0o500
        or not stat.S_ISREG(launcher_stat.st_mode)
        or launcher_stat.st_uid != os.getuid()
        or stat.S_IMODE(launcher_stat.st_mode) != 0o500
        or len(payload) != CLEAN_ENV_LAUNCHER_SIZE
        or hashlib.sha256(payload).hexdigest() != CLEAN_ENV_LAUNCHER_SHA256
    ):
        raise CodexReviewError("staged clean environment launcher is invalid")


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


def _attest_codex_runtime(
    destination_dir: Path, executor: CommandExecutor | None = None
) -> str:
    destination_dir = destination_dir.resolve(strict=False)
    if destination_dir.exists():
        raise CodexReviewError("Codex runtime destination already exists")
    source, host_source = _pinned_runtime_sources()
    temporary = destination_dir.with_name(
        f".{destination_dir.name}.{secrets.token_hex(8)}.partial"
    )
    try:
        temporary.mkdir(mode=0o700)
        staged = temporary / "codex"
        _stage_file(source, staged, PINNED_CODEX_SHA256, PINNED_CODEX_SIZE)
        _stage_file(
            host_source,
            temporary / "codex-code-mode-host",
            PINNED_CODE_MODE_HOST_SHA256,
            PINNED_CODE_MODE_HOST_SIZE,
        )
        boundary = executor or SystemdCgroupExecutor()
        version = boundary.run(
            [str(staged), "--version"],
            input_bytes=b"",
            cwd=temporary.parent,
            timeout_seconds=10,
        )
        if (
            version.returncode != 0
            or version.stderr
            or version.stdout.decode(errors="replace").strip() != PINNED_CODEX_VERSION
        ):
            raise CodexReviewError("Codex runtime version does not match the reviewed pin")
        _verify_staged_file(staged, PINNED_CODEX_SHA256, PINNED_CODEX_SIZE)
        _verify_staged_file(
            temporary / "codex-code-mode-host",
            PINNED_CODE_MODE_HOST_SHA256,
            PINNED_CODE_MODE_HOST_SIZE,
        )
        os.rename(temporary, destination_dir)
    except CodexReviewError:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        raise CodexReviewError("Codex runtime attestation failed") from exc
    return str(destination_dir / "codex")


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
        inaccessible_paths: tuple[Path, ...] = (),
    ) -> None:
        self._delegate = delegate or SubprocessExecutor(
            environment_factory=lambda _: _minimal_systemd_environment()
        )
        self._unit_name_factory = unit_name_factory or (
            lambda: f"td-codex-review-{secrets.token_hex(8)}"
        )
        try:
            self._inaccessible_paths = tuple(
                _systemd_property_path(path) for path in inaccessible_paths
            )
        except CodexReviewError as exc:
            raise CodexReviewError("inaccessible containment path is invalid") from exc

    def run(
        self,
        command: list[str],
        *,
        input_bytes: bytes,
        cwd: Path,
        timeout_seconds: int,
    ) -> ProcessOutput:
        command = _absolute_command(command)
        cwd = _systemd_property_path(cwd)
        _systemd_property_path(Path(command[0]).parent, require_exists=False)
        unit = self._unit_name_factory()
        if not unit.startswith("td-codex-review-") or not unit.removeprefix(
            "td-codex-review-"
        ).isalnum():
            raise CodexReviewError("invalid transient review unit name")
        service_environment = _minimal_codex_environment(command[0])
        clean_launcher = _stage_clean_environment_launcher()
        launcher_root = _systemd_property_path(clean_launcher.parent)
        overlaps = (cwd, *self._inaccessible_paths)
        if any(
            launcher_root.is_relative_to(path) or path.is_relative_to(launcher_root)
            for path in overlaps
        ):
            _remove_launcher_staging(launcher_root)
            raise CodexReviewError("launcher staging overlaps a containment path")
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
            "--property=ProtectSystem=strict",
            "--property=TemporaryFileSystem=/tmp:rw",
            f"--property=ReadWritePaths={cwd}",
            "--property=PrivatePIDs=yes",
            "--property=PrivateUsers=yes",
            f"--property=InaccessiblePaths=/run/user/{os.getuid()}",
            *(
                f"--property=InaccessiblePaths={path}"
                for path in self._inaccessible_paths
            ),
            f"--property=ReadOnlyPaths={launcher_root}",
            f"--property=ReadOnlyPaths={Path(command[0]).parent}",
            "--property=UMask=0077",
            str(clean_launcher),
            *(f"{key}={value}" for key, value in sorted(service_environment.items())),
            "--",
            *command,
        ]
        try:
            _verify_clean_environment_launcher(clean_launcher)
            return self._delegate.run(
                wrapped,
                input_bytes=input_bytes,
                cwd=cwd,
                timeout_seconds=timeout_seconds + 10,
            )
        finally:
            try:
                self._kill_transient_unit(unit)
            finally:
                _remove_launcher_staging(clean_launcher.parent)

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
    boundary = executor or SystemdCgroupExecutor()
    executable = _attest_codex_runtime(destination_dir, executor=boundary)
    return boundary.run(
        [executable, *arguments], input_bytes=input_bytes, cwd=cwd,
        timeout_seconds=timeout_seconds,
    )

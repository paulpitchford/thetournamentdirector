"""Tests for bounded and cgroup-contained Codex execution."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from td_controller.review_contract import CodexReviewError
from td_controller.review_runtime import (
    ProcessOutput,
    SubprocessExecutor,
    SystemdCgroupExecutor,
    _clean_environment_launcher,
    _minimal_codex_environment,
    _minimal_systemd_environment,
)

class FakeExecutor:
    def __init__(self, output: ProcessOutput) -> None:
        self.output = output
        self.commands: list[list[str]] = []

    def run(
        self,
        command: list[str],
        *,
        input_bytes: bytes,
        cwd: Path,
        timeout_seconds: int,
    ) -> ProcessOutput:
        """Record safe invocation fields and return configured output."""
        self.commands.append(command)
        self.input_bytes = input_bytes
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        return self.output


def unit_state(
    *, active: str = "inactive", sub: str = "dead", control_group: str = ""
) -> subprocess.CompletedProcess[bytes]:
    stdout = (
        f"LoadState=loaded\nActiveState={active}\nSubState={sub}\n"
        f"ControlGroup={control_group}\n"
    ).encode()
    return subprocess.CompletedProcess([], returncode=0, stdout=stdout)


class SubprocessExecutorTests(unittest.TestCase):
    """Prove output floods, environment leaks, and timeouts fail closed."""

    def test_ambient_home_cannot_change_allowlisted_environments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            account = Mock(pw_dir=temporary)
            with (
                patch.dict(os.environ, {"HOME": "/attacker-controlled"}),
                patch("td_controller.review_runtime.pwd.getpwuid", return_value=account),
            ):
                parent = _minimal_codex_environment("/runtime/codex")
                service = _minimal_systemd_environment()

        self.assertEqual(parent["HOME"], temporary)
        self.assertEqual(service["HOME"], temporary)

    def test_relative_executables_are_rejected_before_dispatch(self) -> None:
        subprocess_executor = SubprocessExecutor()
        delegate = FakeExecutor(ProcessOutput(0, b"", b""))
        systemd_executor = SystemdCgroupExecutor(delegate=delegate)
        with patch("td_controller.review_runtime.subprocess.Popen") as popen:
            for executor in (subprocess_executor, systemd_executor):
                with self.subTest(executor=type(executor).__name__):
                    with self.assertRaisesRegex(CodexReviewError, "must be absolute"):
                        executor.run(
                            ["codex"], input_bytes=b"", cwd=Path("/tmp"),
                            timeout_seconds=1,
                        )
        popen.assert_not_called()
        self.assertEqual(delegate.commands, [])

    def test_input_limit_is_enforced_before_dispatch(self) -> None:
        executor = SubprocessExecutor()
        with patch("td_controller.review_runtime.subprocess.Popen") as popen:
            with patch("td_controller.review_runtime.MAX_INPUT_BYTES", 1):
                with self.assertRaisesRegex(CodexReviewError, "input limit"):
                    executor.run(
                        ["/pinned/codex"],
                        input_bytes=b"xx",
                        cwd=Path("/tmp"),
                        timeout_seconds=1,
                    )
        popen.assert_not_called()

    def test_input_at_limit_is_accepted(self) -> None:
        executor = SubprocessExecutor()
        with tempfile.TemporaryDirectory() as temporary:
            with patch("td_controller.review_runtime.MAX_INPUT_BYTES", 4):
                output = executor.run(
                    [sys.executable, "-c", "import sys; print(len(sys.stdin.buffer.read()))"],
                    input_bytes=b"1234",
                    cwd=Path(temporary),
                    timeout_seconds=5,
                )
        self.assertEqual(output.stdout.strip(), b"4")

    def test_child_receives_only_minimal_environment(self) -> None:
        executor = SubprocessExecutor()
        script = (
            "import json, os; "
            "print(json.dumps({'sentinel': os.getenv('TD_SECRET_SENTINEL'), "
            "'home': bool(os.getenv('HOME')), 'path': bool(os.getenv('PATH'))}))"
        )
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"TD_SECRET_SENTINEL": "must-not-leak"}):
                output = executor.run(
                    [sys.executable, "-c", script],
                    input_bytes=b"",
                    cwd=Path(temporary),
                    timeout_seconds=5,
                )
        reported = json.loads(output.stdout)
        self.assertIsNone(reported["sentinel"])
        self.assertTrue(reported["home"])
        self.assertTrue(reported["path"])

    def test_pipe_holding_descendant_cannot_hang_cleanup(self) -> None:
        executor = SubprocessExecutor()
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "child.pid"
            child = "import time; time.sleep(30)"
            parent = (
                "import os, pathlib, subprocess, sys; "
                f"p=subprocess.Popen([sys.executable, '-c', {child!r}], "
                "start_new_session=True, stdout=sys.stdout, stderr=sys.stderr); "
                f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); os._exit(0)"
            )
            started = time.monotonic()
            try:
                output = executor.run(
                    [sys.executable, "-c", parent],
                    input_bytes=b"",
                    cwd=Path(temporary),
                    timeout_seconds=2,
                )
                self.assertEqual(output.returncode, 0)
                self.assertLess(time.monotonic() - started, 7)
            finally:
                if pid_file.exists():
                    try:
                        os.kill(int(pid_file.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    time.sleep(0.1)

    def test_combined_output_flood_uses_one_aggregate_limit(self) -> None:
        executor = SubprocessExecutor()
        script = "import os; os.write(1, b'x' * 600); os.write(2, b'y' * 600)"
        with tempfile.TemporaryDirectory() as temporary:
            with patch("td_controller.review_runtime.MAX_OUTPUT_BYTES", 1_024):
                with self.assertRaisesRegex(CodexReviewError, "output limit"):
                    executor.run(
                        [sys.executable, "-c", script],
                        input_bytes=b"",
                        cwd=Path(temporary),
                        timeout_seconds=5,
                    )

    def test_output_flood_is_killed_at_the_hard_limit(self) -> None:
        executor = SubprocessExecutor()
        with tempfile.TemporaryDirectory() as temporary:
            with patch("td_controller.review_runtime.MAX_OUTPUT_BYTES", 1_024):
                with self.assertRaisesRegex(CodexReviewError, "output limit"):
                    executor.run(
                        [sys.executable, "-c", "import os; os.write(1, b'x' * 2048)"],
                        input_bytes=b"",
                        cwd=Path(temporary),
                        timeout_seconds=5,
                    )

    def test_post_kill_wait_is_bounded(self) -> None:
        process = Mock()
        process.pid = 12345
        process.stdin = io.BytesIO()
        process.stdout = io.BytesIO()
        process.stderr = io.BytesIO()
        process.wait.side_effect = [
            subprocess.TimeoutExpired(["codex"], timeout=1),
            subprocess.TimeoutExpired(["codex"], timeout=5),
        ]
        executor = SubprocessExecutor()
        with (
            patch("td_controller.review_runtime.subprocess.Popen", return_value=process),
            patch("td_controller.review_runtime.os.killpg"),
        ):
            with self.assertRaisesRegex(CodexReviewError, "could not be reaped"):
                executor.run(
                    ["/pinned/codex"],
                    input_bytes=b"",
                    cwd=Path("/tmp"),
                    timeout_seconds=1,
                )
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_timeout_kills_the_process_group(self) -> None:
        executor = SubprocessExecutor()
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(CodexReviewError, "timed out"):
                executor.run(
                    [sys.executable, "-c", "import time; time.sleep(10)"],
                    input_bytes=b"",
                    cwd=Path(temporary),
                    timeout_seconds=0,
                )


class SystemdCgroupExecutorTests(unittest.TestCase):
    def test_wraps_review_in_killable_clean_environment_service(self) -> None:
        delegate = FakeExecutor(ProcessOutput(returncode=0, stdout=b"ok", stderr=b""))
        executor = SystemdCgroupExecutor(
            delegate=delegate,
            unit_name_factory=lambda: "td-codex-review-fixed123",
        )
        with (
            patch.dict(os.environ, {"TD_SECRET_SENTINEL": "must-not-leak"}),
            patch.object(SystemdCgroupExecutor, "_kill_transient_unit") as cleanup,
        ):
            result = executor.run(
                ["/pinned/codex", "exec"],
                input_bytes=b"prompt",
                cwd=Path("/tmp"),
                timeout_seconds=30,
            )

        command = delegate.commands[0]
        self.assertEqual(command[0], "/usr/bin/systemd-run")
        self.assertIn("--property=KillMode=control-group", command)
        self.assertIn("--property=RuntimeMaxSec=30s", command)
        self.assertIn("--property=TasksMax=64", command)
        self.assertIn("--property=MemoryMax=2G", command)
        self.assertIn("--property=MemorySwapMax=0", command)
        self.assertIn("--property=CPUQuota=200%", command)
        self.assertIn("--property=ProtectControlGroups=yes", command)
        self.assertIn("--property=PrivatePIDs=yes", command)
        self.assertIn("--property=PrivateUsers=yes", command)
        self.assertIn(
            f"--property=InaccessiblePaths=/run/user/{os.getuid()}", command
        )
        launcher = str(Path(__file__).parents[1] / "bin" / "td-clean-env")
        self.assertIn(f"--property=ReadOnlyPaths={launcher}", command)
        launcher_index = command.index(launcher)
        self.assertEqual(command[launcher_index + 5], "--")
        self.assertFalse(any("TD_SECRET_SENTINEL" in item for item in command))
        self.assertEqual(command[-2:], ["/pinned/codex", "exec"])
        self.assertEqual(delegate.timeout_seconds, 40)
        cleanup.assert_called_once_with("td-codex-review-fixed123")
        self.assertEqual(result.stdout, b"ok")

    def test_modified_clean_environment_launcher_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            launcher = Path(temporary) / "launcher"
            launcher.write_bytes(b"modified")
            with patch("td_controller.review_runtime.CLEAN_ENV_LAUNCHER", launcher):
                with self.assertRaisesRegex(CodexReviewError, "does not match"):
                    _clean_environment_launcher()

    def test_cleanup_rejects_unit_that_remains_active(self) -> None:
        completed = [
            subprocess.CompletedProcess([], returncode=0),
            subprocess.CompletedProcess([], returncode=0),
            unit_state(active="active", sub="running"),
        ]
        with patch("td_controller.review_runtime.subprocess.run", side_effect=completed):
            with self.assertRaisesRegex(CodexReviewError, "remained active"):
                SystemdCgroupExecutor._kill_transient_unit("td-codex-review-fixed123")

    def test_cleanup_attempts_kill_after_stop_timeout(self) -> None:
        results = [
            subprocess.TimeoutExpired(["systemctl", "stop"], timeout=5),
            subprocess.CompletedProcess([], returncode=0),
            unit_state(),
            subprocess.CompletedProcess([], returncode=0),
        ]
        with patch("td_controller.review_runtime.subprocess.run", side_effect=results) as run:
            SystemdCgroupExecutor._kill_transient_unit("td-codex-review-fixed123")

        self.assertEqual(run.call_count, 4)
        self.assertIn("kill", run.call_args_list[1].args[0])

    def test_cleanup_rejects_manager_transport_failure(self) -> None:
        completed = [
            subprocess.CompletedProcess([], returncode=1),
            subprocess.CompletedProcess([], returncode=1),
            subprocess.CompletedProcess([], returncode=1, stdout=b""),
        ]
        with patch("td_controller.review_runtime.subprocess.run", side_effect=completed):
            with self.assertRaisesRegex(CodexReviewError, "could not verify"):
                SystemdCgroupExecutor._kill_transient_unit("td-codex-review-fixed123")

    def test_cleanup_rejects_transitional_state(self) -> None:
        completed = [
            subprocess.CompletedProcess([], returncode=0),
            subprocess.CompletedProcess([], returncode=0),
            unit_state(active="deactivating", sub="stop-sigterm"),
        ]
        with patch("td_controller.review_runtime.subprocess.run", side_effect=completed):
            with self.assertRaisesRegex(CodexReviewError, "remained active"):
                SystemdCgroupExecutor._kill_transient_unit("td-codex-review-fixed123")

    def test_cleanup_rejects_populated_cgroup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cgroup = root / "review"
            cgroup.mkdir()
            (cgroup / "cgroup.events").write_text("populated 1\n")
            with (
                patch("td_controller.review_runtime.CGROUP_ROOT", root),
                patch(
                    "td_controller.review_runtime.subprocess.run",
                    return_value=unit_state(control_group="/review"),
                ),
            ):
                with self.assertRaisesRegex(CodexReviewError, "remained populated"):
                    SystemdCgroupExecutor._verify_transient_unit(
                        "td-codex-review-fixed123", {}
                    )

    def test_invalid_unit_name_is_rejected_before_dispatch(self) -> None:
        delegate = FakeExecutor(ProcessOutput(returncode=0, stdout=b"", stderr=b""))
        executor = SystemdCgroupExecutor(
            delegate=delegate,
            unit_name_factory=lambda: "other-unit",
        )

        with self.assertRaisesRegex(CodexReviewError, "invalid transient"):
            executor.run(
                ["/pinned/codex"],
                input_bytes=b"",
                cwd=Path("/tmp"),
                timeout_seconds=1,
            )
        self.assertEqual(delegate.commands, [])


from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from td_controller.attested_payload_runner import (
    LAUNCHER_SCRIPT,
    AttestedPayloadRunner,
    AttestedPayloadRunnerError,
    PayloadCommand,
)
from td_controller.podman_mount_policy import IMAGE, MountPolicyFixture
from td_controller.review_runtime import ProcessOutput
from td_controller.workspace_identity_handle import (
    WorkspaceIdentityHandle,
    WorkspaceIdentityHandleError,
)


class FakeExecutor:
    def __init__(self, outputs: list[ProcessOutput]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[list[str], bytes]] = []

    def run(
        self,
        command: list[str],
        *,
        input_bytes: bytes,
        cwd: Path,
        timeout_seconds: int,
    ) -> ProcessOutput:
        self.calls.append((command, input_bytes))
        return self.outputs.pop(0)


class AttestedPayloadRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="td-mount-policy-"
        )
        self.root = Path(self.temporary.name)
        self.task = self.root / "task"
        self.sibling = self.root / "sibling"
        self.task.mkdir(mode=0o700)
        self.sibling.mkdir(mode=0o700)
        descriptor = os.open(
            self.task, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            self.handle = WorkspaceIdentityHandle(
                "ORCH-003D1B0D", attempt=1, generation="0" * 32,
                descriptor=descriptor,
            )
        finally:
            os.close(descriptor)
        self.fixture = MountPolicyFixture(self.root, self.task, self.sibling)

    def tearDown(self) -> None:
        self.handle.close()
        self.temporary.cleanup()

    def test_command_preserves_payload_argv_after_all_checks(self) -> None:
        executor = FakeExecutor([])
        runner = AttestedPayloadRunner(executor=executor)
        payload = PayloadCommand(
            "/bin/busybox",
            ("printf", "%s\n", "$(touch /workspace/not-run)"),
        )
        command = runner._command(
            self.fixture, self.handle.identity, payload
        )
        image_index = command.index(IMAGE)
        self.assertEqual(
            command[image_index:],
            (
                IMAGE,
                "/bin/sh",
                "-eu",
                "-c",
                LAUNCHER_SCRIPT,
                "td-workspace-launcher",
                "/bin/busybox",
                "printf",
                "%s\n",
                "$(touch /workspace/not-run)",
            ),
        )
        mount_check = LAUNCHER_SCRIPT.index("/proc/self/mountinfo")
        identity_check = LAUNCHER_SCRIPT.index("EXPECTED_WORKSPACE_IDENTITY")
        exec_index = LAUNCHER_SCRIPT.index('exec "$@"')
        self.assertLess(mount_check, identity_check)
        self.assertLess(identity_check, exec_index)

    def test_run_holds_identity_and_passes_stdin_to_bounded_executor(self) -> None:
        class CloseCheckingExecutor(FakeExecutor):
            def __init__(self, outputs, handle):
                super().__init__(outputs)
                self.handle = handle
                self.close_was_blocked = False

            def run(self, *args, **kwargs):
                if len(self.calls) == 1:
                    try:
                        self.handle.close()
                    except WorkspaceIdentityHandleError:
                        self.close_was_blocked = True
                return super().run(*args, **kwargs)

        executor = CloseCheckingExecutor(
            [ProcessOutput(0, b"true\n", b""), ProcessOutput(0, b"ok", b"")],
            self.handle,
        )
        output = AttestedPayloadRunner(executor=executor).run(
            self.fixture,
            self.handle,
            PayloadCommand("/bin/cat"),
            input_bytes=b"input",
        )
        self.assertEqual(output, ProcessOutput(0, b"ok", b""))
        self.assertEqual(executor.calls[0][1], b"")
        self.assertEqual(executor.calls[1][1], b"input")
        self.assertTrue(executor.close_was_blocked)
        self.handle.verify()

    def test_replacement_inode_rejects_before_payload_output(self) -> None:
        held = self.root / "held"
        self.task.rename(held)
        self.task.mkdir(mode=0o700)
        executor = FakeExecutor(
            [ProcessOutput(0, b"true\n", b""), ProcessOutput(43, b"", b"")]
        )
        output = AttestedPayloadRunner(executor=executor).run(
            self.fixture, self.handle, PayloadCommand("/bin/printf", ("bad",))
        )
        self.assertEqual(output, ProcessOutput(43, b"", b""))
        self.assertEqual(tuple(self.task.iterdir()), ())

    def test_invalid_payload_and_closed_handle_fail_before_payload(self) -> None:
        invalid = (
            object(),
            PayloadCommand("relative"),
            PayloadCommand("/sbin/tool"),
            PayloadCommand("/bin/tool", ("x",) * 65),
            PayloadCommand("/bin/tool", ("é",)),
        )
        for payload in invalid:
            executor = FakeExecutor([])
            with self.assertRaises(AttestedPayloadRunnerError):
                AttestedPayloadRunner(executor=executor).run(
                    self.fixture, self.handle, payload
                )
            self.assertEqual(executor.calls, [])
        self.handle.close()
        executor = FakeExecutor([])
        with self.assertRaisesRegex(AttestedPayloadRunnerError, "unavailable"):
            AttestedPayloadRunner(executor=executor).run(
                self.fixture, self.handle, PayloadCommand("/bin/true")
            )
        self.assertEqual(executor.calls, [])

    def test_rootless_failure_prevents_payload_dispatch(self) -> None:
        executor = FakeExecutor([ProcessOutput(1, b"false\n", b"")])
        with self.assertRaisesRegex(AttestedPayloadRunnerError, "rootless"):
            AttestedPayloadRunner(executor=executor).run(
                self.fixture, self.handle, PayloadCommand("/bin/true")
            )
        self.assertEqual(len(executor.calls), 1)

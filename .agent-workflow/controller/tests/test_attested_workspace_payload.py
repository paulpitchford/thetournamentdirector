from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from td_controller.attested_workspace_payload import (
    ATTESTED_PAYLOAD_SCRIPT,
    AttestedWorkspacePayload,
    AttestedWorkspacePayloadError,
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
        self.calls: list[list[str]] = []

    def run(
        self,
        command: list[str],
        *,
        input_bytes: bytes,
        cwd: Path,
        timeout_seconds: int,
    ) -> ProcessOutput:
        self.calls.append(command)
        output = self.outputs.pop(0)
        return output


class AttestedWorkspacePayloadTests(unittest.TestCase):
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
                "ORCH-003D1B0C", attempt=1, generation="0" * 32,
                descriptor=descriptor,
            )
        finally:
            os.close(descriptor)
        self.fixture = MountPolicyFixture(self.root, self.task, self.sibling)

    def tearDown(self) -> None:
        self.handle.close()
        self.temporary.cleanup()

    def test_command_orders_mount_policy_identity_check_and_payload(self) -> None:
        payload = AttestedWorkspacePayload(executor=FakeExecutor([]))
        command = payload._command(self.fixture, self.handle.identity)
        image_index = command.index(IMAGE)
        expected = self.handle.identity
        self.assertEqual(
            command[image_index - 2:image_index],
            (
                "--env",
                f"EXPECTED_WORKSPACE_IDENTITY={expected.device}:{expected.inode}",
            ),
        )
        mount_check = ATTESTED_PAYLOAD_SCRIPT.index("/proc/self/mountinfo")
        identity_check = ATTESTED_PAYLOAD_SCRIPT.index(
            'test "$actual" != "$EXPECTED_WORKSPACE_IDENTITY"'
        )
        payload_output = ATTESTED_PAYLOAD_SCRIPT.index(
            "printf 'attested-payload-ok"
        )
        self.assertLess(mount_check, identity_check)
        self.assertLess(identity_check, payload_output)

    def test_positive_payload_runs_under_live_handle_hold(self) -> None:
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
            [
                ProcessOutput(0, b"true\n", b""),
                ProcessOutput(0, b"attested-payload-ok\n", b""),
            ],
            self.handle,
        )
        AttestedWorkspacePayload(executor=executor).run(
            self.fixture, self.handle
        )
        self.assertTrue(executor.close_was_blocked)
        self.handle.verify()

    def test_replacement_is_rejected_before_payload_output(self) -> None:
        held = self.root / "held"
        self.task.rename(held)
        self.task.mkdir(mode=0o700)
        payload = AttestedWorkspacePayload(
            executor=FakeExecutor(
                [ProcessOutput(0, b"true\n", b""), ProcessOutput(43, b"", b"")]
            )
        )
        payload.verify_rejected(self.fixture, self.handle, expected_exit=43)
        self.assertEqual(tuple(self.task.iterdir()), ())

    def test_closed_or_invalid_handle_fails_before_payload(self) -> None:
        executor = FakeExecutor([])
        payload = AttestedWorkspacePayload(executor=executor)
        self.handle.close()
        with self.assertRaisesRegex(AttestedWorkspacePayloadError, "unavailable"):
            payload.run(self.fixture, self.handle)
        with self.assertRaisesRegex(AttestedWorkspacePayloadError, "invalid"):
            payload.run(self.fixture, object())
        self.assertEqual(executor.calls, [])

    def test_rootless_and_payload_results_are_exact(self) -> None:
        cases = (
            [ProcessOutput(1, b"false\n", b"")],
            [
                ProcessOutput(0, b"true\n", b""),
                ProcessOutput(0, b"wrong\n", b""),
            ],
        )
        for outputs in cases:
            with self.subTest(outputs=outputs):
                with self.assertRaises(AttestedWorkspacePayloadError):
                    AttestedWorkspacePayload(
                        executor=FakeExecutor(list(outputs))
                    ).run(self.fixture, self.handle)

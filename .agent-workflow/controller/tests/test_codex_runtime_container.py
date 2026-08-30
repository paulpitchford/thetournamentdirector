from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from td_controller.codex_runtime_container import (
    CONTAINER_CODEX,
    RUNTIME_LAUNCHER,
    VERIFIED_CODEX,
    CodexRuntimeContainerError,
    CodexRuntimeContainerProbe,
)
from td_controller.podman_mount_policy import IMAGE, MountPolicyFixture
from td_controller.review_contract import CodexReviewError
from td_controller.review_runtime import (
    PINNED_CODEX_VERSION,
    ProcessOutput,
    SubprocessExecutor,
)
from td_controller.workspace_identity_handle import (
    WorkspaceIdentityHandle,
    WorkspaceIdentityHandleError,
)


class FakeExecutor(SubprocessExecutor):
    def __init__(self, outputs: list[ProcessOutput], handle=None) -> None:
        self.outputs = outputs
        self.calls: list[list[str]] = []
        self.handle = handle
        self.close_was_blocked = False

    def run(self, command: list[str], **kwargs) -> ProcessOutput:
        if len(self.calls) == 1 and self.handle is not None:
            try:
                self.handle.close()
            except WorkspaceIdentityHandleError:
                self.close_was_blocked = True
        self.calls.append(command)
        return self.outputs.pop(0)


class CodexRuntimeContainerProbeTests(unittest.TestCase):
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
                "ORCH-003D1B0E", attempt=1, generation="0" * 32,
                descriptor=descriptor,
            )
        finally:
            os.close(descriptor)
        self.fixture = MountPolicyFixture(self.root, self.task, self.sibling)
        self.runtime = self.root / "codex-staged"
        self.runtime.write_bytes(b"runtime")
        self.runtime.chmod(0o700)

    def tearDown(self) -> None:
        self.handle.close()
        self.temporary.cleanup()

    def test_command_mounts_runtime_read_only_and_checks_before_exec(self) -> None:
        probe = CodexRuntimeContainerProbe(executor=FakeExecutor([]))
        command = probe._command(
            self.fixture, self.handle.identity, self.runtime
        )
        mounts = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--mount"
        ]
        self.assertIn(
            f"type=bind,src={self.runtime},dst={CONTAINER_CODEX},ro",
            mounts,
        )
        self.assertIn("HOME=/home/codex", command)
        self.assertIn("/home/codex:rw,nosuid,nodev,size=300m", command)
        self.assertIn("--memory=384m", command)
        self.assertEqual(
            command[2:4], ("--name", "td-codex-runtime-" + "0" * 32)
        )
        self.assertEqual(command[-5], IMAGE)
        copy_index = RUNTIME_LAUNCHER.index(f"cp {CONTAINER_CODEX} {VERIFIED_CODEX}")
        size_check = RUNTIME_LAUNCHER.index("actual_size=")
        digest_check = RUNTIME_LAUNCHER.index("actual_digest=")
        workspace_check = RUNTIME_LAUNCHER.index("actual_workspace=")
        exec_index = RUNTIME_LAUNCHER.index(f"exec {VERIFIED_CODEX}")
        self.assertLess(copy_index, size_check)
        self.assertLess(size_check, digest_check)
        self.assertLess(digest_check, workspace_check)
        self.assertLess(workspace_check, exec_index)

    def test_exact_version_runs_while_identity_handle_is_live(self) -> None:
        executor = FakeExecutor(
            [
                ProcessOutput(0, b"true\n", b""),
                ProcessOutput(0, f"{PINNED_CODEX_VERSION}\n".encode(), b""),
            ],
            self.handle,
        )
        probe = CodexRuntimeContainerProbe(executor=executor)
        with patch.object(probe, "_validate_runtime"):
            probe.run(self.fixture, self.handle, runtime_path=self.runtime)
        self.assertTrue(executor.close_was_blocked)
        self.handle.verify()

    def test_runtime_replacement_is_rejected_and_original_restored(self) -> None:
        executor = FakeExecutor(
            [ProcessOutput(0, b"true\n", b""), ProcessOutput(45, b"", b"")]
        )
        probe = CodexRuntimeContainerProbe(executor=executor)
        with patch.object(probe, "_validate_runtime"):
            probe.prove_replacement_rejected(
                self.fixture, self.handle, self.runtime
            )
        self.assertEqual(self.runtime.read_bytes(), b"runtime")
        self.assertFalse((self.root / "codex-held").exists())

    def test_invalid_runtime_metadata_fails_before_process_start(self) -> None:
        executor = FakeExecutor([])
        probe = CodexRuntimeContainerProbe(executor=executor)
        with self.assertRaisesRegex(CodexRuntimeContainerError, "metadata"):
            probe.run(self.fixture, self.handle, runtime_path=self.runtime)
        self.assertEqual(executor.calls, [])

    def test_closed_handle_and_unbounded_executor_are_rejected(self) -> None:
        with self.assertRaisesRegex(CodexRuntimeContainerError, "bounded"):
            CodexRuntimeContainerProbe(executor=object())
        executor = FakeExecutor([])
        probe = CodexRuntimeContainerProbe(executor=executor)
        self.handle.close()
        with patch.object(probe, "_validate_runtime"):
            with self.assertRaisesRegex(CodexRuntimeContainerError, "unavailable"):
                probe.run(self.fixture, self.handle, runtime_path=self.runtime)
        self.assertEqual(executor.calls, [])

    def test_runtime_process_failure_reaps_named_container(self) -> None:
        class FailingExecutor(SubprocessExecutor):
            def __init__(self):
                self.calls: list[list[str]] = []

            def run(self, command, **kwargs):
                self.calls.append(command)
                if len(self.calls) == 1:
                    return ProcessOutput(0, b"true\n", b"")
                if len(self.calls) == 2:
                    raise CodexReviewError("timeout")
                if len(self.calls) == 3:
                    return ProcessOutput(0, b"container-id\n", b"")
                return ProcessOutput(1, b"", b"")

        executor = FailingExecutor()
        probe = CodexRuntimeContainerProbe(executor=executor)
        with patch.object(probe, "_validate_runtime"):
            with self.assertRaisesRegex(CodexRuntimeContainerError, "process failed"):
                probe.run(self.fixture, self.handle, runtime_path=self.runtime)
        self.assertEqual(executor.calls[2][1:4], ["rm", "-f", "--time=1"])
        self.assertEqual(executor.calls[3][1:3], ["container", "exists"])

    def test_rootless_and_version_results_are_exact(self) -> None:
        cases = (
            [ProcessOutput(1, b"false\n", b"")],
            [
                ProcessOutput(0, b"true\n", b""),
                ProcessOutput(0, b"wrong\n", b""),
            ],
        )
        for outputs in cases:
            with self.subTest(outputs=outputs):
                probe = CodexRuntimeContainerProbe(
                    executor=FakeExecutor(list(outputs))
                )
                with patch.object(probe, "_validate_runtime"):
                    with self.assertRaises(CodexRuntimeContainerError):
                        probe.run(
                            self.fixture, self.handle,
                            runtime_path=self.runtime,
                        )

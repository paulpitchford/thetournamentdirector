from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from td_controller.review_runtime import ProcessOutput
from td_controller.workspace_isolation import (
    IMAGE,
    IsolationPaths,
    WorkspaceIsolationError,
    WorkspaceIsolationProbe,
)


class FakeExecutor:
    def __init__(self, outputs: list[ProcessOutput]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[list[str], Path, int]] = []

    def run(
        self,
        command: list[str],
        *,
        input_bytes: bytes,
        cwd: Path,
        timeout_seconds: int,
    ) -> ProcessOutput:
        self.calls.append((command, cwd, timeout_seconds))
        if len(self.calls) == 2 and self.outputs[-1].returncode == 0:
            (cwd / "task" / "proof.txt").write_bytes(b"contained-worker-write\n")
        return self.outputs.pop(0)


class WorkspaceIsolationProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.task = self.root / "task"
        self.sibling = self.root / "sibling"
        self.task.mkdir(mode=0o700)
        self.sibling.mkdir(mode=0o700)
        self.paths = IsolationPaths(self.root, self.task, self.sibling)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_command_mounts_only_direct_task_with_hardening(self) -> None:
        command = WorkspaceIsolationProbe(executor=FakeExecutor([])).command(
            self.paths
        )
        mount = command[command.index("--mount") + 1]
        self.assertEqual(command[0], "/usr/bin/podman")
        self.assertIn("--network=none", command)
        self.assertIn("--read-only", command)
        self.assertIn("--cap-drop=all", command)
        self.assertIn("--security-opt=no-new-privileges", command)
        self.assertIn("--userns=keep-id", command)
        self.assertIn(
            f"type=bind,src={self.task},dst=/workspace,rw", command
        )
        self.assertNotEqual(mount, f"type=bind,src={self.root},dst=/workspace,rw")
        self.assertNotEqual(
            mount, f"type=bind,src={self.sibling},dst=/workspace,rw"
        )
        self.assertNotIn("--privileged", command)
        self.assertFalse(any(value.startswith("PROBE_") for value in command))
        self.assertEqual(command[-5], IMAGE)

    def test_success_requires_exact_bounded_results_and_host_postconditions(self) -> None:
        executor = FakeExecutor(
            [
                ProcessOutput(0, b"true\n", b""),
                ProcessOutput(0, b"workspace-isolation-ok\n", b""),
            ]
        )
        WorkspaceIsolationProbe(executor=executor).run(self.paths)
        self.assertEqual(len(executor.calls), 2)
        self.assertEqual(executor.calls[0][2], 30)
        self.assertEqual(executor.calls[1][2], 60)
        self.assertEqual(tuple(self.sibling.iterdir()), ())

    def test_rootless_or_worker_failure_is_normalized(self) -> None:
        cases = (
            [ProcessOutput(1, b"false\n", b"")],
            [
                ProcessOutput(0, b"true\n", b""),
                ProcessOutput(0, b"unexpected\n", b""),
            ],
            [
                ProcessOutput(0, b"true\n", b""),
                ProcessOutput(0, b"workspace-isolation-ok\n", b"warning"),
            ],
        )
        for outputs in cases:
            with self.subTest(outputs=outputs):
                executor = FakeExecutor(list(outputs))
                with self.assertRaises(WorkspaceIsolationError):
                    WorkspaceIsolationProbe(executor=executor).run(self.paths)

    def test_missing_marker_and_modified_sibling_fail(self) -> None:
        class NoMarker(FakeExecutor):
            def run(self, *args, **kwargs):
                output = self.outputs.pop(0)
                return output

        def outputs() -> list[ProcessOutput]:
            return [
                ProcessOutput(0, b"true\n", b""),
                ProcessOutput(0, b"workspace-isolation-ok\n", b""),
            ]

        with self.assertRaisesRegex(WorkspaceIsolationError, "write proof"):
            WorkspaceIsolationProbe(executor=NoMarker(outputs())).run(self.paths)
        (self.task / "proof.txt").write_bytes(b"contained-worker-write\n")
        (self.sibling / "changed").write_text("bad")
        with self.assertRaisesRegex(WorkspaceIsolationError, "sibling"):
            WorkspaceIsolationProbe(executor=FakeExecutor(outputs())).run(self.paths)

    def test_noncanonical_nonprivate_and_nondirect_paths_are_rejected(self) -> None:
        probe = WorkspaceIsolationProbe(executor=FakeExecutor([]))
        nested = self.task / "nested"
        nested.mkdir(mode=0o700)
        invalid = IsolationPaths(self.root, nested, self.sibling)
        with self.assertRaisesRegex(WorkspaceIsolationError, "direct child"):
            probe.command(invalid)
        self.task.chmod(0o777)
        with self.assertRaisesRegex(WorkspaceIsolationError, "not private"):
            probe.command(self.paths)
        self.task.chmod(0o700)
        link = self.root / "link"
        link.symlink_to(self.task)
        with self.assertRaisesRegex(WorkspaceIsolationError, "canonical"):
            probe.command(IsolationPaths(self.root, link, self.sibling))
        unsafe = self.root / "unsafe,name"
        unsafe.mkdir(mode=0o700)
        with self.assertRaisesRegex(WorkspaceIsolationError, "syntax"):
            probe.command(IsolationPaths(self.root, unsafe, self.sibling))

    def test_root_host_identity_is_rejected(self) -> None:
        probe = WorkspaceIsolationProbe(executor=FakeExecutor([]))
        with patch("td_controller.workspace_isolation.os.getuid", return_value=0):
            with self.assertRaisesRegex(WorkspaceIsolationError, "root host"):
                probe.command(self.paths)

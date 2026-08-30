from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from td_controller.podman_mount_policy import IMAGE, MountPolicyFixture
from td_controller.review_runtime import ProcessOutput
from td_controller.workspace_identity_handle import WorkspaceIdentity
from td_controller.workspace_mount_attestation import (
    ATTESTED_WORKER_SCRIPT,
    WorkspaceMountAttestation,
    WorkspaceMountAttestationError,
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
        if len(self.calls) == 2 and output.returncode == 0:
            marker = cwd / "task" / ".attested-proof"
            marker.mkdir()
            (marker / "result").write_bytes(b"attested-workspace-write\n")
        return output


class WorkspaceMountAttestationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="td-mount-policy-"
        )
        self.root = Path(self.temporary.name)
        self.task = self.root / "task"
        self.sibling = self.root / "sibling"
        self.task.mkdir(mode=0o700)
        self.sibling.mkdir(mode=0o700)
        metadata = self.task.stat()
        self.identity = WorkspaceIdentity(
            "ORCH-003D1B0B", 1, "0" * 32,
            metadata.st_dev, metadata.st_ino,
        )
        self.fixture = MountPolicyFixture(self.root, self.task, self.sibling)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_command_checks_expected_inode_before_workspace_write(self) -> None:
        probe = WorkspaceMountAttestation(executor=FakeExecutor([]))
        command = probe.command(self.fixture, self.identity)
        image_index = command.index(IMAGE)
        self.assertEqual(
            command[image_index - 2:image_index],
            (
                "--env",
                f"EXPECTED_WORKSPACE_IDENTITY={self.identity.device}:"
                f"{self.identity.inode}",
            ),
        )
        self.assertEqual(command[-1], ATTESTED_WORKER_SCRIPT)
        check_index = ATTESTED_WORKER_SCRIPT.index(
            'test "$actual" != "$EXPECTED_WORKSPACE_IDENTITY"'
        )
        write_index = ATTESTED_WORKER_SCRIPT.index(
            "mkdir /workspace/.attested-proof"
        )
        self.assertLess(check_index, write_index)

    def test_exact_positive_result_and_marker_are_required(self) -> None:
        executor = FakeExecutor(
            [
                ProcessOutput(0, b"true\n", b""),
                ProcessOutput(0, b"workspace-mount-attested\n", b""),
            ]
        )
        WorkspaceMountAttestation(executor=executor).verify(
            self.fixture, self.identity
        )
        self.assertEqual(len(executor.calls), 2)

    def test_replacement_inode_is_rejected_before_write(self) -> None:
        original = self.task
        held = self.root / "held"
        original.rename(held)
        original.mkdir(mode=0o700)
        executor = FakeExecutor(
            [
                ProcessOutput(0, b"true\n", b""),
                ProcessOutput(43, b"", b""),
            ]
        )
        WorkspaceMountAttestation(executor=executor).verify_replacement_rejected(
            self.fixture, self.identity
        )
        self.assertFalse((original / ".attested-proof").exists())

    def test_existing_symlink_and_regular_marker_results_are_rejected(self) -> None:
        probe = WorkspaceMountAttestation(
            executor=FakeExecutor(
                [
                    ProcessOutput(0, b"true\n", b""),
                    ProcessOutput(44, b"", b""),
                ]
            )
        )
        marker = self.task / ".attested-proof"
        sentinel = self.task / "sentinel"
        sentinel.write_text("retain")
        marker.symlink_to("sentinel")
        probe.verify_existing_marker_rejected(self.fixture, self.identity)
        self.assertEqual(sentinel.read_text(), "retain")
        marker.unlink()
        marker.write_text("retain")
        probe = WorkspaceMountAttestation(
            executor=FakeExecutor(
                [
                    ProcessOutput(0, b"true\n", b""),
                    ProcessOutput(44, b"", b""),
                ]
            )
        )
        probe.verify_existing_marker_rejected(self.fixture, self.identity)
        self.assertEqual(marker.read_text(), "retain")

    def test_unexpected_rejection_or_rootless_result_fails_closed(self) -> None:
        cases = (
            [ProcessOutput(1, b"false\n", b"")],
            [
                ProcessOutput(0, b"true\n", b""),
                ProcessOutput(1, b"", b""),
            ],
            [
                ProcessOutput(0, b"true\n", b""),
                ProcessOutput(43, b"unexpected", b""),
            ],
        )
        for outputs in cases:
            with self.subTest(outputs=outputs):
                probe = WorkspaceMountAttestation(
                    executor=FakeExecutor(list(outputs))
                )
                with self.assertRaises(WorkspaceMountAttestationError):
                    if len(outputs) == 1:
                        probe.verify(self.fixture, self.identity)
                    else:
                        probe.verify_replacement_rejected(
                            self.fixture, self.identity
                        )

    def test_invalid_identity_is_rejected_before_process_start(self) -> None:
        executor = FakeExecutor([])
        probe = WorkspaceMountAttestation(executor=executor)
        with self.assertRaisesRegex(WorkspaceMountAttestationError, "identity"):
            probe.command(self.fixture, object())
        self.assertEqual(executor.calls, [])

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from td_controller.podman_mount_policy import (
    IMAGE,
    MountPolicyFixture,
    PodmanMountPolicyError,
    PodmanMountPolicyProbe,
)
from td_controller.review_runtime import ProcessOutput, SubprocessExecutor


class FakeExecutor:
    def __init__(self, outputs: list[ProcessOutput]) -> None:
        self.outputs = outputs
        self.calls: list[list[str]] = []
        self.cwd: Path | None = None

    def run(
        self,
        command: list[str],
        *,
        input_bytes: bytes,
        cwd: Path,
        timeout_seconds: int,
    ) -> ProcessOutput:
        self.calls.append(command)
        self.cwd = cwd
        if len(self.calls) == 2 and self.outputs[0].returncode == 0:
            (cwd / "task" / "proof.txt").write_bytes(b"mount-policy-write\n")
        return self.outputs.pop(0)


class PodmanMountPolicyProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="td-mount-policy-"
        )
        self.root = Path(self.temporary.name)
        self.task = self.root / "task"
        self.sibling = self.root / "sibling"
        self.task.mkdir(mode=0o700)
        self.sibling.mkdir(mode=0o700)
        self.fixture = MountPolicyFixture(self.root, self.task, self.sibling)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def passing_outputs() -> list[ProcessOutput]:
        return [
            ProcessOutput(0, b"true\n", b""),
            ProcessOutput(0, b"podman-mount-policy-ok\n", b""),
            ProcessOutput(41, b"", b""),
        ]

    def test_environment_ignores_ambient_container_configuration(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "CONTAINERS_CONF": "/host/unsafe.conf",
                "CONTAINERS_CONF_OVERRIDE": "/host/override.conf",
            },
        ):
            environment = PodmanMountPolicyProbe.environment("/usr/bin/podman")
        self.assertEqual(environment["CONTAINERS_CONF"], "/dev/null")
        self.assertNotIn("CONTAINERS_CONF_OVERRIDE", environment)
        self.assertEqual(set(environment), {
            "CONTAINERS_CONF", "HOME", "PATH", "XDG_RUNTIME_DIR"
        })

    def test_default_process_uses_reviewed_bounded_executor(self) -> None:
        probe = PodmanMountPolicyProbe()
        self.assertIsInstance(probe._executor, SubprocessExecutor)

    def test_normal_command_has_only_task_mount_and_fixed_hardening(self) -> None:
        probe = PodmanMountPolicyProbe(executor=FakeExecutor([]))
        command = probe.command(self.fixture)
        mounts = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--mount"
        ]
        self.assertEqual(
            mounts, [f"type=bind,src={self.task},dst=/workspace,rw"]
        )
        self.assertIn("--network=none", command)
        self.assertIn("--read-only", command)
        self.assertIn("--cap-drop=all", command)
        self.assertIn("--security-opt=no-new-privileges", command)
        self.assertIn("--userns=keep-id", command)
        self.assertNotIn("--privileged", command)
        self.assertEqual(command[-5], IMAGE)

    def test_adversarial_command_adds_root_mount_for_negative_proof(self) -> None:
        probe = PodmanMountPolicyProbe(executor=FakeExecutor([]))
        command = probe.command(self.fixture, inject_root=True)
        mounts = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--mount"
        ]
        self.assertEqual(len(mounts), 2)
        self.assertEqual(
            mounts[1], f"type=bind,src={self.root},dst=/unexpected,ro"
        )

    def test_exact_positive_and_negative_results_are_required(self) -> None:
        executor = FakeExecutor(self.passing_outputs())
        PodmanMountPolicyProbe(executor=executor).run(self.fixture)
        self.assertEqual(len(executor.calls), 3)
        self.assertEqual(executor.cwd, self.root)
        self.assertEqual(tuple(self.sibling.iterdir()), ())

    def test_rootless_positive_and_negative_failures_are_normalized(self) -> None:
        cases = (
            [ProcessOutput(1, b"false\n", b"")],
            [
                ProcessOutput(0, b"true\n", b""),
                ProcessOutput(0, b"wrong\n", b""),
                ProcessOutput(41, b"", b""),
            ],
            [
                ProcessOutput(0, b"true\n", b""),
                ProcessOutput(0, b"podman-mount-policy-ok\n", b""),
                ProcessOutput(0, b"podman-mount-policy-ok\n", b""),
            ],
        )
        for outputs in cases:
            with self.subTest(outputs=outputs):
                with self.assertRaises(PodmanMountPolicyError):
                    PodmanMountPolicyProbe(
                        executor=FakeExecutor(list(outputs))
                    ).run(self.fixture)

    def test_nonprivate_nondirect_and_unsafe_fixtures_are_rejected(self) -> None:
        probe = PodmanMountPolicyProbe(executor=FakeExecutor([]))
        nested = self.task / "nested"
        nested.mkdir(mode=0o700)
        with self.assertRaisesRegex(PodmanMountPolicyError, "not direct"):
            probe.command(MountPolicyFixture(self.root, nested, self.sibling))
        self.task.chmod(0o777)
        with self.assertRaisesRegex(PodmanMountPolicyError, "not private"):
            probe.command(self.fixture)
        self.task.chmod(0o700)
        unsafe = self.root / "unsafe,name"
        unsafe.mkdir(mode=0o700)
        with self.assertRaisesRegex(PodmanMountPolicyError, "unsafe"):
            probe.command(MountPolicyFixture(self.root, unsafe, self.sibling))

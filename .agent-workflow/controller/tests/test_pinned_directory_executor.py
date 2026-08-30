from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from td_controller.pinned_directory_executor import (
    PinnedDirectoryExecutor,
    PinnedDirectoryExecutorError,
)

ENVIRONMENT = {"HOME": "/nonexistent", "PATH": "/usr/bin:/bin"}


class PinnedDirectoryExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="td-pinned-executor-")
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o755)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_executor(self) -> PinnedDirectoryExecutor:
        descriptor = os.open(
            self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            return PinnedDirectoryExecutor(descriptor=descriptor)
        finally:
            os.close(descriptor)

    def test_transient_path_replacement_cannot_redirect_command(self) -> None:
        executor = self.make_executor()
        held = self.root.with_name(self.root.name + "-held")
        self.root.rename(held)
        self.root.mkdir(mode=0o755)
        try:
            output = executor.run(
                ["/usr/bin/touch", "descriptor-marker"],
                environment=ENVIRONMENT,
            )
            self.assertEqual(output.returncode, 0)
            self.assertTrue((held / "descriptor-marker").exists())
            self.assertFalse((self.root / "descriptor-marker").exists())
            executor.verify()
        finally:
            executor.close()
            self.root.rmdir()
            held.rename(self.root)

    def test_caller_descriptor_can_close_and_environment_is_exact(self) -> None:
        executor = self.make_executor()
        try:
            output = executor.run(
                ["/usr/bin/env"], environment={"ONLY_VALUE": "exact"}
            )
            self.assertEqual(output, type(output)(0, b"ONLY_VALUE=exact\n", b""))
        finally:
            executor.close()

    def test_close_waits_for_running_descriptor_command(self) -> None:
        executor = self.make_executor()
        started = self.root / "command-started"
        finished = threading.Event()

        def run() -> None:
            executor.run(
                [
                    "/bin/sh", "-c",
                    "touch command-started; sleep 0.15",
                ],
                environment=ENVIRONMENT,
            )

        def close() -> None:
            executor.close()
            finished.set()

        worker = threading.Thread(target=run)
        worker.start()
        deadline = time.monotonic() + 1
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(started.exists())
        closer = threading.Thread(target=close)
        closer.start()
        time.sleep(0.03)
        self.assertFalse(finished.is_set())
        worker.join(timeout=2)
        closer.join(timeout=2)
        self.assertTrue(finished.is_set())

    def test_closed_unsafe_and_non_directory_descriptors_fail(self) -> None:
        executor = self.make_executor()
        executor.close()
        with self.assertRaisesRegex(PinnedDirectoryExecutorError, "closed"):
            executor.verify()
        self.root.chmod(0o777)
        descriptor = os.open(
            self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            with self.assertRaisesRegex(PinnedDirectoryExecutorError, "initialization"):
                PinnedDirectoryExecutor(descriptor=descriptor)
        finally:
            os.close(descriptor)
            self.root.chmod(0o755)
        file_path = self.root / "file"
        file_path.write_text("file", encoding="utf-8")
        descriptor = os.open(file_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            with self.assertRaises(PinnedDirectoryExecutorError):
                PinnedDirectoryExecutor(descriptor=descriptor)
        finally:
            os.close(descriptor)

    def test_invalid_environment_command_and_timeout_fail_closed(self) -> None:
        executor = self.make_executor()
        try:
            invalid_environments = (
                {"lower": "value"}, {"GOOD": "bad\x00value"},
                {"GOOD": "\ud800"}, {"GOOD": "x" * 4097},
            )
            for environment in invalid_environments:
                with self.subTest(environment=environment):
                    with self.assertRaises(PinnedDirectoryExecutorError):
                        executor.run(["/usr/bin/true"], environment=environment)
            for command in ([], ["relative"], ["/usr/bin/true", "é"]):
                with self.assertRaises(PinnedDirectoryExecutorError):
                    executor.run(command, environment=ENVIRONMENT)
            with self.assertRaisesRegex(PinnedDirectoryExecutorError, "process"):
                executor.run(
                    ["/usr/bin/sleep", "2"], environment=ENVIRONMENT,
                    timeout_seconds=1,
                )
        finally:
            executor.close()

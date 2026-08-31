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


class PinnedExecutionHoldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="td-execution-hold-")
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o755)
        descriptor = os.open(
            self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            self.executor = PinnedDirectoryExecutor(descriptor=descriptor)
        finally:
            os.close(descriptor)

    def tearDown(self) -> None:
        self.root.chmod(0o755)
        self.executor.close()
        self.temporary.cleanup()

    def test_hold_serializes_multiple_commands_against_close(self) -> None:
        closed = threading.Event()

        def close() -> None:
            self.executor.close()
            closed.set()

        with self.executor.hold_execution():
            for marker in ("first", "second"):
                output = self.executor.run(
                    ["/usr/bin/touch", marker], environment=ENVIRONMENT
                )
                self.assertEqual(output.returncode, 0)
            closer = threading.Thread(target=close)
            closer.start()
            time.sleep(0.03)
            self.assertFalse(closed.is_set())
        closer.join(timeout=2)
        self.assertTrue(closed.is_set())
        self.assertTrue((self.root / "first").exists())
        self.assertTrue((self.root / "second").exists())

    def test_nested_hold_and_same_thread_close_are_rejected(self) -> None:
        with self.executor.hold_execution():
            with self.assertRaisesRegex(PinnedDirectoryExecutorError, "already"):
                with self.executor.hold_execution():
                    self.fail("nested hold entered")
            with self.assertRaisesRegex(PinnedDirectoryExecutorError, "active"):
                self.executor.close()
        self.executor.verify()

    def test_failed_body_is_preserved_and_executor_is_poisoned(self) -> None:
        body_error = ValueError("caller-owned-body-failure")
        body_error.add_note("caller-owned-note")
        with self.assertRaises(ValueError) as raised:
            with self.executor.hold_execution():
                self.root.chmod(0o777)
                raise body_error
        self.assertIs(raised.exception, body_error)
        self.root.chmod(0o755)
        with self.assertRaisesRegex(
            PinnedDirectoryExecutorError, "poisoned"
        ) as poisoned:
            self.executor.verify()
        self.assertIsNone(poisoned.exception.__cause__)
        self.assertIsNone(poisoned.exception.__context__)

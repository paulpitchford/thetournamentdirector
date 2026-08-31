from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from td_controller.bounded_relative_leaf import (
    BoundedRelativeLeafError,
    read_bounded_regular_at,
)


class BoundedRelativeLeafTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="td-bounded-leaf-")
        self.root = Path(self.temporary.name).resolve()
        self.descriptor = os.open(
            self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )

    def tearDown(self) -> None:
        os.close(self.descriptor)
        self.temporary.cleanup()

    def test_reads_one_stable_safe_regular_leaf(self) -> None:
        path = self.root / "gitdir"
        path.write_bytes(b"/workspace/.git\n")
        leaf = read_bounded_regular_at(
            self.descriptor, "gitdir", expected_uid=os.geteuid()
        )
        self.assertEqual(leaf.payload, b"/workspace/.git\n")
        self.assertEqual(leaf.size, len(leaf.payload))
        self.assertGreater(leaf.ctime_ns, 0)

    def test_fifo_symlink_writable_and_oversized_leaf_fail_promptly(self) -> None:
        fifo = self.root / "fifo"
        os.mkfifo(fifo, mode=0o600)
        started = time.monotonic()
        with self.assertRaises(BoundedRelativeLeafError):
            read_bounded_regular_at(
                self.descriptor, "fifo", expected_uid=os.geteuid()
            )
        self.assertLess(time.monotonic() - started, 0.5)
        target = self.root / "target"
        target.write_bytes(b"target")
        (self.root / "link").symlink_to(target)
        target.chmod(0o666)
        for name in ("link", "target"):
            with self.subTest(name=name):
                with self.assertRaises(BoundedRelativeLeafError):
                    read_bounded_regular_at(
                        self.descriptor, name, expected_uid=os.geteuid()
                    )
        oversized = self.root / "oversized"
        oversized.write_bytes(b"x" * 4097)
        with self.assertRaises(BoundedRelativeLeafError):
            read_bounded_regular_at(
                self.descriptor, "oversized", expected_uid=os.geteuid()
            )

    def test_equal_size_rewrite_with_restored_mtime_is_detected_by_ctime(self) -> None:
        path = self.root / "marker"
        path.write_bytes(b"original")
        original = path.stat()
        real_read = os.read

        def rewrite(descriptor: int, size: int) -> bytes:
            payload = real_read(descriptor, size)
            path.write_bytes(b"changed!")
            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
            return payload

        with patch("td_controller.bounded_relative_leaf.os.read", side_effect=rewrite):
            with self.assertRaises(BoundedRelativeLeafError):
                read_bounded_regular_at(
                    self.descriptor, "marker", expected_uid=os.geteuid()
                )

    def test_invalid_inputs_fail_before_open(self) -> None:
        with patch("td_controller.bounded_relative_leaf.os.open") as opened:
            for name in ("../leaf", "nested/leaf", "", "é"):
                with self.assertRaises(BoundedRelativeLeafError):
                    read_bounded_regular_at(
                        self.descriptor, name, expected_uid=os.geteuid()
                    )
        opened.assert_not_called()

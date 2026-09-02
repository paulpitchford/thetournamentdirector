from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from td_controller.bounded_fd_read import BoundedFdReadError, read_exact_fd


class BoundedFdReadTests(unittest.TestCase):
    def test_real_descriptor_returns_exact_binary_bytes(self) -> None:
        descriptor, path = tempfile.mkstemp(prefix="td-bounded-fd-")
        try:
            payload = b"binary\x00payload\xff"
            os.write(descriptor, payload)
            os.lseek(descriptor, 0, os.SEEK_SET)
            self.assertEqual(read_exact_fd(descriptor, len(payload)), payload)
        finally:
            os.close(descriptor)
            os.unlink(path)

    def test_short_reads_are_accumulated_until_exact_eof(self) -> None:
        chunks = [b"ab", b"c", b"def", b""]
        with patch(
            "td_controller.bounded_fd_read.os.read", side_effect=chunks
        ) as reader:
            self.assertEqual(read_exact_fd(7, 6), b"abcdef")
        self.assertEqual(reader.call_count, 4)

    def test_short_long_failed_and_invalid_reads_fail_closed(self) -> None:
        for chunks, expected_size in (([b"short", b""], 6), ([b"longer"], 5)):
            with self.subTest(chunks=chunks):
                with patch(
                    "td_controller.bounded_fd_read.os.read", side_effect=chunks
                ):
                    with self.assertRaises(BoundedFdReadError):
                        read_exact_fd(7, expected_size)
        with patch(
            "td_controller.bounded_fd_read.os.read",
            side_effect=OSError("secret diagnostic"),
        ):
            with self.assertRaises(BoundedFdReadError) as raised:
                read_exact_fd(7, 1)
        self.assertNotIn("secret diagnostic", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)
        for descriptor, size in ((-1, 0), (-100, 0), (7, -1), (7, 2_000_001)):
            with self.assertRaises(BoundedFdReadError):
                read_exact_fd(descriptor, size)

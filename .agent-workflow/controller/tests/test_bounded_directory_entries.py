from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from td_controller.bounded_directory_entries import (
    BoundedDirectoryEntriesError,
    list_bounded_directory_names,
)


class FakeIterator:
    def __init__(self, names: list[str], *, close_fails: bool = False) -> None:
        self.names = names
        self.index = 0
        self.close_fails = close_fails
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self.index >= len(self.names):
            raise StopIteration
        name = self.names[self.index]
        self.index += 1
        return SimpleNamespace(name=name)

    def close(self) -> None:
        self.closed = True
        if self.close_fails:
            raise OSError("close failure")


class BoundedDirectoryEntriesTests(unittest.TestCase):
    def test_real_descriptor_returns_sorted_names(self) -> None:
        with tempfile.TemporaryDirectory(prefix="td-bounded-directory-") as temporary:
            root = Path(temporary)
            (root / "z.txt").touch()
            (root / "a.txt").touch()
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                self.assertEqual(
                    list_bounded_directory_names(descriptor, max_entries=2),
                    ("a.txt", "z.txt"),
                )
            finally:
                os.close(descriptor)

    def test_non_utf8_maximum_name_round_trips_filesystem_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="td-bounded-directory-") as temporary:
            raw_name = b"a" * 253 + b"\xff"
            raw_path = os.fsencode(temporary) + b"/" + raw_name
            file_fd = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(file_fd)
            descriptor = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
            try:
                names = list_bounded_directory_names(descriptor, max_entries=1)
            finally:
                os.close(descriptor)
            self.assertEqual(tuple(os.fsencode(name) for name in names), (raw_name,))

    def test_lazy_iterator_stops_at_first_excess_entry(self) -> None:
        iterator = FakeIterator(["one", "two", "three", "must-not-read"])
        with patch(
            "td_controller.bounded_directory_entries.os.scandir",
            return_value=iterator,
        ):
            with self.assertRaises(BoundedDirectoryEntriesError):
                list_bounded_directory_names(7, max_entries=2)
        self.assertEqual(iterator.index, 3)
        self.assertTrue(iterator.closed)

    def test_invalid_name_cleanup_failure_and_inputs_fail_closed(self) -> None:
        for iterator in (FakeIterator(["bad/name"]), FakeIterator([], close_fails=True)):
            with self.subTest(iterator=iterator):
                with patch(
                    "td_controller.bounded_directory_entries.os.scandir",
                    return_value=iterator,
                ):
                    with self.assertRaises(BoundedDirectoryEntriesError):
                        list_bounded_directory_names(7, max_entries=1)
                self.assertTrue(iterator.closed)
        for descriptor, maximum in ((-1, 1), (7, -1), (7, 10_002)):
            with self.assertRaises(BoundedDirectoryEntriesError):
                list_bounded_directory_names(descriptor, max_entries=maximum)

"""Lazy bounded descriptor-relative directory name enumeration."""

from __future__ import annotations

import os

MAX_DIRECTORY_ENTRIES = 10_001


class BoundedDirectoryEntriesError(RuntimeError):
    """Raised when directory names cannot be safely bounded."""


def list_bounded_directory_names(
    descriptor: int,
    *,
    max_entries: int,
) -> tuple[str, ...]:
    """Consume at most one entry beyond the bound, then return sorted names."""
    if (
        isinstance(descriptor, bool)
        or not isinstance(descriptor, int)
        or descriptor < 0
        or isinstance(max_entries, bool)
        or not isinstance(max_entries, int)
        or not 0 <= max_entries <= MAX_DIRECTORY_ENTRIES
    ):
        raise BoundedDirectoryEntriesError("directory entry input is invalid")
    names: list[str] = []
    iterator = None
    failed = False
    try:
        iterator = os.scandir(descriptor)
        for directory_entry in iterator:
            name = directory_entry.name
            if (
                not isinstance(name, str)
                or not name
                or name in (".", "..")
                or "/" in name
                or "\x00" in name
                or len(os.fsencode(name)) > 255
            ):
                raise OSError("directory entry name is invalid")
            names.append(name)
            if len(names) > max_entries:
                raise OSError("directory entry bound exceeded")
    except OSError:
        failed = True
    cleanup_failed = False
    if iterator is not None:
        try:
            iterator.close()
        except OSError:
            cleanup_failed = True
    if failed or cleanup_failed or len(names) != len(set(names)):
        raise BoundedDirectoryEntriesError(
            "directory entries could not be confirmed"
        ) from None
    return tuple(sorted(names))

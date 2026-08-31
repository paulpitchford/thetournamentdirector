"""Bounded nonblocking reads of descriptor-relative regular leaf files."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass

LEAF_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")


class BoundedRelativeLeafError(RuntimeError):
    """Raised when a relative leaf cannot be read as one stable safe object."""


@dataclass(frozen=True, slots=True)
class BoundedLeaf:
    payload: bytes
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


def read_bounded_regular_at(
    parent_descriptor: int,
    name: str,
    *,
    expected_uid: int,
    max_bytes: int = 4096,
) -> BoundedLeaf:
    """Read one no-follow regular leaf and bind complete mutation metadata."""
    if (
        isinstance(parent_descriptor, bool)
        or not isinstance(parent_descriptor, int)
        or parent_descriptor < 0
        or not isinstance(name, str)
        or not LEAF_NAME.fullmatch(name)
        or name in (".", "..")
        or isinstance(expected_uid, bool)
        or not isinstance(expected_uid, int)
        or expected_uid < 0
        or isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= 65_536
    ):
        raise BoundedRelativeLeafError("bounded leaf input is invalid")
    descriptor = -1
    failed = False
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_descriptor,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != expected_uid
            or stat.S_IMODE(before.st_mode) & 0o022
            or not 1 <= before.st_size <= max_bytes
        ):
            raise OSError("bounded leaf metadata is unsafe")
        payload = os.read(descriptor, max_bytes + 1)
        after = os.fstat(descriptor)
        version_before = (
            before.st_dev, before.st_ino, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns,
        )
        version_after = (
            after.st_dev, after.st_ino, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns,
        )
        if len(payload) != before.st_size or version_after != version_before:
            raise OSError("bounded leaf changed")
        result = BoundedLeaf(payload, *version_before)
    except OSError:
        failed = True
        result = None
    cleanup_failed = False
    if descriptor >= 0:
        try:
            os.close(descriptor)
        except OSError:
            cleanup_failed = True
    if failed or cleanup_failed or result is None:
        raise BoundedRelativeLeafError("bounded leaf read failed") from None
    return result

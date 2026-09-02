"""Exact short-read-safe bounded reads from an already trusted descriptor."""

from __future__ import annotations

import os

MAX_FD_READ_BYTES = 2_000_000


class BoundedFdReadError(RuntimeError):
    """Raised when a descriptor does not yield exactly the expected bytes."""


def read_exact_fd(
    descriptor: int,
    expected_size: int,
    *,
    max_bytes: int = MAX_FD_READ_BYTES,
) -> bytes:
    """Read until EOF or one byte beyond the exact expected size."""
    if (
        isinstance(descriptor, bool)
        or not isinstance(descriptor, int)
        or descriptor < 0
        or isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size < 0
        or isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= MAX_FD_READ_BYTES
        or expected_size > max_bytes
    ):
        raise BoundedFdReadError("bounded descriptor read input is invalid")
    result = bytearray()
    failed = False
    try:
        while len(result) <= expected_size:
            remaining = expected_size + 1 - len(result)
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            result.extend(chunk)
    except OSError:
        failed = True
    if failed or len(result) != expected_size:
        raise BoundedFdReadError("descriptor did not yield exact bytes") from None
    return bytes(result)

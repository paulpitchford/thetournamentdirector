"""Exact Git blob command and cryptographic payload verification."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .exact_ref_command import GIT
from .git_tree_manifest import GitTreeEntry
from .review_runtime import MAX_OUTPUT_BYTES


class ExactGitBlobError(ValueError):
    """Raised when blob retrieval inputs or bytes are not exact."""


@dataclass(frozen=True, slots=True)
class ExactGitBlobCommand:
    argv: tuple[str, ...]
    environment: Mapping[str, str]
    expected_sha: str


@dataclass(frozen=True, slots=True)
class VerifiedGitBlob:
    payload: bytes
    blob_sha: str
    executable: bool


def build_exact_git_blob_command(entry: GitTreeEntry) -> ExactGitBlobCommand:
    if type(entry) is not GitTreeEntry or not entry.materialize:
        raise ExactGitBlobError("blob entry is not materializable")
    return ExactGitBlobCommand(
        (GIT, "-c", "core.hooksPath=/dev/null", "cat-file", "blob", entry.blob_sha),
        MappingProxyType({
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "HOME": "/nonexistent",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
        }),
        entry.blob_sha,
    )


def verify_exact_git_blob(
    entry: GitTreeEntry, payload: bytes
) -> VerifiedGitBlob:
    if (
        type(entry) is not GitTreeEntry
        or not entry.materialize
        or not isinstance(payload, bytes)
        or len(payload) > MAX_OUTPUT_BYTES
    ):
        raise ExactGitBlobError("blob verification input is invalid")
    header = f"blob {len(payload)}\0".encode("ascii")
    digest = hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()
    if digest != entry.blob_sha:
        raise ExactGitBlobError("blob digest does not match manifest")
    return VerifiedGitBlob(payload, digest, entry.executable)

"""Exact Git tree listing command and strict tracked-file manifest parser."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType

from .exact_ref_command import GIT, SHA

PATH = re.compile(rb"[A-Za-z0-9._/-]{1,512}")
ENTRY = re.compile(rb"(100644|100755) blob ([0-9a-f]{40})\t(.+)")
DENIED_PARTS = frozenset({".git", "downloads", "extracted", "analysis"})
MAX_MANIFEST_BYTES = 2_000_000
MAX_ENTRIES = 10_000


class GitTreeManifestError(ValueError):
    """Raised when a tree command or manifest is not canonical."""


@dataclass(frozen=True, slots=True)
class GitTreeCommand:
    argv: tuple[str, ...]
    environment: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class GitTreeEntry:
    path: PurePosixPath
    blob_sha: str
    executable: bool
    materialize: bool


def build_git_tree_command(commit_sha: str) -> GitTreeCommand:
    if not isinstance(commit_sha, str) or not SHA.fullmatch(commit_sha):
        raise GitTreeManifestError("tree commit is invalid")
    return GitTreeCommand(
        (GIT, "-c", "core.hooksPath=/dev/null", "ls-tree", "-r", "-z",
         "--full-tree", commit_sha),
        MappingProxyType({
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "HOME": "/nonexistent",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
        }),
    )


def parse_git_tree_manifest(payload: bytes) -> tuple[GitTreeEntry, ...]:
    if not isinstance(payload, bytes) or len(payload) > MAX_MANIFEST_BYTES:
        raise GitTreeManifestError("tree manifest size is invalid")
    if payload and not payload.endswith(b"\x00"):
        raise GitTreeManifestError("tree manifest framing is invalid")
    records = payload[:-1].split(b"\x00") if payload else []
    if len(records) > MAX_ENTRIES:
        raise GitTreeManifestError("tree manifest has too many entries")
    result: list[GitTreeEntry] = []
    previous = b""
    seen: set[PurePosixPath] = set()
    for record in records:
        match = ENTRY.fullmatch(record)
        if match is None or not PATH.fullmatch(match.group(3)):
            raise GitTreeManifestError("tree manifest entry is invalid")
        raw_path = match.group(3)
        if previous and raw_path <= previous:
            raise GitTreeManifestError("tree manifest order is invalid")
        previous = raw_path
        path = PurePosixPath(raw_path.decode("ascii"))
        if (
            path.as_posix().encode("ascii") != raw_path
            or not path.parts
            or path.is_absolute()
            or ".." in path.parts
            or ".git" in path.parts
            or any(parent in seen for parent in path.parents)
        ):
            raise GitTreeManifestError("tree manifest path is unsafe")
        seen.add(path)
        result.append(
            GitTreeEntry(
                path, match.group(2).decode("ascii"),
                match.group(1) == b"100755",
                not any(part in DENIED_PARTS for part in path.parts),
            )
        )
    return tuple(result)

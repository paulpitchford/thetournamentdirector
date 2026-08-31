"""Parse Git worktree marker bytes as bounded non-authoritative evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

SAFE_PATH = re.compile(rb"/[A-Za-z0-9._/+:-]{1,4000}")
ADMIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class WorktreeMarkerContractError(ValueError):
    """Raised when marker bytes are not one canonical admin-directory target."""


@dataclass(frozen=True, slots=True)
class WorktreeMarkerTarget:
    """Lexical evidence only; this value grants no filesystem authority."""

    admin_path: PurePosixPath
    admin_name: str


def parse_worktree_marker(payload: bytes) -> WorktreeMarkerTarget:
    """Parse one bounded `gitdir:` line without filesystem lookup."""
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= 4096:
        raise WorktreeMarkerContractError("worktree marker size is invalid")
    if not payload.startswith(b"gitdir: ") or not payload.endswith(b"\n"):
        raise WorktreeMarkerContractError("worktree marker framing is invalid")
    raw_path = payload[8:-1]
    if not SAFE_PATH.fullmatch(raw_path):
        raise WorktreeMarkerContractError("worktree marker path is invalid")
    path = PurePosixPath(raw_path.decode("ascii"))
    if path.as_posix().encode("ascii") != raw_path or ".." in path.parts:
        raise WorktreeMarkerContractError("worktree marker path is not canonical")
    if len(path.parts) < 4 or path.parts[-3:-1] != (".git", "worktrees"):
        raise WorktreeMarkerContractError("worktree marker target is invalid")
    admin_name = path.name
    if not ADMIN_NAME.fullmatch(admin_name):
        raise WorktreeMarkerContractError("worktree marker admin name is invalid")
    return WorktreeMarkerTarget(path, admin_name)

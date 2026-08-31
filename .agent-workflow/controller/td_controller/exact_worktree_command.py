"""Build one hook-disabled no-checkout Git worktree registration command."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .exact_ref_command import GIT, REF

WORKSPACE_DESCRIPTOR_MARKER = "{PINNED_WORKSPACE_DESCRIPTOR}"


class ExactWorktreeCommandError(ValueError):
    """Raised when a worktree command input is not canonical."""


@dataclass(frozen=True, slots=True)
class ExactWorktreeCommand:
    """Immutable argv and environment for one no-checkout registration."""

    argv: tuple[str, ...]
    environment: Mapping[str, str]


def build_exact_worktree_command(ref_name: str) -> ExactWorktreeCommand:
    """Build a command whose descriptor marker must be replaced internally."""
    if not isinstance(ref_name, str) or not REF.fullmatch(ref_name):
        raise ExactWorktreeCommandError("worktree branch ref is invalid")
    branch_name = ref_name.removeprefix("refs/heads/")
    return ExactWorktreeCommand(
        (
            GIT,
            "-c",
            "core.hooksPath=/dev/null",
            "worktree",
            "add",
            "--no-checkout",
            "--quiet",
            "--no-guess-remote",
            WORKSPACE_DESCRIPTOR_MARKER,
            branch_name,
        ),
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

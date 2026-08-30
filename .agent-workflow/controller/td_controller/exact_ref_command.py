"""Build a hook-disabled, non-dereferencing Git ref creation command."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

GIT = "/usr/bin/git"
ZERO_SHA = "0" * 40
REF = re.compile(r"refs/heads/agent-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{32}")
SHA = re.compile(r"[0-9a-f]{40}")


class ExactRefCommandError(ValueError):
    """Raised when exact-ref command inputs are not canonical."""


@dataclass(frozen=True)
class ExactRefCommand:
    """Validated argv and clean environment for one absent exact ref."""

    argv: tuple[str, ...]
    environment: Mapping[str, str]


def build_exact_ref_command(ref_name: str, commit_sha: str) -> ExactRefCommand:
    """Build an atomic zero-old-value transaction without hooks or dereference."""
    if not isinstance(ref_name, str) or not REF.fullmatch(ref_name):
        raise ExactRefCommandError("exact ref name is invalid")
    if not isinstance(commit_sha, str) or not SHA.fullmatch(commit_sha):
        raise ExactRefCommandError("exact ref commit is invalid")
    return ExactRefCommand(
        (
            GIT,
            "-c",
            "core.hooksPath=/dev/null",
            "update-ref",
            "--no-deref",
            "--create-reflog",
            ref_name,
            commit_sha,
            ZERO_SHA,
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

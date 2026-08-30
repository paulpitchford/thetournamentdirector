"""Guard the accepted exact-ref command's hook suppression contract."""

from __future__ import annotations

from .exact_ref_command import (
    GIT,
    ZERO_SHA,
    ExactRefCommand,
    build_exact_ref_command,
)


class ExactRefHookGuardError(RuntimeError):
    """Raised when exact-ref argv could permit repository hooks."""


def guarded_exact_ref_command(ref_name: str, commit_sha: str) -> ExactRefCommand:
    """Require the complete reviewed argv, including command-line hook denial."""
    command = build_exact_ref_command(ref_name, commit_sha)
    expected = (
        GIT,
        "-c",
        "core.hooksPath=/dev/null",
        "update-ref",
        "--no-deref",
        "--create-reflog",
        ref_name,
        commit_sha,
        ZERO_SHA,
    )
    if command.argv != expected:
        raise ExactRefHookGuardError("exact-ref command shape is invalid")
    if dict(command.environment) != {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }:
        raise ExactRefHookGuardError("exact-ref environment shape is invalid")
    return command

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from td_controller.exact_worktree_command import (
    WORKSPACE_DESCRIPTOR_MARKER,
    ExactWorktreeCommandError,
    build_exact_worktree_command,
)

REF_NAME = "refs/heads/agent-orch-test-0123456789abcdef0123456789abcdef"


class ExactWorktreeCommandTests(unittest.TestCase):
    def test_command_is_exact_immutable_and_hook_disabled(self) -> None:
        command = build_exact_worktree_command(REF_NAME)
        self.assertEqual(
            command.argv,
            (
                "/usr/bin/git", "-c", "core.hooksPath=/dev/null",
                "worktree", "add", "--no-checkout", "--quiet",
                "--no-guess-remote", WORKSPACE_DESCRIPTOR_MARKER, REF_NAME,
            ),
        )
        self.assertEqual(
            dict(command.environment),
            {
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "HOME": "/nonexistent",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
            },
        )
        with self.assertRaises(TypeError):
            command.environment["HOME"] = "/tmp"

    def test_invalid_refs_fail_closed(self) -> None:
        for ref_name in (
            "main", "refs/heads/main", "refs/heads/agent-UPPER-" + "a" * 32,
            "refs/heads/agent-task-" + "a" * 31, True,
        ):
            with self.subTest(ref_name=ref_name):
                with self.assertRaises(ExactWorktreeCommandError):
                    build_exact_worktree_command(ref_name)

    def test_real_command_registers_without_checkout_filters_or_hooks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="td-worktree-command-") as temporary:
            parent = Path(temporary).resolve()
            repository = parent / "repository"
            target = parent / "target"
            repository.mkdir(mode=0o755)
            target.mkdir(mode=0o700)
            subprocess.run(
                ["/usr/bin/git", "init", "-q", str(repository)], check=True
            )
            (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            (repository / ".gitattributes").write_text(
                "*.txt filter=evil\n", encoding="utf-8"
            )
            environment = {
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
                "PATH": "/usr/bin:/bin",
            }
            subprocess.run(
                ["/usr/bin/git", "-C", str(repository), "add", "."],
                check=True, env=environment,
            )
            subprocess.run(
                ["/usr/bin/git", "-C", str(repository), "commit", "--no-verify",
                 "-q", "-m", "seed"],
                check=True, env=environment,
            )
            subprocess.run(
                ["/usr/bin/git", "-C", str(repository), "update-ref", REF_NAME,
                 "HEAD", "0" * 40],
                check=True,
            )
            hook_marker = parent / "hook-ran"
            filter_marker = parent / "filter-ran"
            hook = repository / ".git/hooks/post-checkout"
            hook.write_text(
                f"#!/bin/sh\ntouch {hook_marker}\n", encoding="utf-8"
            )
            hook.chmod(0o700)
            subprocess.run(
                ["/usr/bin/git", "-C", str(repository), "config",
                 "filter.evil.smudge", f"touch {filter_marker}; cat"],
                check=True,
            )
            command = build_exact_worktree_command(REF_NAME)
            argv = [str(target) if item == WORKSPACE_DESCRIPTOR_MARKER else item
                    for item in command.argv]
            result = subprocess.run(
                argv, cwd=repository, env=dict(command.environment),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b"")
            self.assertEqual(result.stderr, b"")
            self.assertTrue((target / ".git").is_file())
            self.assertFalse((target / "tracked.txt").exists())
            self.assertFalse(hook_marker.exists())
            self.assertFalse(filter_marker.exists())

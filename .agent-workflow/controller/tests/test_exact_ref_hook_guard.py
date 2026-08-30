from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from td_controller.exact_ref_command import ExactRefCommand
from td_controller.exact_ref_hook_guard import (
    ExactRefHookGuardError,
    guarded_exact_ref_command,
)

REF = "refs/heads/agent-orch-003d1b0k5-" + "a" * 32


class ExactRefHookGuardTests(unittest.TestCase):
    def test_successful_creation_does_not_run_repository_hook(self) -> None:
        with tempfile.TemporaryDirectory(prefix="td-hook-guard-") as temporary:
            root = Path(temporary)
            subprocess.run(["/usr/bin/git", "init", "-q", str(root)], check=True)
            environment = {
                "GIT_AUTHOR_NAME": "TD", "GIT_AUTHOR_EMAIL": "td@example.invalid",
                "GIT_COMMITTER_NAME": "TD", "GIT_COMMITTER_EMAIL": "td@example.invalid",
                "HOME": "/nonexistent", "PATH": "/usr/bin:/bin",
            }
            (root / "file").write_text("base\n", encoding="utf-8")
            subprocess.run(
                ["/usr/bin/git", "add", "file"], cwd=root, check=True,
                env=environment,
            )
            subprocess.run(
                ["/usr/bin/git", "commit", "-q", "-m", "base"], cwd=root,
                check=True, env=environment,
            )
            commit = subprocess.check_output(
                ["/usr/bin/git", "rev-parse", "HEAD"], cwd=root,
                env=environment,
            ).decode().strip()
            hooks = root / "hooks"
            hooks.mkdir()
            marker = root / "hook-ran"
            hook = hooks / "reference-transaction"
            hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
            hook.chmod(0o700)
            subprocess.run(
                ["/usr/bin/git", "config", "core.hooksPath", str(hooks)],
                cwd=root, check=True, env=environment,
            )
            command = guarded_exact_ref_command(REF, commit)

            created = subprocess.run(
                command.argv, cwd=root, env=dict(command.environment),
                check=False, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
            )

            self.assertEqual(created.returncode, 0)
            self.assertEqual(created.stdout + created.stderr, b"")
            self.assertFalse(marker.exists())

    def test_guard_rejects_missing_hook_override(self) -> None:
        altered = ExactRefCommand(
            (
                "/usr/bin/git", "update-ref", "--no-deref",
                "--create-reflog", REF, "1" * 40, "0" * 40,
            ),
            {
                "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_NO_REPLACE_OBJECTS": "1", "HOME": "/nonexistent",
                "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin",
            },
        )
        with patch(
            "td_controller.exact_ref_hook_guard.build_exact_ref_command",
            return_value=altered,
        ):
            with self.assertRaisesRegex(ExactRefHookGuardError, "command"):
                guarded_exact_ref_command(REF, "1" * 40)

    def test_guard_rejects_additional_environment(self) -> None:
        from td_controller.exact_ref_command import build_exact_ref_command

        original = build_exact_ref_command(REF, "1" * 40)
        altered = ExactRefCommand(
            original.argv, dict(original.environment) | {"GIT_DIR": "/tmp/other"}
        )
        with patch(
            "td_controller.exact_ref_hook_guard.build_exact_ref_command",
            return_value=altered,
        ):
            with self.assertRaisesRegex(ExactRefHookGuardError, "environment"):
                guarded_exact_ref_command(REF, "1" * 40)

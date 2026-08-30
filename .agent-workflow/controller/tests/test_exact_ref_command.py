from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from td_controller.exact_ref_command import (
    GIT,
    ExactRefCommandError,
    build_exact_ref_command,
)

REF = "refs/heads/agent-orch-003d1b0k1-" + "a" * 32


class ExactRefCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="td-exact-ref-")
        self.root = Path(self.temporary.name)
        self.environment = dict(
            build_exact_ref_command(REF, "1" * 40).environment
        ) | {
            "GIT_AUTHOR_NAME": "TD", "GIT_AUTHOR_EMAIL": "td@example.invalid",
            "GIT_COMMITTER_NAME": "TD", "GIT_COMMITTER_EMAIL": "td@example.invalid",
        }
        subprocess.run(
            [GIT, "init", "-q", str(self.root)], check=True,
            env=self.environment,
        )
        (self.root / "file.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(
            [GIT, "add", "file.txt"], cwd=self.root, check=True,
            env=self.environment,
        )
        subprocess.run(
            [GIT, "commit", "-q", "-m", "base"], cwd=self.root,
            check=True, env=self.environment,
        )
        self.commit = subprocess.check_output(
            [GIT, "rev-parse", "HEAD"], cwd=self.root,
            env=self.environment,
        ).decode().strip()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_command(self):
        command = build_exact_ref_command(REF, self.commit)
        return subprocess.run(
            command.argv, cwd=self.root, env=dict(command.environment),
            check=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        )

    def test_command_creates_only_absent_exact_ref(self) -> None:
        created = self.run_command()
        duplicate = self.run_command()

        self.assertEqual(created.returncode, 0)
        self.assertEqual(created.stdout + created.stderr, b"")
        self.assertNotEqual(duplicate.returncode, 0)
        value = subprocess.check_output(
            [GIT, "rev-parse", REF], cwd=self.root, env=self.environment
        ).decode().strip()
        self.assertEqual(value, self.commit)

    def test_repository_hook_configuration_is_overridden(self) -> None:
        hooks = self.root / "malicious-hooks"
        hooks.mkdir()
        marker = self.root / "hook-ran"
        hook = hooks / "reference-transaction"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        hook.chmod(0o700)
        subprocess.run(
            [GIT, "config", "core.hooksPath", str(hooks)], cwd=self.root,
            check=True, env=self.environment,
        )

        created = self.run_command()

        self.assertEqual(created.returncode, 0)
        self.assertFalse(marker.exists())

    def test_symbolic_ref_is_not_dereferenced(self) -> None:
        target = "refs/heads/unintended-target"
        subprocess.run(
            [GIT, "symbolic-ref", REF, target], cwd=self.root,
            check=True, env=self.environment,
        )

        created = self.run_command()

        self.assertNotEqual(created.returncode, 0)
        target_value = subprocess.run(
            [GIT, "show-ref", "--verify", target], cwd=self.root,
            env=self.environment, check=False, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.assertNotEqual(target_value.returncode, 0)
        symbolic = subprocess.check_output(
            [GIT, "symbolic-ref", REF], cwd=self.root,
            env=self.environment,
        ).decode().strip()
        self.assertEqual(symbolic, target)

    def test_inputs_and_returned_environment_are_immutable(self) -> None:
        for ref, sha in (("refs/heads/main", self.commit), (REF, "bad")):
            with self.subTest(ref=ref, sha=sha):
                with self.assertRaises(ExactRefCommandError):
                    build_exact_ref_command(ref, sha)
        command = build_exact_ref_command(REF, self.commit)
        with self.assertRaises(TypeError):
            command.environment["HOME"] = "/tmp"
        self.assertIn("--no-deref", command.argv)
        self.assertIn("core.hooksPath=/dev/null", command.argv)

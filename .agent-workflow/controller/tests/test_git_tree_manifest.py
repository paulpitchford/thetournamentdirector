from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath

from td_controller.git_tree_manifest import (
    GitTreeEntry,
    GitTreeManifestError,
    build_git_tree_command,
    parse_git_tree_manifest,
)

SHA = "a" * 40


class GitTreeManifestTests(unittest.TestCase):
    def test_exact_command_and_valid_manifest_are_immutable(self) -> None:
        command = build_git_tree_command(SHA)
        self.assertEqual(
            command.argv,
            (
                "/usr/bin/git", "-c", "core.hooksPath=/dev/null", "ls-tree",
                "-r", "-z", "--full-tree", SHA,
            ),
        )
        payload = (
            b"100644 blob " + b"1" * 40 + b"\tdocs/readme.md\x00"
            b"100755 blob " + b"2" * 40 + b"\ttools/run.sh\x00"
        )
        self.assertEqual(
            parse_git_tree_manifest(payload),
            (
                GitTreeEntry(
                    PurePosixPath("docs/readme.md"), "1" * 40, False, True
                ),
                GitTreeEntry(
                    PurePosixPath("tools/run.sh"), "2" * 40, True, True
                ),
            ),
        )
        with self.assertRaises(TypeError):
            command.environment["HOME"] = "/tmp"

    def test_malformed_unsafe_and_ambiguous_entries_fail_closed(self) -> None:
        prefix = b"100644 blob " + b"1" * 40 + b"\t"
        invalid = (
            prefix + b"path",
            prefix + b"../path\x00",
            prefix + b".git/config\x00",
            prefix + b"space name\x00",
            b"120000 blob " + b"1" * 40 + b"\tlink\x00",
            b"160000 commit " + b"1" * 40 + b"\tsubmodule\x00",
            prefix + b"a\x00" + prefix + b"a/b\x00",
            prefix + b"b\x00" + prefix + b"a\x00",
        )
        for payload in invalid:
            with self.subTest(payload=payload[:80]):
                with self.assertRaises(GitTreeManifestError):
                    parse_git_tree_manifest(payload)
        excluded = parse_git_tree_manifest(
            prefix + b"extracted/file\x00"
        )
        self.assertFalse(excluded[0].materialize)
        with self.assertRaises(GitTreeManifestError):
            build_git_tree_command("not-a-sha")

    def test_real_git_tree_output_round_trips_without_checkout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="td-tree-manifest-") as temporary:
            repository = Path(temporary).resolve()
            subprocess.run(["/usr/bin/git", "init", "-q", repository], check=True)
            (repository / "file.txt").write_text("file\n", encoding="utf-8")
            script = repository / "run.sh"
            script.write_text("#!/bin/sh\n", encoding="utf-8")
            script.chmod(0o755)
            environment = {
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
                "PATH": "/usr/bin:/bin",
            }
            subprocess.run(
                ["/usr/bin/git", "-C", repository, "add", "."],
                check=True, env=environment,
            )
            subprocess.run(
                ["/usr/bin/git", "-C", repository, "commit", "--no-verify",
                 "-q", "-m", "seed"],
                check=True, env=environment,
            )
            commit = subprocess.check_output(
                ["/usr/bin/git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            command = build_git_tree_command(commit)
            output = subprocess.check_output(
                command.argv, cwd=repository, env=dict(command.environment)
            )
            entries = parse_git_tree_manifest(output)
            self.assertEqual(
                [
                    (entry.path.as_posix(), entry.executable, entry.materialize)
                    for entry in entries
                ],
                [("file.txt", False, True), ("run.sh", True, True)],
            )

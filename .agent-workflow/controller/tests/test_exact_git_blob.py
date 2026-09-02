from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath

from td_controller.exact_git_blob import (
    ExactGitBlobError,
    build_exact_git_blob_command,
    verify_exact_git_blob,
)
from td_controller.git_tree_manifest import GitTreeEntry


class ExactGitBlobTests(unittest.TestCase):
    def test_exact_command_and_known_blob_are_verified(self) -> None:
        payload = b"hello\n"
        blob_sha = "ce013625030ba8dba906f756967f9e9ca394464a"
        entry = GitTreeEntry(PurePosixPath("hello.txt"), blob_sha, False, True)
        command = build_exact_git_blob_command(entry)
        self.assertEqual(
            command.argv,
            (
                "/usr/bin/git", "-c", "core.hooksPath=/dev/null", "cat-file",
                "blob", blob_sha,
            ),
        )
        verified = verify_exact_git_blob(entry, payload)
        self.assertEqual(verified.payload, payload)
        self.assertEqual(verified.blob_sha, blob_sha)
        self.assertFalse(verified.executable)

    def test_wrong_payload_and_denied_entry_fail_closed(self) -> None:
        entry = GitTreeEntry(PurePosixPath("file"), "1" * 40, False, True)
        with self.assertRaises(ExactGitBlobError):
            verify_exact_git_blob(entry, b"wrong")
        denied = GitTreeEntry(
            PurePosixPath("analysis/file"), "1" * 40, False, False
        )
        with self.assertRaises(ExactGitBlobError):
            build_exact_git_blob_command(denied)
        with self.assertRaises(ExactGitBlobError):
            verify_exact_git_blob(denied, b"content")

    def test_real_git_cat_file_round_trips_binary_blob(self) -> None:
        with tempfile.TemporaryDirectory(prefix="td-exact-blob-") as temporary:
            repository = Path(temporary).resolve()
            subprocess.run(["/usr/bin/git", "init", "-q", repository], check=True)
            payload = b"binary\x00payload\xff"
            blob_sha = subprocess.check_output(
                ["/usr/bin/git", "-C", repository, "hash-object", "-w", "--stdin"],
                input=payload,
            ).decode("ascii").strip()
            entry = GitTreeEntry(
                PurePosixPath("binary.dat"), blob_sha, True, True
            )
            command = build_exact_git_blob_command(entry)
            output = subprocess.check_output(
                command.argv, cwd=repository, env=dict(command.environment)
            )
            self.assertEqual(verify_exact_git_blob(entry, output).payload, payload)

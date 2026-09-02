from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from td_controller.pinned_directory_executor import PinnedDirectoryExecutor
from td_controller.repository_manifest_loader import (
    RepositoryManifestLoadError,
    load_verified_repository_manifest,
)
from td_controller.review_runtime import ProcessOutput


class RepositoryManifestLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="td-manifest-repo-")
        self.root = Path(self.temporary.name).resolve()
        subprocess.run(["/usr/bin/git", "init", "-q", self.root], check=True)
        (self.root / "a.txt").write_text("a\n")
        subprocess.run(["/usr/bin/git", "-C", self.root, "add", "a.txt"], check=True)
        subprocess.run(
            ["/usr/bin/git", "-C", self.root, "-c", "user.name=Test",
             "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
            check=True,
        )
        self.commit_sha = subprocess.check_output(
            ["/usr/bin/git", "-C", self.root, "rev-parse", "HEAD"], text=True
        ).strip()
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            self.repository = PinnedDirectoryExecutor(descriptor=descriptor)
        finally:
            os.close(descriptor)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def test_real_pinned_repository_returns_commit_bound_manifest(self) -> None:
        manifest = load_verified_repository_manifest(
            self.repository, self.commit_sha
        )
        self.assertEqual(manifest.commit_sha, self.commit_sha)
        self.assertEqual(tuple(str(entry.path) for entry in manifest.entries), ("a.txt",))

    def test_command_and_manifest_failures_are_normalized(self) -> None:
        outputs = (
            ProcessOutput(1, b"", b""),
            ProcessOutput(0, b"", b"diagnostic"),
            ProcessOutput(0, b"malformed", b""),
        )
        for output in outputs:
            with self.subTest(output=output):
                with patch.object(self.repository, "run", return_value=output):
                    with self.assertRaises(RepositoryManifestLoadError):
                        load_verified_repository_manifest(
                            self.repository, self.commit_sha
                        )
        with self.assertRaises(RepositoryManifestLoadError):
            load_verified_repository_manifest(self.repository, "not-a-sha")

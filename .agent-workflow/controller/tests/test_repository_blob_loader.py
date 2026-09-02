from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from td_controller.git_tree_manifest import GitTreeEntry
from td_controller.pinned_directory_executor import PinnedDirectoryExecutor
from td_controller.repository_blob_loader import (
    RepositoryBlobLoadError,
    load_verified_repository_blob,
)
from td_controller.review_runtime import ProcessOutput


class RepositoryBlobLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="td-repo-blob-")
        self.repository_path = Path(self.temporary.name).resolve()
        subprocess.run(
            ["/usr/bin/git", "init", "-q", self.repository_path], check=True
        )
        self.payload = b"binary\x00payload\xff"
        self.blob_sha = subprocess.check_output(
            ["/usr/bin/git", "-C", self.repository_path,
             "hash-object", "-w", "--stdin"],
            input=self.payload,
        ).decode("ascii").strip()
        self.entry = GitTreeEntry(
            PurePosixPath("binary.dat"), self.blob_sha, False, True
        )
        descriptor = os.open(
            self.repository_path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            self.repository = PinnedDirectoryExecutor(descriptor=descriptor)
        finally:
            os.close(descriptor)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def test_real_pinned_repository_loads_exact_binary_blob(self) -> None:
        verified = load_verified_repository_blob(self.repository, self.entry)
        self.assertEqual(verified.payload, self.payload)
        self.assertEqual(verified.blob_sha, self.blob_sha)

    def test_command_failure_stderr_and_wrong_payload_fail_closed(self) -> None:
        outputs = (
            ProcessOutput(1, b"", b""),
            ProcessOutput(0, self.payload, b"diagnostic"),
            ProcessOutput(0, b"wrong", b""),
        )
        for output in outputs:
            with self.subTest(output=output):
                with patch.object(self.repository, "run", return_value=output):
                    with self.assertRaises(RepositoryBlobLoadError):
                        load_verified_repository_blob(self.repository, self.entry)

    def test_denied_entry_and_invalid_capability_are_rejected(self) -> None:
        denied = GitTreeEntry(
            PurePosixPath("analysis/file"), self.blob_sha, False, False
        )
        with self.assertRaises(RepositoryBlobLoadError):
            load_verified_repository_blob(self.repository, denied)
        with self.assertRaises(RepositoryBlobLoadError):
            load_verified_repository_blob(object(), self.entry)  # type: ignore[arg-type]

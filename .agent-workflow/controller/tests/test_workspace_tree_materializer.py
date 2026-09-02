from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath

from td_controller.git_tree_manifest import GitTreeEntry
from td_controller.pinned_directory_executor import PinnedDirectoryExecutor
from td_controller.workspace_identity_handle import WorkspaceIdentityHandle
from td_controller.workspace_tree_materializer import (
    WorkspaceTreeIndeterminateError,
    WorkspaceTreeRejectedError,
    materialize_workspace_tree,
)


class WorkspaceTreeMaterializerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository_temp = tempfile.TemporaryDirectory(prefix="td-tree-repo-")
        self.workspace_temp = tempfile.TemporaryDirectory(prefix="td-tree-workspace-")
        self.repository_path = Path(self.repository_temp.name).resolve()
        self.workspace_path = Path(self.workspace_temp.name).resolve()
        self.workspace_path.chmod(0o700)
        subprocess.run(
            ["/usr/bin/git", "init", "-q", self.repository_path], check=True
        )
        repository_fd = os.open(
            self.repository_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        self.workspace_fd = os.open(
            self.workspace_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            self.repository = PinnedDirectoryExecutor(descriptor=repository_fd)
            self.workspace = WorkspaceIdentityHandle(
                "ORCH-TEST", attempt=1, generation="a" * 32,
                descriptor=self.workspace_fd,
            )
        finally:
            os.close(repository_fd)

    def tearDown(self) -> None:
        self.workspace.close()
        self.repository.close()
        os.close(self.workspace_fd)
        self.workspace_temp.cleanup()
        self.repository_temp.cleanup()

    def add_blob(self, path: str, payload: bytes, executable: bool = False):
        blob_sha = subprocess.check_output(
            ["/usr/bin/git", "-C", self.repository_path,
             "hash-object", "-w", "--stdin"],
            input=payload,
        ).decode("ascii").strip()
        return GitTreeEntry(PurePosixPath(path), blob_sha, executable, True)

    def test_materializes_all_allowed_entries_and_reports_denied_entries(self) -> None:
        denied = GitTreeEntry(
            PurePosixPath("analysis/private"), "1" * 40, False, False
        )
        regular = self.add_blob("docs/readme.md", b"readme\n")
        executable = self.add_blob("tools/run.sh", b"#!/bin/sh\n", True)
        result = materialize_workspace_tree(
            repository=self.repository, workspace=self.workspace,
            descriptor=self.workspace_fd,
            manifest=(denied, regular, executable),
        )
        self.assertEqual(result.written_paths, (regular.path, executable.path))
        self.assertEqual(result.skipped_paths, (denied.path,))
        self.assertEqual(
            (self.workspace_path / regular.path).read_bytes(), b"readme\n"
        )
        self.assertEqual(
            (self.workspace_path / executable.path).stat().st_mode & 0o777,
            0o755,
        )
        self.assertFalse((self.workspace_path / denied.path).exists())

    def test_second_blob_failure_reports_partial_tree_as_indeterminate(self) -> None:
        first = self.add_blob("a.txt", b"first")
        missing = GitTreeEntry(PurePosixPath("z.txt"), "0" * 40, False, True)
        with self.assertRaises(WorkspaceTreeIndeterminateError):
            materialize_workspace_tree(
                repository=self.repository, workspace=self.workspace,
                descriptor=self.workspace_fd, manifest=(first, missing),
            )
        self.assertEqual((self.workspace_path / "a.txt").read_bytes(), b"first")
        self.assertFalse((self.workspace_path / "z.txt").exists())

    def test_noncanonical_manifests_are_rejected_without_effect(self) -> None:
        payload = b"content"
        digest = hashlib.sha1(
            f"blob {len(payload)}\0".encode() + payload,
            usedforsecurity=False,
        ).hexdigest()
        entry = GitTreeEntry(PurePosixPath("file.txt"), digest, False, True)
        invalid = (
            [entry],
            (entry, entry),
            (GitTreeEntry(PurePosixPath("analysis/x"), digest, False, True),),
            (GitTreeEntry(PurePosixPath(".td-x.partial"), digest, False, True),),
        )
        for manifest in invalid:
            with self.subTest(manifest=manifest):
                with self.assertRaises(WorkspaceTreeRejectedError):
                    materialize_workspace_tree(
                        repository=self.repository, workspace=self.workspace,
                        descriptor=self.workspace_fd,
                        manifest=manifest,  # type: ignore[arg-type]
                    )
        self.assertEqual(list(self.workspace_path.iterdir()), [])

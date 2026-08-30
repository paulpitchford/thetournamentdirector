from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from td_controller.workspace_pins import WorkspacePinError, WorkspacePinRegistry


class WorkspacePinRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(mode=0o700)
        self.descriptor = os.open(
            self.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )

    def tearDown(self) -> None:
        os.close(self.descriptor)
        self.temporary.cleanup()

    def register(self, registry: WorkspacePinRegistry):
        return registry.register(
            "DOC-001", attempt=1, generation="0" * 32,
            descriptor=self.descriptor,
        )

    def test_pin_survives_path_and_symlink_replacement(self) -> None:
        registry = WorkspacePinRegistry()
        pin = self.register(registry)
        self.workspace.rename(self.root / "held")
        self.workspace.symlink_to(self.root)
        snapshot = registry.inspect(pin)
        self.assertEqual(snapshot.inode, (self.root / "held").stat().st_ino)
        registry.retire(pin)
        registry.close()

    def test_registry_duplicates_and_owns_the_supplied_descriptor(self) -> None:
        supplied = os.dup(self.descriptor)
        registry = WorkspacePinRegistry()
        pin = registry.register(
            "DOC-001", attempt=1, generation="0" * 32,
            descriptor=supplied,
        )
        os.close(supplied)
        self.assertEqual(registry.inspect(pin).inode, pin.inode)
        registry.retire(pin)
        registry.close()

    def test_forced_close_releases_active_internal_descriptors(self) -> None:
        duplicates: list[int] = []
        real_dup = os.dup

        def capture(descriptor: int) -> int:
            duplicate = real_dup(descriptor)
            duplicates.append(duplicate)
            return duplicate

        registry = WorkspacePinRegistry()
        with patch("td_controller.workspace_pins.os.dup", side_effect=capture):
            self.register(registry)
        registry.close()
        registry.close()
        with self.assertRaises(OSError):
            os.fstat(duplicates[0])
        with self.assertRaisesRegex(WorkspacePinError, "closed"):
            self.register(registry)

    def test_registry_never_uses_pathname_apis(self) -> None:
        registry = WorkspacePinRegistry()
        with patch(
            "td_controller.workspace_pins.os.open",
            side_effect=AssertionError("pathname access"),
            create=True,
        ), patch(
            "td_controller.workspace_pins.os.mkdir",
            side_effect=AssertionError("pathname mutation"),
            create=True,
        ):
            pin = self.register(registry)
            registry.retire(pin)
        registry.close()

    def test_retirement_preserves_workspace_bytes(self) -> None:
        registry = WorkspacePinRegistry()
        pin = self.register(registry)
        evidence = self.workspace / "evidence"
        evidence.write_text("retain")
        self.assertEqual(registry.inspect(pin).entry_count, 1)
        registry.retire(pin)
        self.assertEqual(evidence.read_text(), "retain")
        with self.assertRaisesRegex(WorkspacePinError, "unavailable"):
            registry.inspect(pin)
        registry.close()

    def test_duplicate_forged_and_permission_drift_fail_closed(self) -> None:
        registry = WorkspacePinRegistry()
        pin = self.register(registry)
        with self.assertRaisesRegex(WorkspacePinError, "already active"):
            self.register(registry)
        forged = replace(pin, inode=pin.inode + 1)
        with self.assertRaisesRegex(WorkspacePinError, "unavailable"):
            registry.inspect(forged)
        self.workspace.chmod(0o777)
        with self.assertRaisesRegex(WorkspacePinError, "permissions"):
            registry.inspect(pin)
        self.workspace.chmod(0o700)
        registry.retire(pin)
        registry.close()

    def test_invalid_identity_and_non_directory_are_rejected(self) -> None:
        registry = WorkspacePinRegistry()
        invalid = (
            ("bad", 1, "0" * 32, self.descriptor),
            ("DOC-001", True, "0" * 32, self.descriptor),
            ("DOC-001", 1, "bad", self.descriptor),
            ("DOC-001", 1, "0" * 32, True),
        )
        for task_id, attempt, generation, descriptor in invalid:
            with self.assertRaises(WorkspacePinError):
                registry.register(
                    task_id, attempt=attempt, generation=generation,
                    descriptor=descriptor,
                )
        file_path = self.root / "file"
        file_path.write_text("not a directory")
        file_descriptor = os.open(file_path, os.O_RDONLY)
        try:
            with self.assertRaisesRegex(WorkspacePinError, "ownership"):
                registry.register(
                    "DOC-001", attempt=1, generation="0" * 32,
                    descriptor=file_descriptor,
                )
        finally:
            os.close(file_descriptor)
        registry.close()

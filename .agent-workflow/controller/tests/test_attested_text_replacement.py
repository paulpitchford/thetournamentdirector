from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from td_controller.attested_payload_runner import AttestedPayloadRunnerError
from td_controller.attested_text_replacement import (
    REPLACEMENT_SCRIPT,
    AttestedTextReplacementApplier,
    AttestedTextReplacementError,
    AttestedTextReplacementIndeterminateError,
)
from td_controller.podman_mount_policy import MountPolicyFixture
from td_controller.review_runtime import ProcessOutput
from td_controller.text_mutation_contract import (
    TextMutationProposal,
    TextReplacement,
)
from td_controller.workspace_identity_handle import WorkspaceIdentityHandle


class FakeRunner:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def run(self, fixture, handle, payload, *, input_bytes=b""):
        self.calls.append((fixture, handle, payload, input_bytes))
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


class AttestedTextReplacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="td-mount-policy-")
        self.root = Path(self.temporary.name)
        self.task = self.root / "task"
        self.sibling = self.root / "sibling"
        self.task.mkdir(mode=0o700)
        self.sibling.mkdir(mode=0o700)
        descriptor = os.open(
            self.task, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            self.handle = WorkspaceIdentityHandle(
                "ORCH-003D1B0I", attempt=1, generation="0" * 32,
                descriptor=descriptor,
            )
        finally:
            os.close(descriptor)
        self.fixture = MountPolicyFixture(self.root, self.task, self.sibling)

    def tearDown(self) -> None:
        self.handle.close()
        self.temporary.cleanup()

    def proposal(
        self,
        *,
        task_id: str = "ORCH-003D1B0I",
        path: str = "docs/pilot.md",
        expected: str | None = None,
        content: str = "beta\n",
        extra: bool = False,
    ) -> TextMutationProposal:
        replacement = TextReplacement(
            path, expected or hashlib.sha256(b"alpha\n").hexdigest(), content
        )
        replacements = (replacement, replacement) if extra else (replacement,)
        return TextMutationProposal(
            task_id, "1" * 40, "2" * 32, "proposal", replacements, 2
        )

    def test_apply_passes_fixed_script_metadata_and_content_to_runner(self) -> None:
        runner = FakeRunner(
            ProcessOutput(0, b"text-replacement-ok\n", b"")
        )

        AttestedTextReplacementApplier(runner=runner).apply(
            self.fixture, self.handle, self.proposal()
        )

        _, passed_handle, payload, input_bytes = runner.calls[0]
        self.assertIs(passed_handle, self.handle)
        self.assertEqual(payload.executable, "/bin/sh")
        self.assertEqual(payload.arguments[:5], (
            "-eu", "-c", REPLACEMENT_SCRIPT, "td-text-replacer",
            "docs/pilot.md",
        ))
        self.assertEqual(
            payload.arguments[5], hashlib.sha256(b"alpha\n").hexdigest()
        )
        self.assertEqual(
            payload.arguments[6], hashlib.sha256(b"beta\n").hexdigest()
        )
        self.assertEqual(input_bytes, b"beta\n")

    def test_script_checks_physical_parent_and_old_hash_before_temp_write(self) -> None:
        parent_check = REPLACEMENT_SCRIPT.index('resolved_parent=$(readlink -f')
        link_check = REPLACEMENT_SCRIPT.index('test ! -L "$target"')
        hardlink_check = REPLACEMENT_SCRIPT.index("stat -c '%h'")
        old_hash = REPLACEMENT_SCRIPT.index('sha256sum "$target"')
        temporary = REPLACEMENT_SCRIPT.index("mktemp")
        new_hash = REPLACEMENT_SCRIPT.index('sha256sum "$temporary"')
        data_sync = REPLACEMENT_SCRIPT.index('sync -d "$temporary"')
        move = REPLACEMENT_SCRIPT.index('mv "$temporary" "$target"')
        directory_sync = REPLACEMENT_SCRIPT.index('sync -f "$parent"')
        self.assertLess(parent_check, link_check)
        self.assertLess(link_check, hardlink_check)
        self.assertLess(hardlink_check, old_hash)
        self.assertLess(old_hash, temporary)
        self.assertLess(temporary, new_hash)
        self.assertLess(new_hash, data_sync)
        self.assertLess(data_sync, move)
        self.assertLess(move, directory_sync)

    def test_task_count_and_path_mismatch_fail_before_runner(self) -> None:
        invalid = (
            self.proposal(task_id="ORCH-OTHER-001"),
            self.proposal(extra=True),
            self.proposal(path="../outside"),
            self.proposal(path="docs/é.md"),
        )
        for proposal in invalid:
            with self.subTest(proposal=proposal):
                runner = FakeRunner(ProcessOutput(0, b"", b""))
                with self.assertRaises(AttestedTextReplacementError):
                    AttestedTextReplacementApplier(runner=runner).apply(
                        self.fixture, self.handle, proposal
                    )
                self.assertEqual(runner.calls, [])

    def test_nonzero_stdout_stderr_and_runner_failure_are_normalized(self) -> None:
        outputs = (
            ProcessOutput(1, b"", b""),
            ProcessOutput(0, b"wrong", b""),
            ProcessOutput(0, b"text-replacement-ok\n", b"warning"),
            AttestedPayloadRunnerError("sensitive detail"),
        )
        for output in outputs:
            with self.subTest(output=output):
                with self.assertRaises(AttestedTextReplacementError) as raised:
                    AttestedTextReplacementApplier(
                        runner=FakeRunner(output)
                    ).apply(self.fixture, self.handle, self.proposal())
                self.assertNotIn("sensitive detail", str(raised.exception))

    def test_post_dispatch_failure_requires_reconciliation(self) -> None:
        outputs = (
            ProcessOutput(
                52, b"text-replacement-indeterminate\n", b""
            ),
            AttestedPayloadRunnerError("timeout"),
        )
        for output in outputs:
            with self.subTest(output=output):
                with self.assertRaises(
                    AttestedTextReplacementIndeterminateError
                ):
                    AttestedTextReplacementApplier(
                        runner=FakeRunner(output)
                    ).apply(self.fixture, self.handle, self.proposal())

    def test_invalid_runner_handle_and_proposal_are_rejected(self) -> None:
        with self.assertRaisesRegex(AttestedTextReplacementError, "runner"):
            AttestedTextReplacementApplier(runner=object())
        applier = AttestedTextReplacementApplier(
            runner=FakeRunner(ProcessOutput(0, b"", b""))
        )
        with self.assertRaisesRegex(AttestedTextReplacementError, "handle"):
            applier.apply(self.fixture, object(), self.proposal())
        with self.assertRaisesRegex(AttestedTextReplacementError, "proposal"):
            applier.apply(self.fixture, self.handle, object())

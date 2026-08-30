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
)
from td_controller.podman_mount_policy import MountPolicyFixture
from td_controller.replacement_outcome import (
    APPLIED_SENTINEL,
    ReplacementIndeterminateError,
    ReplacementOutcomeError,
)
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
        self.task, self.sibling = self.root / "task", self.root / "sibling"
        self.task.mkdir(mode=0o700)
        self.sibling.mkdir(mode=0o700)
        descriptor = os.open(
            self.task, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            self.handle = WorkspaceIdentityHandle(
                "ORCH-003D1B0I2", attempt=1, generation="0" * 32,
                descriptor=descriptor,
            )
        finally:
            os.close(descriptor)
        self.fixture = MountPolicyFixture(self.root, self.task, self.sibling)

    def tearDown(self) -> None:
        self.handle.close()
        self.temporary.cleanup()

    def proposal(self, *, task_id="ORCH-003D1B0I2", path="docs/pilot.md",
                 content="beta\n", count=1):
        replacement = TextReplacement(
            path, hashlib.sha256(b"alpha\n").hexdigest(), content
        )
        return TextMutationProposal(
            task_id, "1" * 40, "2" * 32, "proposal",
            (replacement,) * count, 2,
        )

    def test_apply_passes_only_fixed_payload_and_content(self) -> None:
        runner = FakeRunner(ProcessOutput(0, APPLIED_SENTINEL, b""))

        AttestedTextReplacementApplier(runner=runner).apply(
            self.fixture, self.handle, self.proposal()
        )

        _, passed_handle, payload, content = runner.calls[0]
        self.assertIs(passed_handle, self.handle)
        self.assertEqual(payload.executable, "/bin/sh")
        self.assertEqual(payload.arguments[1:5], (
            "-c", REPLACEMENT_SCRIPT, "td-text-replacer", "docs/pilot.md"
        ))
        self.assertEqual(payload.arguments[5], hashlib.sha256(b"alpha\n").hexdigest())
        self.assertEqual(payload.arguments[6], hashlib.sha256(b"beta\n").hexdigest())
        self.assertEqual(content, b"beta\n")

    def test_script_marks_mutation_point_and_postchecks_indeterminate(self) -> None:
        old_hash = REPLACEMENT_SCRIPT.index('sha256sum "$target"')
        temp = REPLACEMENT_SCRIPT.index("mktemp")
        data_sync = REPLACEMENT_SCRIPT.index('sync -d "$temporary"')
        move = REPLACEMENT_SCRIPT.index('mv -T "$temporary" "$target"')
        post_hash = REPLACEMENT_SCRIPT.rindex('sha256sum "$target"')
        directory_sync = REPLACEMENT_SCRIPT.index('sync -f "$parent"')
        self.assertLess(old_hash, temp)
        self.assertLess(temp, data_sync)
        self.assertLess(data_sync, move)
        self.assertIn("|| exit 52", REPLACEMENT_SCRIPT[move:])
        self.assertLess(move, post_hash)
        self.assertLess(post_hash, directory_sync)

    def test_shared_outcome_protocol_rejects_or_reconciles(self) -> None:
        with self.assertRaisesRegex(ReplacementOutcomeError, "rejected"):
            AttestedTextReplacementApplier(
                runner=FakeRunner(ProcessOutput(1, b"", b""))
            ).apply(self.fixture, self.handle, self.proposal())
        with self.assertRaises(ReplacementIndeterminateError):
            AttestedTextReplacementApplier(
                runner=FakeRunner(AttestedPayloadRunnerError("secret"))
            ).apply(self.fixture, self.handle, self.proposal())

    def test_invalid_task_count_path_content_and_runner_fail_before_dispatch(self) -> None:
        invalid = (
            self.proposal(task_id="ORCH-OTHER-001"),
            self.proposal(count=2),
            self.proposal(path="../escape"),
            self.proposal(path="docs/é.md"),
            self.proposal(content="bad\x00content"),
        )
        for proposal in invalid:
            with self.subTest(proposal=proposal):
                runner = FakeRunner(ProcessOutput(0, APPLIED_SENTINEL, b""))
                with self.assertRaises(AttestedTextReplacementError):
                    AttestedTextReplacementApplier(runner=runner).run(
                        self.fixture, self.handle, proposal
                    )
                self.assertEqual(runner.calls, [])
        with self.assertRaisesRegex(AttestedTextReplacementError, "runner"):
            AttestedTextReplacementApplier(runner=object())

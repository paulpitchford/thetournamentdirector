from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from td_controller.mutation_dependencies import MutationDependencyError
from td_controller.mutation_pipeline import MutationPipeline, MutationPipelineError
from td_controller.podman_mount_policy import MountPolicyFixture
from td_controller.replacement_outcome import ReplacementIndeterminateError
from td_controller.task_contract import parse_task
from td_controller.text_mutation_broker import TextMutationBrokerResult
from td_controller.text_mutation_contract import (
    SelectedTextFile,
    TextMutationProposal,
    TextReplacement,
    build_text_mutation_request,
)
from td_controller.workspace_identity_handle import WorkspaceIdentityHandle


def task(task_id="ORCH-003D1B0J2"):
    criterion = "pipeline-proof|WHEN selected text is changed|ASSERT TEST_PASS proof"
    return parse_task({
        "id": task_id, "status": "APPROVED", "parentEpic": "ORCH-PIPELINE",
        "objective": "Replace alpha with beta.", "nonGoals": [], "dependsOn": [],
        "acceptanceCriteria": [criterion],
        "acceptanceEvidenceIds": {criterion: ["proof"]},
        "acceptanceEvidenceRequirements": {},
        "requiredTests": ["python3 .agent-workflow/scripts/check_repository.py"],
        "allowedPaths": ["docs/pilot.md"], "protectedPaths": [".github/**"],
        "riskClass": "R3", "maxChangedLines": 4, "maxAttempts": 1,
        "humanApprovalRequired": True,
    })


def request():
    return build_text_mutation_request(
        task(), base_sha="1" * 40, nonce="2" * 32,
        files=[SelectedTextFile(
            "docs/pilot.md", hashlib.sha256(b"alpha\n").hexdigest(), "alpha\n"
        )],
    )


def result(
    *, replacements=1, nonce="2" * 32, path="docs/pilot.md",
    expected=None,
):
    replacement = TextReplacement(
        path, expected or hashlib.sha256(b"alpha\n").hexdigest(), "beta\n"
    )
    proposal = TextMutationProposal(
        "ORCH-003D1B0J2", "1" * 40, nonce, "replace",
        (replacement,) * replacements, 2,
    )
    return TextMutationBrokerResult("fresh-session", proposal)


class FakeBroker:
    def __bool__(self):
        return False

    def __init__(self, value):
        self.value = value
        self.calls = []

    def run(self, mutation_request):
        self.calls.append(mutation_request)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeApplier:
    def __bool__(self):
        return False

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def apply(self, fixture, handle, proposal):
        self.calls.append((fixture, handle, proposal))
        if self.error:
            raise self.error


class MutationPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="td-mount-policy-")
        root = Path(self.temporary.name)
        workspace, sibling = root / "task", root / "sibling"
        workspace.mkdir(mode=0o700)
        sibling.mkdir(mode=0o700)
        descriptor = os.open(
            workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            self.handle = WorkspaceIdentityHandle(
                "ORCH-003D1B0J2", attempt=1, generation="0" * 32,
                descriptor=descriptor,
            )
        finally:
            os.close(descriptor)
        self.fixture = MountPolicyFixture(root, workspace, sibling)

    def tearDown(self) -> None:
        self.handle.close()
        self.temporary.cleanup()

    def test_valid_fresh_single_proposal_is_applied_once(self) -> None:
        broker, applier = FakeBroker(result()), FakeApplier()

        output = MutationPipeline(broker=broker, applier=applier).run(
            self.fixture, self.handle, request()
        )

        self.assertEqual(output.session_id, "fresh-session")
        self.assertEqual(output.changed_path, "docs/pilot.md")
        self.assertEqual(len(broker.calls), 1)
        self.assertEqual(len(applier.calls), 1)

    def test_stale_or_multiple_broker_result_never_reaches_applier(self) -> None:
        for broker_result in (result(nonce="3" * 32), result(replacements=2), object()):
            with self.subTest(broker_result=broker_result):
                applier = FakeApplier()
                with self.assertRaisesRegex(MutationPipelineError, "result"):
                    MutationPipeline(
                        broker=FakeBroker(broker_result), applier=applier
                    ).run(self.fixture, self.handle, request())
                self.assertEqual(applier.calls, [])

    def test_unselected_path_or_digest_never_reaches_applier(self) -> None:
        invalid = (
            result(path="docs/unselected.md"),
            result(expected="0" * 64),
        )
        for broker_result in invalid:
            with self.subTest(broker_result=broker_result):
                applier = FakeApplier()
                with self.assertRaisesRegex(MutationPipelineError, "selection"):
                    MutationPipeline(
                        broker=FakeBroker(broker_result), applier=applier
                    ).run(self.fixture, self.handle, request())
                self.assertEqual(applier.calls, [])

    def test_request_task_mismatch_fails_before_broker(self) -> None:
        other = build_text_mutation_request(
            task("ORCH-OTHER-001"), base_sha="1" * 40, nonce="2" * 32,
            files=[SelectedTextFile(
                "docs/pilot.md", hashlib.sha256(b"alpha\n").hexdigest(),
                "alpha\n",
            )],
        )
        broker = FakeBroker(result())
        with self.assertRaisesRegex(MutationPipelineError, "identity"):
            MutationPipeline(broker=broker, applier=FakeApplier()).run(
                self.fixture, self.handle, other
            )
        self.assertEqual(broker.calls, [])

    def test_indeterminate_applier_outcome_is_not_normalized_or_retried(self) -> None:
        error = ReplacementIndeterminateError("reconcile")
        applier = FakeApplier(error)
        with self.assertRaises(ReplacementIndeterminateError):
            MutationPipeline(
                broker=FakeBroker(result()), applier=applier
            ).run(self.fixture, self.handle, request())
        self.assertEqual(len(applier.calls), 1)

    def test_invalid_dependencies_are_rejected(self) -> None:
        with self.assertRaisesRegex(MutationDependencyError, "broker"):
            MutationPipeline(broker=object())
        with self.assertRaisesRegex(MutationDependencyError, "applier"):
            MutationPipeline(applier=object())

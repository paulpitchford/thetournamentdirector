"""Coordinate one tool-less proposal with one attested text replacement."""

from __future__ import annotations

import hashlib
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .attested_text_replacement import AttestedTextReplacementApplier
from .mutation_dependencies import build_mutation_dependencies
from .podman_mount_policy import MountPolicyFixture
from .text_mutation_broker import (
    TextMutationBroker,
    TextMutationBrokerResult,
)
from .text_mutation_contract import (
    SelectedTextFile,
    TextMutationProposal,
    TextMutationRequest,
    build_text_mutation_request,
)
from .task_contract import parse_task
from .workspace_identity_handle import WorkspaceIdentityHandle


class MutationBroker(Protocol):
    def run(self, request: TextMutationRequest) -> TextMutationBrokerResult:
        ...


class MutationApplier(Protocol):
    def apply(
        self, fixture: MountPolicyFixture, handle: WorkspaceIdentityHandle,
        proposal: object,
    ) -> None:
        ...


class MutationPipelineError(RuntimeError):
    """Raised when proposal identity cannot authorize a single replacement."""


@dataclass(frozen=True)
class MutationPipelineResult:
    """Identity of the model session and proposal applied by the worker."""

    session_id: str
    changed_path: str


class MutationPipeline:
    """Keep model inference and filesystem effects in separate boundaries."""

    def __init__(
        self, *, broker: MutationBroker | None = None,
        applier: MutationApplier | None = None,
    ) -> None:
        dependencies = build_mutation_dependencies(
            broker=broker,
            applier=applier,
            broker_factory=TextMutationBroker,
            applier_factory=AttestedTextReplacementApplier,
        )
        self._broker = dependencies.broker
        self._applier = dependencies.applier

    def run(
        self, fixture: MountPolicyFixture, handle: WorkspaceIdentityHandle,
        request: TextMutationRequest,
    ) -> MutationPipelineResult:
        """Broker first, then apply only one exactly bound replacement."""
        if (
            not isinstance(handle, WorkspaceIdentityHandle)
            or not isinstance(request, TextMutationRequest)
            or request.task.task_id != handle.identity.task_id
        ):
            raise MutationPipelineError("mutation pipeline identity is invalid")
        result = self._broker.run(request)
        if (
            not isinstance(result, TextMutationBrokerResult)
            or not isinstance(result.session_id, str)
            or not result.session_id.strip()
            or not isinstance(result.proposal, TextMutationProposal)
            or result.proposal.task_id != handle.identity.task_id
            or result.proposal.base_sha != request.base_sha
            or result.proposal.nonce != request.nonce
            or len(result.proposal.replacements) != 1
        ):
            raise MutationPipelineError("mutation broker result is invalid")
        replacement = result.proposal.replacements[0]
        selected = request.files.get(replacement.path)
        if (
            selected is None
            or replacement.expected_sha256 != selected.sha256
        ):
            raise MutationPipelineError("mutation broker selection is invalid")
        self._applier.apply(fixture, handle, result.proposal)
        return MutationPipelineResult(result.session_id, replacement.path)


def run_local_probe() -> None:
    criterion = "pipeline-proof|WHEN selected text is changed|ASSERT TEST_PASS proof"
    task_contract = parse_task(
        {
            "id": "ORCH-003D1B0J2", "status": "APPROVED",
            "parentEpic": "ORCH-PIPELINE", "objective": "Replace alpha with beta.",
            "nonGoals": ["Change other text"], "dependsOn": [],
            "acceptanceCriteria": [criterion],
            "acceptanceEvidenceIds": {criterion: ["proof"]},
            "acceptanceEvidenceRequirements": {},
            "requiredTests": ["python3 .agent-workflow/scripts/check_repository.py"],
            "allowedPaths": ["docs/pilot.md"], "protectedPaths": [".github/**"],
            "riskClass": "R3", "maxChangedLines": 4, "maxAttempts": 1,
            "humanApprovalRequired": True,
        }
    )
    with tempfile.TemporaryDirectory(
        prefix="td-mount-policy-", dir="/var/tmp"
    ) as temporary:
        root = Path(temporary)
        workspace, sibling = root / "task", root / "sibling"
        workspace.mkdir(mode=0o700)
        sibling.mkdir(mode=0o700)
        docs = workspace / "docs"
        docs.mkdir(mode=0o700)
        target = docs / "pilot.md"
        target.write_bytes(b"alpha\n")
        target.chmod(0o644)
        descriptor = os.open(
            workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            handle = WorkspaceIdentityHandle(
                task_contract.task_id, attempt=1, generation="0" * 32,
                descriptor=descriptor,
            )
        finally:
            os.close(descriptor)
        request = build_text_mutation_request(
            task_contract, base_sha="1" * 40, nonce=secrets.token_hex(16),
            files=[
                SelectedTextFile(
                    "docs/pilot.md", hashlib.sha256(b"alpha\n").hexdigest(),
                    "alpha\n",
                )
            ],
        )
        try:
            result = MutationPipeline().run(
                MountPolicyFixture(root, workspace, sibling), handle, request
            )
            if not result.session_id or target.read_bytes() == b"alpha\n":
                raise MutationPipelineError("mutation pipeline proof made no change")
            handle.verify()
        finally:
            handle.close()


if __name__ == "__main__":
    run_local_probe()
    print("Tool-less-to-attested mutation pipeline proof passed.")

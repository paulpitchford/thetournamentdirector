"""Tests for structured local-review contracts and QA evidence rules."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from td_controller.review_contract import (
    CodexReviewError,
    ReviewRequest,
    TrustedEvidence,
    _parse_artifact,
    _validate_trusted_evidence,
)

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def request(role: str = "qa_review") -> ReviewRequest:
    return ReviewRequest(
        task_id="TASK-001",
        role=role,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        task_contract={
            "id": "TASK-001",
            "acceptanceCriteria": ["CI passes"],
            "acceptanceEvidenceRequirements": {"CI passes": ["github_actions"]},
        },
        diff="diff --git a/a.py b/a.py\n+value = 1\n",
        deterministic_evidence=(
            TrustedEvidence(
                evidence_id="ci-controller-tests",
                source="github_actions",
                description="controller-tests: pass",
            ),
        ),
    )


def qa_artifact() -> dict[str, object]:
    return {
        "reviewType": "qa",
        "taskId": "TASK-001",
        "baseSha": BASE_SHA,
        "headSha": HEAD_SHA,
        "verdict": "pass",
        "findings": [],
        "acceptanceEvidence": [
            {
                "criterion": "CI passes",
                "status": "pass",
                "evidence": "controller-tests: pass",
                "evidenceRefs": ["ci-controller-tests"],
            }
        ],
    }


def finding() -> dict[str, object]:
    return {
        "id": "finding-1",
        "severity": "medium",
        "path": "README.md",
        "line": 1,
        "evidence": "bounded evidence",
        "risk": "bounded risk",
        "requiredAction": "fix it",
        "suggestedTest": None,
        "confidence": 0.9,
    }


class ReviewContractTests(unittest.TestCase):
    def parse(self, value: dict[str, object], role: str = "qa_review"):
        return _parse_artifact(json.dumps(value), request(role))

    def test_valid_qa_maps_exactly_one_trusted_criterion(self) -> None:
        artifact = self.parse(qa_artifact())

        self.assertEqual(artifact.verdict, "pass")
        self.assertEqual(artifact.acceptance_evidence[0].evidence_refs, ("ci-controller-tests",))

    def test_qa_requires_every_configured_criterion(self) -> None:
        value = qa_artifact()
        value["acceptanceEvidence"] = []

        with self.assertRaisesRegex(CodexReviewError, "map every acceptance"):
            self.parse(value)

    def test_qa_rejects_unknown_and_duplicate_criteria(self) -> None:
        unknown = qa_artifact()
        unknown["acceptanceEvidence"][0]["criterion"] = "invented"
        with self.assertRaisesRegex(CodexReviewError, "map every acceptance"):
            self.parse(unknown)

        duplicate = qa_artifact()
        duplicate["acceptanceEvidence"].append(
            duplicate["acceptanceEvidence"][0].copy()
        )
        with self.assertRaisesRegex(CodexReviewError, "duplicate acceptance"):
            self.parse(duplicate)

    def test_qa_rejects_unknown_evidence_reference(self) -> None:
        value = qa_artifact()
        value["acceptanceEvidence"][0]["evidenceRefs"] = ["invented"]

        with self.assertRaisesRegex(CodexReviewError, "unknown refs"):
            self.parse(value)

    def test_qa_enforces_required_evidence_source(self) -> None:
        qa_request = replace(
            request(),
            task_contract={
                "id": "TASK-001",
                "acceptanceCriteria": ["CI passes"],
                "acceptanceEvidenceRequirements": {"CI passes": ["local_rootless"]},
            },
        )

        with self.assertRaisesRegex(CodexReviewError, "required evidence sources"):
            _parse_artifact(json.dumps(qa_artifact()), qa_request)

    def test_qa_verdict_exactly_matches_aggregate_status(self) -> None:
        failed_pass = qa_artifact()
        failed_pass["acceptanceEvidence"][0]["status"] = "fail"
        with self.assertRaisesRegex(CodexReviewError, "aggregate criterion"):
            self.parse(failed_pass)

        passing_block = qa_artifact()
        passing_block["verdict"] = "block"
        passing_block["findings"] = [finding()]
        with self.assertRaisesRegex(CodexReviewError, "aggregate criterion"):
            self.parse(passing_block)

    def test_qa_block_needs_no_duplicate_finding(self) -> None:
        value = qa_artifact()
        value["verdict"] = "block"
        value["acceptanceEvidence"][0]["status"] = "not_tested"

        artifact = self.parse(value)

        self.assertEqual(artifact.verdict, "block")
        self.assertEqual(artifact.findings, ())

    def test_code_review_block_requires_a_finding(self) -> None:
        value = qa_artifact()
        value.update(
            reviewType="code_security",
            verdict="block",
            findings=[],
            acceptanceEvidence=[],
        )

        with self.assertRaisesRegex(CodexReviewError, "must contain findings"):
            self.parse(value, "code_review")

    def test_trusted_evidence_ids_and_sources_are_validated(self) -> None:
        duplicate = (
            TrustedEvidence("same", "github_actions", "first"),
            TrustedEvidence("same", "local_controller", "second"),
        )
        with self.assertRaisesRegex(CodexReviewError, "duplicate trusted evidence"):
            _validate_trusted_evidence(duplicate)

        with self.assertRaisesRegex(CodexReviewError, "unapproved trusted evidence"):
            _validate_trusted_evidence((TrustedEvidence("one", "model", "result"),))


if __name__ == "__main__":
    unittest.main()

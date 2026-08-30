from __future__ import annotations

import hashlib
import unittest
from copy import deepcopy

from td_controller.task_contract import parse_task
from td_controller.text_mutation_contract import (
    MAX_FILE_BYTES,
    MUTATION_PROPOSAL_SCHEMA,
    SelectedTextFile,
    TextMutationContractError,
    build_text_mutation_request,
    parse_text_mutation_proposal,
)

BASE_SHA = "1" * 40
NONCE = "2" * 32


def task(*, max_lines: int = 20):
    criterion = "proposal-proof|WHEN selected text is replaced|ASSERT TEST_PASS proof"
    return parse_task(
        {
            "id": "ORCH-TEST-001",
            "status": "APPROVED",
            "parentEpic": "ORCH-TEST",
            "objective": "Replace selected documentation text safely",
            "nonGoals": [],
            "dependsOn": [],
            "acceptanceCriteria": [criterion],
            "acceptanceEvidenceIds": {criterion: ["proof"]},
            "acceptanceEvidenceRequirements": {},
            "requiredTests": ["python3 .agent-workflow/scripts/check_repository.py"],
            "allowedPaths": ["docs/**"],
            "protectedPaths": [".github/**"],
            "riskClass": "R2",
            "maxChangedLines": max_lines,
            "maxAttempts": 2,
            "humanApprovalRequired": False,
        }
    )


def selected(path: str = "docs/pilot.md", content: str = "alpha\n"):
    return SelectedTextFile(
        path, hashlib.sha256(content.encode()).hexdigest(), content
    )


def request(*, max_lines: int = 20):
    return build_text_mutation_request(
        task(max_lines=max_lines), base_sha=BASE_SHA, nonce=NONCE,
        files=[selected()],
    )


def proposal(**changes):
    value = {
        "taskId": "ORCH-TEST-001",
        "baseSha": BASE_SHA,
        "nonce": NONCE,
        "summary": "Replace alpha with beta.",
        "replacements": [
            {
                "path": "docs/pilot.md",
                "expectedSha256": selected().sha256,
                "content": "beta\n",
            }
        ],
    }
    value.update(changes)
    return value


class TextMutationContractTests(unittest.TestCase):
    def test_valid_replacement_is_bound_and_immutable(self) -> None:
        mutation_request = request()

        result = parse_text_mutation_proposal(proposal(), mutation_request)

        self.assertEqual(result.task_id, "ORCH-TEST-001")
        self.assertEqual(result.changed_lines, 2)
        self.assertEqual(result.replacements[0].content, "beta\n")
        with self.assertRaises(TypeError):
            mutation_request.files["docs/other.md"] = selected()

    def test_request_requires_exact_hash_allowed_path_and_unique_files(self) -> None:
        cases = (
            [SelectedTextFile("docs/pilot.md", "0" * 64, "alpha\n")],
            [selected("README.md")],
            [selected(), selected()],
        )
        for files in cases:
            with self.subTest(files=files):
                with self.assertRaises(TextMutationContractError):
                    build_text_mutation_request(
                        task(), base_sha=BASE_SHA, nonce=NONCE, files=files
                    )

    def test_request_identity_and_collection_are_strict(self) -> None:
        cases = (
            ("1" * 39, NONCE, [selected()]),
            (BASE_SHA, "2" * 31, [selected()]),
            (BASE_SHA, NONCE, []),
            (BASE_SHA, NONCE, "not-files"),
        )
        for base_sha, nonce, files in cases:
            with self.subTest(base_sha=base_sha, nonce=nonce, files=files):
                with self.assertRaises(TextMutationContractError):
                    build_text_mutation_request(
                        task(), base_sha=base_sha, nonce=nonce, files=files
                    )

    def test_proposal_requires_exact_task_sha_and_nonce(self) -> None:
        for field, value in (
            ("taskId", "ORCH-OTHER-001"),
            ("baseSha", "3" * 40),
            ("nonce", "4" * 32),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(TextMutationContractError, "stale"):
                    parse_text_mutation_proposal(
                        proposal(**{field: value}), request()
                    )

    def test_replacement_path_hash_and_fields_are_closed(self) -> None:
        cases = []
        for field, value in (
            ("path", "docs/other.md"),
            ("expectedSha256", "0" * 64),
        ):
            candidate = proposal()
            candidate["replacements"][0][field] = value
            cases.append(candidate)
        extra = proposal()
        extra["replacements"][0]["operation"] = "write"
        cases.append(extra)
        for candidate in cases:
            with self.subTest(candidate=candidate):
                with self.assertRaises(TextMutationContractError):
                    parse_text_mutation_proposal(candidate, request())

    def test_duplicate_and_unchanged_replacements_are_rejected(self) -> None:
        unchanged = proposal()
        unchanged["replacements"][0]["content"] = "alpha\n"
        duplicate = proposal()
        duplicate["replacements"].append(
            deepcopy(duplicate["replacements"][0])
        )
        for candidate in (unchanged, duplicate):
            with self.subTest(candidate=candidate):
                with self.assertRaises(TextMutationContractError):
                    parse_text_mutation_proposal(candidate, request())

    def test_text_and_changed_line_limits_fail_closed(self) -> None:
        oversized = proposal()
        oversized["replacements"][0]["content"] = "x" * (MAX_FILE_BYTES + 1)
        too_many_lines = proposal()
        too_many_lines["replacements"][0]["content"] = "one\ntwo\nthree\n"
        invalid_unicode = proposal()
        invalid_unicode["replacements"][0]["content"] = "\ud800"
        for candidate, mutation_request in (
            (oversized, request()),
            (too_many_lines, request(max_lines=3)),
            (invalid_unicode, request()),
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaises(TextMutationContractError):
                    parse_text_mutation_proposal(candidate, mutation_request)

    def test_top_level_fields_summary_and_replacement_count_are_strict(self) -> None:
        extra = proposal(extra="field")
        blank = proposal(summary="   ")
        empty = proposal(replacements=[])
        for candidate in (extra, blank, empty):
            with self.subTest(candidate=candidate):
                with self.assertRaises(TextMutationContractError):
                    parse_text_mutation_proposal(candidate, request())

    def test_schema_is_closed_at_both_object_levels(self) -> None:
        self.assertFalse(MUTATION_PROPOSAL_SCHEMA["additionalProperties"])
        replacement = MUTATION_PROPOSAL_SCHEMA["properties"]["replacements"]
        self.assertFalse(replacement["items"]["additionalProperties"])
        self.assertEqual(replacement["maxItems"], 8)

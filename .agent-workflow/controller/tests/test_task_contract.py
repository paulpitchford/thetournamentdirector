"""Adversarial tests for the durable task contract."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from td_controller.task_contract import (
    TaskContractError, load_task, parse_dispatch_task_json, parse_task, parse_task_json,
    tracked_path_is_allowed, validate_for_dispatch,
)


def valid_task() -> dict[str, object]:
    criterion = (
        "controller-tests|WHEN controller verification runs|ASSERT TEST_PASS controller-tests"
    )
    return {
        "id": "APP-001",
        "status": "APPROVED",
        "parentEpic": "MODERN-APP",
        "objective": "Create one bounded domain capability",
        "nonGoals": [],
        "dependsOn": [],
        "acceptanceCriteria": [criterion],
        "acceptanceEvidenceIds": {criterion: ["controller-tests"]},
        "acceptanceEvidenceRequirements": {criterion: ["github_actions"]},
        "requiredTests": ["python3 .agent-workflow/scripts/check_repository.py"],
        "allowedPaths": ["modern-app/src/**"],
        "protectedPaths": [".github/**"],
        "riskClass": "R1",
        "maxChangedLines": 500,
        "maxAttempts": 2,
        "humanApprovalRequired": False,
    }


class TaskContractTests(unittest.TestCase):
    def test_valid_task_is_normalized_without_coercion(self) -> None:
        task = parse_task(valid_task())

        self.assertEqual(task.task_id, "APP-001")
        self.assertEqual(task.acceptance_criteria, (
            "controller-tests|WHEN controller verification runs|ASSERT TEST_PASS "
            "controller-tests",
        ))
        self.assertEqual(task.acceptance_evidence_ids[task.acceptance_criteria[0]],
                         ("controller-tests",))
        self.assertFalse(task.human_approval_required)

    def test_repository_task_fixtures_remain_valid(self) -> None:
        root = Path(__file__).parents[2] / "tasks"
        tasks = [
            load_task(path, trusted_root=root) for path in sorted(root.glob("*.json"))
        ]

        self.assertGreaterEqual(len(tasks), 2)
        self.assertEqual(len({task.task_id for task in tasks}), len(tasks))

    def test_duplicate_json_keys_are_rejected_recursively(self) -> None:
        payload = json.dumps(valid_task()).replace(
            '"acceptanceEvidenceIds": {',
            '"acceptanceEvidenceIds": {"duplicate": [], "duplicate": [],',
        )

        with self.assertRaisesRegex(TaskContractError, "unambiguous JSON"):
            parse_task_json(payload)

    def test_decoded_surrogates_are_rejected_in_all_text_fields(self) -> None:
        for surrogate in ("\ud800", "\udfff"):
            value = valid_task()
            value["objective"] = surrogate
            payload = json.dumps(value)
            with self.assertRaisesRegex(TaskContractError, "valid Unicode"):
                parse_task_json(payload)

    def test_unknown_and_missing_fields_fail_closed(self) -> None:
        for mutation in (
            lambda task: task.update({"surprise": True}),
            lambda task: task.pop("allowedPaths"),
        ):
            with self.subTest(mutation=mutation):
                value = valid_task()
                mutation(value)
                with self.assertRaisesRegex(TaskContractError, "fields"):
                    parse_task(value)

    def test_boolean_is_not_accepted_as_integer(self) -> None:
        value = valid_task()
        value["maxChangedLines"] = True

        with self.assertRaisesRegex(TaskContractError, "allowed range"):
            parse_task(value)

    def test_invalid_status_risk_and_approval_types_are_rejected(self) -> None:
        cases = (("status", "UNKNOWN"), ("riskClass", "R9"),
                 ("humanApprovalRequired", 1))
        for field, invalid in cases:
            with self.subTest(field=field):
                value = valid_task()
                value[field] = invalid
                with self.assertRaises(TaskContractError):
                    parse_task(value)

    def test_identifiers_and_dependencies_are_strict(self) -> None:
        for field, invalid in (("id", "lowercase"), ("parentEpic", "../EPIC")):
            with self.subTest(field=field):
                value = valid_task()
                value[field] = invalid
                with self.assertRaisesRegex(TaskContractError, "identifier"):
                    parse_task(value)
        value = valid_task()
        value["dependsOn"] = ["APP-001"]
        with self.assertRaisesRegex(TaskContractError, "depend on itself"):
            parse_task(value)

    def test_vague_duplicate_and_empty_criteria_are_rejected(self) -> None:
        for criteria, message in (
                                  (["WHEN any input is provided THEN feature works well"],
                                   "vague"),
                                  (["WHEN anything happens THEN service returns something"],
                                   "vague"),
                                  (["WHEN any request arrives THEN service returns "
                                    "something immediately"], "vague"),
                                  (["WHEN any request arrives THEN service produces a "
                                    "response successfully"], "vague"),
                                  (["WHEN any request arrives THEN service validates all "
                                    "data consistently"], "vague"),
                                  (["WHEN any request arrives THEN API returns an "
                                    "unspecified response"], "vague"),
                                  (["WHEN any request arrives THEN service returns a "
                                    "response with information"], "vague"),
                                  (["WHEN routine user activity occurs THEN service handles "
                                    "the request with care"], "vague"),
                                  (["WHEN any request arrives THEN service returns response "
                                    "labelled FOO"], "vague"),
                                  (["WHEN any request arrives THEN service returns response "
                                    "with \"information"], "vague"),
                                  (["WHEN any request arrives THEN service returns empty \"\""],
                                   "vague"),
                                  (["WHEN any request arrives THEN service returns empty ``"],
                                   "vague"),
                                  (['WHEN any request arrives THEN service does a thing '
                                    'called "x"'], "vague"),
                                  (['WHEN any request arrives THEN service returns status "READY" '
                                    'and dangling "'], "vague"),
                                  (["It works well"], "vague"),
                                  (["The feature works correctly"], "vague"),
                                  (["It works"], "vague"),
                                  (["Everything works"], "vague"),
                                  (["The API works"], "vague"),
                                  (["The feature works."], "vague"),
                                  (["The feature performs correctly"], "vague"),
                                  (["The response is good enough"], "vague"),
                                  (["Fast and accurate"], "vague"),
                                  (["It passes"], "vague"),
                                  (["Everything returns"], "vague"),
                                  (["The result validates"], "vague"),
                                  (["The controller passes"], "vague"),
                                  (["The service is validated"], "vague"),
                                  (["Validates input"], "vague"),
                                  (["Returns values"], "vague"),
                                  (["Uses configuration"], "vague"),
                                  (["Everything returns a result"], "vague"),
                                  (["The feature passes successfully"], "vague"),
                                  (["The service returns something"], "vague"),
                                  (["The controller validates data"], "vague"),
                                  (["The API returns output"], "vague"),
                                  (["Controller returns expected output"], "vague"),
                                  (["Service produces a response"], "vague"),
                                  (["The service returns the expected result"], "vague"),
                                  (["Passes every test"], "vague"),
                                  (["Rejects invalid input"], "vague"),
                                  (["Returns an error"], "vague"),
                                  (["Validates all data"], "vague"),
                                  (["The modern API controller validates data"], "vague"),
                                  (["Exact result", "Exact result"], "duplicates"),
                                  ([], "bounded list")):
            with self.subTest(criteria=criteria):
                if message == "vague":
                    message = "acceptance criterion"
                value = valid_task()
                value["acceptanceCriteria"] = criteria
                value["acceptanceEvidenceIds"] = {}
                value["acceptanceEvidenceRequirements"] = {}
                with self.assertRaisesRegex(TaskContractError, message):
                    parse_task(value)
        value = valid_task()
        value["acceptanceCriteria"] = [
            "malformed-json|WHEN malformed JSON is parsed|ASSERT STATE REJECTED",
            " malformed-json|WHEN malformed JSON is parsed|ASSERT STATE REJECTED",
        ]
        value["acceptanceEvidenceIds"] = {}
        value["acceptanceEvidenceRequirements"] = {}
        with self.assertRaisesRegex(TaskContractError, "canonical"):
            parse_task(value)

    def test_machine_verifiable_criteria_are_accepted(self) -> None:
        for criterion in (
            "path-traversal|WHEN a request contains traversal|ASSERT ERROR TaskContractError",
            "created-status|WHEN a valid request is submitted|ASSERT STATE CREATED",
            'ready-value|WHEN processing completes successfully|ASSERT VALUE "READY"',
            "model-absent|WHEN automated contract tests execute|ASSERT ABSENT model",
        ):
            value = valid_task()
            value["acceptanceCriteria"] = [criterion]
            value["acceptanceEvidenceIds"] = {criterion: ["criterion-evidence"]}
            value["acceptanceEvidenceRequirements"] = {}
            self.assertEqual(parse_task(value).acceptance_criteria, (criterion,))

    def test_assertion_kinds_and_values_are_strict(self) -> None:
        criteria = (
            "bad-kind|WHEN a request is processed|ASSERT UNKNOWN value",
            "bad-error|WHEN a request is processed|ASSERT ERROR failure",
            'bad-value|WHEN a request is processed|ASSERT VALUE ""',
            "bad-state|WHEN a request is processed|ASSERT STATE ready",
            "bad-absent|WHEN a request is processed|ASSERT ABSENT two words",
        )
        for criterion in criteria:
            value = valid_task()
            value["acceptanceCriteria"] = [criterion]
            value["acceptanceEvidenceIds"] = {criterion: ["evidence"]}
            with self.assertRaisesRegex(TaskContractError, "acceptance criterion"):
                parse_task(value)

    def test_criterion_ids_and_evidence_binding_are_exact(self) -> None:
        first = "same-id|WHEN a request is processed|ASSERT STATE READY"
        second = "same-id|WHEN another request is processed|ASSERT STATE FAILED"
        value = valid_task()
        value["acceptanceCriteria"] = [first, second]
        value["acceptanceEvidenceIds"] = {first: ["one"], second: ["two"]}
        with self.assertRaisesRegex(TaskContractError, "duplicate IDs"):
            parse_task(value)

        value = valid_task()
        value["acceptanceEvidenceIds"] = {}
        with self.assertRaisesRegex(TaskContractError, "requires selected evidence"):
            parse_task(value)

        value = valid_task()
        criterion = value["acceptanceCriteria"][0]
        value["acceptanceEvidenceIds"] = {criterion: ["other-test"]}
        with self.assertRaisesRegex(TaskContractError, "not selected evidence"):
            parse_task(value)

    def test_dispatch_validation_rejects_non_dispatchable_states(self) -> None:
        self.assertEqual(
            parse_dispatch_task_json(json.dumps(valid_task())).status, "APPROVED"
        )
        for status in ("CANCELLED", "QUARANTINED", "MERGED", "DONE", "SUPERSEDED"):
            value = valid_task()
            value["status"] = status
            structural = parse_task(value)
            with self.assertRaisesRegex(TaskContractError, "not dispatchable"):
                validate_for_dispatch(structural)

    def test_required_tests_use_the_trusted_registry(self) -> None:
        for command in ("sh -c 'malicious-command'", "python3 unknown.py",
                        "test > output", "$(malicious-command)"):
            value = valid_task()
            value["requiredTests"] = [command]
            with self.assertRaisesRegex(TaskContractError, "unregistered test"):
                parse_task(value)

    def test_evidence_mappings_require_known_criteria_and_sources(self) -> None:
        value = valid_task()
        value["acceptanceEvidenceIds"] = {"Unknown criterion": ["test"]}
        with self.assertRaisesRegex(TaskContractError, "unknown criterion"):
            parse_task(value)

        value = valid_task()
        criterion = value["acceptanceCriteria"][0]
        value["acceptanceEvidenceRequirements"] = {criterion: ["model_claim"]}
        with self.assertRaisesRegex(TaskContractError, "unapproved source"):
            parse_task(value)

    def test_path_escape_ignored_roots_and_exact_overlap_are_rejected(self) -> None:
        paths = ("/etc/passwd", "../parent", ":(exclude).github/**", ":(top)foo",
                 "-rf", "--", "!modern-app/**", "^.github/**", "modern-app/[.]git/**",
                 ".git/config",
                 ".git/hooks/**", "modern-app/.git/config", "modern-app/.git/hooks/**",
                 ".git/objects/**", "downloads/tool", "extracted/app",
                 "**", "*", "*/tool", ".")
        for path in paths:
            with self.subTest(path=path):
                value = valid_task()
                value["allowedPaths"] = [path]
                with self.assertRaisesRegex(
                    TaskContractError, "prohibited path|unsupported glob"
                ):
                    parse_task(value)
        for alias in (".github/./**", "modern-app/src/**/"):
            value = valid_task()
            value["allowedPaths"] = [alias]
            with self.assertRaisesRegex(TaskContractError, "non-canonical"):
                parse_task(value)
        for protected_root in (".github/**", ".agent-workflow/policy/**",
                               ".agent-workflow/scripts/**", "AGENTS.md"):
            value = valid_task()
            value["allowedPaths"] = [protected_root]
            value["protectedPaths"] = ["docs/**"]
            with self.assertRaisesRegex(TaskContractError, "controller-owned"):
                parse_task(value)
        for allowed, protected in (
            ("modern-app/**", "modern-app/secrets/**"),
            ("modern-app/secrets/**", "modern-app/**"),
            ("modern-app/src/**", "modern-app/src/**"),
        ):
            value = valid_task()
            value["allowedPaths"] = [allowed]
            value["protectedPaths"] = [protected]
            with self.assertRaisesRegex(TaskContractError, "overlap"):
                parse_task(value)

    def test_tracked_path_matching_applies_segment_aware_denies_first(self) -> None:
        task = parse_task(valid_task())
        self.assertTrue(tracked_path_is_allowed(task, "modern-app/src/domain/model.py"))
        for candidate in (
            "modern-app/src/.git/hooks/pre-commit",
            "modern-app/src/downloads/tool",
            ".github/workflows/change.yml",
            "modern-app/src/**",
        ):
            self.assertFalse(tracked_path_is_allowed(task, candidate))

        value = valid_task()
        value["allowedPaths"] = ["modern-app/src/*"]
        one_segment = parse_task(value)
        self.assertTrue(tracked_path_is_allowed(one_segment, "modern-app/src/model.py"))
        self.assertFalse(
            tracked_path_is_allowed(one_segment, "modern-app/src/domain/model.py")
        )

    def test_trusted_task_root_rejects_parent_symlink_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tasks"
            external = Path(temporary) / "external"
            root.mkdir()
            external.mkdir()
            (external / "task.json").write_text(json.dumps(valid_task()))
            (root / "link").symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(TaskContractError, "trusted task root"):
                load_task(root / "link" / "task.json", trusted_root=root)

    def test_limits_and_collection_bounds_are_enforced(self) -> None:
        for field, invalid in (("maxChangedLines", 0), ("maxChangedLines", 5_001),
                               ("maxAttempts", 0), ("maxAttempts", 6)):
            with self.subTest(field=field, invalid=invalid):
                value = valid_task()
                value[field] = invalid
                with self.assertRaisesRegex(TaskContractError, "allowed range"):
                    parse_task(value)
        value = valid_task()
        value["nonGoals"] = [str(index) for index in range(201)]
        with self.assertRaisesRegex(TaskContractError, "bounded list"):
            parse_task(value)

    def test_json_resource_failures_use_contract_error_boundary(self) -> None:
        deeply_nested = "[" * 1_100 + "0" + "]" * 1_100
        with self.assertRaises(TaskContractError):
            parse_task_json(deeply_nested)
        with patch("td_controller.task_contract.json.loads", side_effect=RecursionError):
            with self.assertRaisesRegex(TaskContractError, "unambiguous JSON"):
                parse_task_json("{}")
        with self.assertRaisesRegex(TaskContractError, "unambiguous JSON"):
            parse_task_json("\ud800")
        payload = json.dumps(valid_task()).replace(
            '"maxChangedLines": 500', '"maxChangedLines": ' + "9" * 5_000
        )
        with self.assertRaisesRegex(TaskContractError, "unambiguous JSON"):
            parse_task_json(payload)

    def test_direct_task_payload_size_is_bounded(self) -> None:
        for payload in (b" " * 256_001, " " * 256_001, "😀" * 100_000):
            with self.assertRaisesRegex(TaskContractError, "size limit"):
                parse_task_json(payload)

    def test_task_file_size_and_file_type_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "task.json"
            path.write_bytes(b" " * 1_000_000)
            with self.assertRaisesRegex(TaskContractError, "size limit"):
                load_task(path, trusted_root=root)
            target = root / "target.json"
            target.write_text(json.dumps(valid_task()))
            path.unlink()
            path.symlink_to(target)
            with self.assertRaisesRegex(TaskContractError, "unavailable"):
                load_task(path, trusted_root=root)
            path.unlink()
            os.mkfifo(path)
            with self.assertRaisesRegex(TaskContractError, "not regular"):
                load_task(path, trusted_root=root)


if __name__ == "__main__":
    unittest.main()

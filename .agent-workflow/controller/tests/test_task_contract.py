"""Adversarial tests for the durable task contract."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from td_controller.task_contract import TaskContractError, load_task, parse_task, parse_task_json


def valid_task() -> dict[str, object]:
    criterion = "The deterministic controller suite passes"
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
        self.assertEqual(task.acceptance_criteria, ("The deterministic controller suite passes",))
        self.assertEqual(task.acceptance_evidence_ids[task.acceptance_criteria[0]],
                         ("controller-tests",))
        self.assertFalse(task.human_approval_required)

    def test_repository_task_fixtures_remain_valid(self) -> None:
        root = Path(__file__).parents[2] / "tasks"
        tasks = [load_task(path) for path in sorted(root.glob("*.json"))]

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
        for criteria, message in ((["It works well"], "vague"),
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
                value = valid_task()
                value["acceptanceCriteria"] = criteria
                value["acceptanceEvidenceIds"] = {}
                value["acceptanceEvidenceRequirements"] = {}
                with self.assertRaisesRegex(TaskContractError, message):
                    parse_task(value)
        value = valid_task()
        value["acceptanceCriteria"] = ["Controller returns exit code 2",
                                       " Controller returns exit code 2"]
        value["acceptanceEvidenceIds"] = {}
        value["acceptanceEvidenceRequirements"] = {}
        with self.assertRaisesRegex(TaskContractError, "canonical"):
            parse_task(value)

    def test_concrete_action_led_criteria_are_accepted(self) -> None:
        for criterion in ("Rejects path traversal with TaskContractError",
                          "Returns HTTP 201 for a valid request",
                          "It returns HTTP 201 for a valid request"):
            value = valid_task()
            value["acceptanceCriteria"] = [criterion]
            value["acceptanceEvidenceIds"] = {}
            value["acceptanceEvidenceRequirements"] = {}
            self.assertEqual(parse_task(value).acceptance_criteria, (criterion,))

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
                 "-rf", "--", "!modern-app/**", ".git/config", ".git/hooks/**",
                 ".git/objects/**", "downloads/tool", "extracted/app",
                 "**", "*", "*/tool", ".")
        for path in paths:
            with self.subTest(path=path):
                value = valid_task()
                value["allowedPaths"] = [path]
                with self.assertRaisesRegex(TaskContractError, "prohibited path"):
                    parse_task(value)
        for alias in (".github/./**", "modern-app/src/**/"):
            value = valid_task()
            value["allowedPaths"] = [alias]
            with self.assertRaisesRegex(TaskContractError, "non-canonical"):
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
                load_task(path)
            target = root / "target.json"
            target.write_text(json.dumps(valid_task()))
            path.unlink()
            path.symlink_to(target)
            with self.assertRaisesRegex(TaskContractError, "unavailable"):
                load_task(path)
            path.unlink()
            os.mkfifo(path)
            with self.assertRaisesRegex(TaskContractError, "not regular"):
                load_task(path)


if __name__ == "__main__":
    unittest.main()

"""Adversarial tests for the durable task contract."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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
        "requiredTests": ["python3 -m unittest"],
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
                                  (["Exact result", "Exact result"], "duplicates"),
                                  ([], "bounded list")):
            with self.subTest(criteria=criteria):
                value = valid_task()
                value["acceptanceCriteria"] = criteria
                value["acceptanceEvidenceIds"] = {}
                value["acceptanceEvidenceRequirements"] = {}
                with self.assertRaisesRegex(TaskContractError, message):
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
        for path in ("/etc/passwd", "../parent", "downloads/tool", "extracted/app"):
            with self.subTest(path=path):
                value = valid_task()
                value["allowedPaths"] = [path]
                with self.assertRaisesRegex(TaskContractError, "prohibited path"):
                    parse_task(value)
        value = valid_task()
        value["protectedPaths"] = ["modern-app/src/**"]
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

    def test_task_file_size_is_bounded_before_json_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "task.json"
            path.write_bytes(b" " * 256_001)

            with self.assertRaisesRegex(TaskContractError, "size limit"):
                load_task(path)


if __name__ == "__main__":
    unittest.main()

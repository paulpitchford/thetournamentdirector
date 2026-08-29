"""Adversarial tests for read-only planner output."""

from __future__ import annotations

import copy
import json
import unittest

from td_controller.plan_contract import PlanContractError, parse_plan, parse_plan_json

BASE_SHA = "a" * 40
BACKLOG_SHA = "b" * 64


def proposed_task(
    task_id: str, path: str, *, depends_on: list[str] | None = None
) -> dict[str, object]:
    criterion = (
        f"{task_id.lower()}-tests|WHEN deterministic verification executes for the task|"
        f"ASSERT TEST_PASS {task_id.lower()}-tests"
    )
    return {
        "id": task_id,
        "status": "PROPOSED",
        "parentEpic": "MODERN-APP",
        "objective": f"Propose bounded work for {task_id}",
        "nonGoals": [],
        "dependsOn": depends_on or [],
        "acceptanceCriteria": [criterion],
        "acceptanceEvidenceIds": {criterion: [f"{task_id.lower()}-tests"]},
        "acceptanceEvidenceRequirements": {},
        "requiredTests": ["python3 .agent-workflow/scripts/check_repository.py"],
        "allowedPaths": [path],
        "protectedPaths": [".github/**"],
        "riskClass": "R1",
        "maxChangedLines": 500,
        "maxAttempts": 2,
        "humanApprovalRequired": True,
    }


def valid_plan() -> dict[str, object]:
    return {
        "planId": "PLAN-001",
        "baseSha": BASE_SHA,
        "backlogSha256": BACKLOG_SHA,
        "tasks": [
            proposed_task("APP-001", "modern-app/domain/**"),
            proposed_task("APP-002", "modern-app/ui/**"),
        ],
        "parallelGroups": [["APP-001", "APP-002"]],
        "assumptions": [],
    }


class PlanContractTests(unittest.TestCase):
    def test_valid_plan_is_immutable_and_indexed(self) -> None:
        plan = parse_plan(valid_plan())

        self.assertEqual(plan.plan_id, "PLAN-001")
        self.assertEqual(tuple(plan.tasks_by_id), ("APP-001", "APP-002"))
        self.assertEqual(plan.parallel_groups, (("APP-001", "APP-002"),))

    def test_duplicate_json_keys_are_rejected_recursively(self) -> None:
        payload = json.dumps(valid_plan()).replace(
            '"assumptions": []', '"assumptions": [], "assumptions": []'
        )
        with self.assertRaisesRegex(PlanContractError, "unambiguous JSON"):
            parse_plan_json(payload)

    def test_unknown_missing_and_invalid_source_fields_are_rejected(self) -> None:
        cases = (
            ("extra", True),
            ("baseSha", "not-a-sha"),
            ("backlogSha256", "0" * 63),
            ("planId", "lowercase"),
        )
        for field, invalid in cases:
            value = valid_plan()
            value[field] = invalid
            with self.subTest(field=field):
                with self.assertRaises(PlanContractError):
                    parse_plan(value)
        value = valid_plan()
        value.pop("tasks")
        with self.assertRaisesRegex(PlanContractError, "fields"):
            parse_plan(value)

    def test_planner_cannot_approve_or_remove_human_approval(self) -> None:
        for field, invalid in (("status", "APPROVED"),
                               ("humanApprovalRequired", False)):
            value = valid_plan()
            value["tasks"][0][field] = invalid
            with self.assertRaisesRegex(PlanContractError, "human approval"):
                parse_plan(value)

    def test_duplicate_ids_unknown_dependencies_and_cycles_are_rejected(self) -> None:
        value = valid_plan()
        value["tasks"][1]["id"] = "APP-001"
        with self.assertRaises(PlanContractError):
            parse_plan(value)

        value = valid_plan()
        value["tasks"][0]["dependsOn"] = ["MISSING-001"]
        with self.assertRaisesRegex(PlanContractError, "unknown dependency"):
            parse_plan(value)

        value = valid_plan()
        value["parallelGroups"] = []
        value["tasks"][0]["dependsOn"] = ["APP-002"]
        value["tasks"][1]["dependsOn"] = ["APP-001"]
        with self.assertRaisesRegex(PlanContractError, "cycle"):
            parse_plan(value)

    def test_controller_known_dependencies_are_accepted(self) -> None:
        value = valid_plan()
        value["tasks"][0]["dependsOn"] = ["FOUNDATION-001"]

        plan = parse_plan(value, known_task_ids=frozenset({"FOUNDATION-001"}))

        self.assertEqual(plan.tasks[0].depends_on, ("FOUNDATION-001",))

    def test_parallel_groups_require_unique_known_disjoint_tasks(self) -> None:
        value = valid_plan()
        value["tasks"][1]["allowedPaths"] = ["modern-app/domain/subsystem/**"]
        with self.assertRaisesRegex(PlanContractError, "path claims overlap"):
            parse_plan(value)

        for group in ((["APP-001", "UNKNOWN-001"],),
                      (["APP-001", "APP-001"],),
                      (["APP-001"],)):
            value = valid_plan()
            value["parallelGroups"] = list(group)
            with self.assertRaisesRegex(PlanContractError, "membership|duplicates"):
                parse_plan(value)

        value = valid_plan()
        value["parallelGroups"] = [
            ["APP-001", "APP-002"], ["APP-001", "APP-002"]
        ]
        with self.assertRaisesRegex(PlanContractError, "membership"):
            parse_plan(value)

    def test_invalid_nested_task_is_normalized(self) -> None:
        value = valid_plan()
        value["tasks"][0]["requiredTests"] = ["sh -c malicious"]
        with self.assertRaisesRegex(PlanContractError, "invalid task"):
            parse_plan(value)

    def test_payload_and_collection_limits_fail_closed(self) -> None:
        for payload in (b" " * 512_001, " " * 512_001, "😀" * 130_000):
            with self.assertRaisesRegex(PlanContractError, "size limit"):
                parse_plan_json(payload)
        value = valid_plan()
        value["tasks"] = [copy.deepcopy(value["tasks"][0]) for _ in range(101)]
        with self.assertRaisesRegex(PlanContractError, "bounded non-empty"):
            parse_plan(value)


if __name__ == "__main__":
    unittest.main()

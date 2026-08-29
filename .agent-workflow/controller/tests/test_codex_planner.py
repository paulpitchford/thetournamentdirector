"""Tests for the effect-contained read-only Codex planner provider."""

from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from td_controller.codex_planner import (
    CodexPlannerError,
    CodexPlannerProvider,
    PlannerRequest,
)
from td_controller.review_runtime import ProcessOutput, SystemdCgroupExecutor

BASE_SHA = "a" * 40
BACKLOG = "# Reviewed backlog\n\nBuild the domain foundation.\n"
BACKLOG_SHA = hashlib.sha256(BACKLOG.encode()).hexdigest()


class FakeExecutor:
    """Capture an attested Codex invocation without using a model."""

    def __init__(self, output: ProcessOutput) -> None:
        self.output = output
        self.commands: list[list[str]] = []
        self.schemas: list[dict[str, object]] = []

    def run(
        self,
        command: list[str],
        *,
        input_bytes: bytes,
        cwd: Path,
        timeout_seconds: int,
    ) -> ProcessOutput:
        self.commands.append(command)
        schema = Path(command[command.index("--output-schema") + 1])
        self.schemas.append(json.loads(schema.read_text()))
        self.input_bytes = input_bytes
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        return self.output


def planner_request() -> PlannerRequest:
    return PlannerRequest(
        plan_id="PLAN-001",
        base_sha=BASE_SHA,
        backlog_sha256=BACKLOG_SHA,
        backlog=BACKLOG,
        planning_context={"maxTasks": 10, "instruction": "propose bounded work"},
        known_task_ids=frozenset({"ORCH-001"}),
    )


def proposed_task() -> dict[str, object]:
    criterion = (
        "domain-tests|WHEN deterministic domain verification executes|"
        "ASSERT TEST_PASS domain-tests"
    )
    return {
        "id": "APP-001",
        "status": "PROPOSED",
        "parentEpic": "MODERN-APP",
        "objective": "Build a framework-free domain foundation",
        "nonGoals": [],
        "dependsOn": [],
        "acceptanceCriteria": [criterion],
        "acceptanceEvidence": [
            {
                "criterion": criterion,
                "evidenceIds": ["domain-tests"],
                "requiredSources": [],
            }
        ],
        "requiredTests": ["python3 .agent-workflow/scripts/check_repository.py"],
        "allowedPaths": ["modern-app/src/domain/**"],
        "protectedPaths": [".github/**"],
        "riskClass": "R1",
        "maxChangedLines": 500,
        "maxAttempts": 2,
        "humanApprovalRequired": True,
    }


def plan() -> dict[str, object]:
    return {
        "planId": "PLAN-001",
        "baseSha": BASE_SHA,
        "backlogSha256": BACKLOG_SHA,
        "tasks": [proposed_task()],
        "parallelGroups": [],
        "assumptions": [],
    }


def event_stream(value: dict[str, object], *, tool: str | None = None) -> bytes:
    events: list[dict[str, object]] = [
        {"type": "thread.started", "thread_id": "planner-session"},
        {"type": "turn.started"},
    ]
    if tool is not None:
        events.append({"type": "item.started", "item": {"type": tool}})
    events.extend(
        [
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": json.dumps(value)},
            },
            {"type": "turn.completed"},
        ]
    )
    return ("\n".join(json.dumps(event) for event in events) + "\n").encode()


class CodexPlannerProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime = patch(
            "td_controller.review_runtime._attest_codex_runtime",
            return_value="/pinned/codex",
        )
        runtime.start()
        self.addCleanup(runtime.stop)

    def test_production_provider_uses_systemd_boundary(self) -> None:
        provider = CodexPlannerProvider(planner_request())

        self.assertIsInstance(provider.executor, SystemdCgroupExecutor)

    def test_valid_plan_returns_fresh_session_and_validated_plan(self) -> None:
        executor = FakeExecutor(ProcessOutput(0, event_stream(plan()), b""))
        provider = CodexPlannerProvider(planner_request(), executor=executor)

        result = provider.run(task_id="PLAN-001", role="planner")

        self.assertEqual(result.session_id, "planner-session")
        self.assertEqual(provider.plan.plan_id, "PLAN-001")
        command = executor.commands[0]
        self.assertEqual(command[:4], ["/pinned/codex", "-a", "never", "exec"])
        self.assertIn('default_permissions="deny-all"', command)
        self.assertIn("permissions.deny-all.network.enabled=false", command)
        self.assertNotIn("--sandbox", command)
        developer = next(
            item for item in command if item.startswith("developer_instructions=")
        )
        self.assertIn("untrusted inert", developer)
        self.assertTrue(executor.input_bytes.startswith(b"UNTRUSTED_PLANNING"))
        task_schema = executor.schemas[0]["properties"]["tasks"]["items"]
        self.assertIn("acceptanceEvidence", task_schema["required"])
        self.assertNotIn("acceptanceEvidenceIds", task_schema["properties"])

    def test_output_schema_uses_the_pinned_supported_subset(self) -> None:
        executor = FakeExecutor(ProcessOutput(0, event_stream(plan()), b""))
        CodexPlannerProvider(planner_request(), executor=executor).run(
            task_id="PLAN-001", role="planner"
        )
        allowed = {
            "type", "additionalProperties", "required", "properties", "items", "enum"
        }

        def verify(schema: object) -> None:
            if isinstance(schema, dict):
                self.assertTrue(set(schema).issubset(allowed))
                if schema.get("type") == "object":
                    self.assertIs(schema.get("additionalProperties"), False)
                for key, value in schema.items():
                    if key == "properties":
                        for child in value.values():
                            verify(child)
                    elif key == "items":
                        verify(value)

        verify(executor.schemas[0])

    def test_backlog_digest_is_verified_before_execution(self) -> None:
        with self.assertRaisesRegex(CodexPlannerError, "trusted digest"):
            CodexPlannerProvider(replace(planner_request(), backlog="changed"))

    def test_invocation_must_match_controller_request(self) -> None:
        provider = CodexPlannerProvider(
            planner_request(), executor=FakeExecutor(ProcessOutput(0, b"", b""))
        )
        for task_id, role in (("PLAN-002", "planner"), ("PLAN-001", "review")):
            with self.assertRaisesRegex(CodexPlannerError, "does not match"):
                provider.run(task_id=task_id, role=role)

    def test_oversized_or_non_json_context_is_rejected_before_prompt_build(self) -> None:
        requests = (
            replace(planner_request(), planning_context={"value": "x" * 512_001}),
            replace(planner_request(), planning_context={"value": object()}),
        )
        for request in requests:
            provider = CodexPlannerProvider(
                request, executor=FakeExecutor(ProcessOutput(0, b"", b""))
            )
            with patch.object(provider, "_build_prompt") as build:
                with self.assertRaisesRegex(CodexPlannerError, "bounded JSON|size"):
                    provider.run(task_id="PLAN-001", role="planner")
            build.assert_not_called()

    def test_stale_or_unapproved_output_is_rejected(self) -> None:
        stale = plan()
        stale["baseSha"] = "b" * 40
        approved = plan()
        approved["tasks"][0]["status"] = "APPROVED"
        for value in (stale, approved):
            provider = CodexPlannerProvider(
                planner_request(),
                executor=FakeExecutor(ProcessOutput(0, event_stream(value), b"")),
            )
            with self.assertRaisesRegex(CodexPlannerError, "invalid plan"):
                provider.run(task_id="PLAN-001", role="planner")

    def test_tool_attempt_and_stderr_fail_closed(self) -> None:
        outputs = (
            ProcessOutput(0, event_stream(plan(), tool="command_execution"), b""),
            ProcessOutput(0, event_stream(plan()), b"unexpected"),
        )
        for output in outputs:
            provider = CodexPlannerProvider(
                planner_request(), executor=FakeExecutor(output)
            )
            with self.assertRaises(CodexPlannerError):
                provider.run(task_id="PLAN-001", role="planner")

    def test_failed_reuse_clears_the_previous_validated_plan(self) -> None:
        executor = FakeExecutor(ProcessOutput(0, event_stream(plan()), b""))
        provider = CodexPlannerProvider(planner_request(), executor=executor)
        provider.run(task_id="PLAN-001", role="planner")
        self.assertIsNotNone(provider.plan)

        executor.output = ProcessOutput(0, event_stream(plan()), b"failure")
        with self.assertRaises(CodexPlannerError):
            provider.run(task_id="PLAN-001", role="planner")

        self.assertIsNone(provider.plan)

    def test_provider_failure_does_not_expose_diagnostics(self) -> None:
        provider = CodexPlannerProvider(
            planner_request(),
            executor=FakeExecutor(ProcessOutput(1, b"", b"api_key=secret")),
        )

        with self.assertRaises(CodexPlannerError) as raised:
            provider.run(task_id="PLAN-001", role="planner")

        self.assertNotIn("api_key", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

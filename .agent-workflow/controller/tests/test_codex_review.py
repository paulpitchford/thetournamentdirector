"""Tests for the effect-contained local Codex review provider."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from td_controller.codex_review import (
    CodexReviewError,
    CodexReviewProvider,
    ProcessOutput,
    ReviewRequest,
    SystemdCgroupExecutor,
    TrustedEvidence,
)

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40

class FakeExecutor:
    """Return a configured Codex JSONL stream without model usage."""

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
        """Record safe invocation fields and return configured output."""
        self.commands.append(command)
        schema_path = Path(command[command.index("--output-schema") + 1])
        self.schemas.append(json.loads(schema_path.read_text(encoding="utf-8")))
        self.input_bytes = input_bytes
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        return self.output


def request(role: str = "code_review") -> ReviewRequest:
    """Return a current-SHA review request."""
    return ReviewRequest(
        task_id="TASK-001",
        role=role,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        task_contract={
            "id": "TASK-001",
            "acceptanceCriteria": ["CI passes"],
            "acceptanceEvidenceIds": {"CI passes": ["ci-controller-tests"]},
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


def artifact(role: str = "code_review") -> dict[str, object]:
    """Return a passing structured artifact for one role."""
    return {
        "reviewType": "code_security" if role == "code_review" else "qa",
        "taskId": "TASK-001",
        "baseSha": BASE_SHA,
        "headSha": HEAD_SHA,
        "verdict": "pass",
        "findings": [],
        "acceptanceEvidence": (
            []
            if role == "code_review"
            else [
                {
                    "criterion": "CI passes",
                    "status": "pass",
                    "evidence": "controller-tests: pass",
                    "evidenceRefs": ["ci-controller-tests"],
                }
            ]
        ),
    }


def event_stream(value: dict[str, object], *, tool: str | None = None) -> bytes:
    """Encode a minimal Codex JSONL stream with optional forbidden tool use."""
    events: list[dict[str, object]] = [
        {"type": "thread.started", "thread_id": "fresh-session"},
        {"type": "turn.started"},
    ]
    if tool is not None:
        events.append({"type": "item.started", "item": {"type": tool}})
    events.append(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps(value)},
        }
    )
    events.append({"type": "turn.completed"})
    return ("\n".join(json.dumps(item) for item in events) + "\n").encode()


class CodexReviewProviderTests(unittest.TestCase):
    """Prove tool attempts, stale evidence, and malformed QA fail closed."""

    def setUp(self) -> None:
        runtime = patch(
            "td_controller.review_runtime._attest_codex_runtime",
            return_value="/pinned/codex",
        )
        runtime.start()
        self.addCleanup(runtime.stop)

    def test_production_provider_uses_systemd_cgroup_executor(self) -> None:
        provider = CodexReviewProvider(request())

        self.assertIsInstance(provider.executor, SystemdCgroupExecutor)

    def test_oversized_diff_is_rejected_before_prompt_materialization(self) -> None:
        provider = CodexReviewProvider(replace(request(), diff="x" * 512_001),
                                       executor=FakeExecutor(ProcessOutput(0, b"", b"")))
        with patch.object(provider, "_build_prompt") as build:
            with self.assertRaisesRegex(CodexReviewError, "size limit"):
                provider.run(task_id="TASK-001", role="code_review")
        build.assert_not_called()

    def test_many_evidence_records_are_bounded_before_prompt_build(self) -> None:
        evidence = tuple(TrustedEvidence(f"e-{i}", "local_controller", "x")
                         for i in range(10_000))
        provider = CodexReviewProvider(replace(request(), deterministic_evidence=evidence),
                                       executor=FakeExecutor(ProcessOutput(0, b"", b"")))
        with patch.object(provider, "_build_prompt") as build:
            with self.assertRaisesRegex(CodexReviewError, "size limit"):
                provider.run(task_id="TASK-001", role="code_review")
        build.assert_not_called()

    def test_valid_code_review_returns_session_and_artifact(self) -> None:
        executor = FakeExecutor(
            ProcessOutput(returncode=0, stdout=event_stream(artifact()), stderr=b"")
        )
        provider = CodexReviewProvider(request(), executor=executor)

        result = provider.run(task_id="TASK-001", role="code_review")

        self.assertEqual(result.session_id, "fresh-session")
        self.assertEqual(provider.artifact.verdict, "pass")
        command = executor.commands[0]
        self.assertEqual(command[:4], ["/pinned/codex", "-a", "never", "exec"])
        self.assertNotIn("--sandbox", command)
        self.assertIn('default_permissions="deny-all"', command)
        self.assertIn('web_search="disabled"', command)
        self.assertIn("permissions.deny-all.network.enabled=false", command)
        disabled = {command[index + 1] for index, value in enumerate(command[:-1])
                    if value == "--disable"}
        self.assertIn("shell_tool", disabled)
        self.assertIn("code_mode_host", disabled)
        self.assertIn("view_image", disabled)
        developer = next(value for value in command
                         if value.startswith("developer_instructions="))
        self.assertIn("untrusted inert", developer)
        self.assertIn("acceptanceEvidence must be empty", developer)
        self.assertTrue(executor.input_bytes.startswith(b"UNTRUSTED_REVIEW_PAYLOAD_JSON"))
        self.assertNotIn(b"acceptanceEvidence must", executor.input_bytes)
        properties = executor.schemas[0]["properties"]
        self.assertEqual(properties["reviewType"]["enum"], ["code_security"])
        self.assertEqual(properties["acceptanceEvidence"]["maxItems"], 0)

    def test_missing_turn_lifecycle_is_rejected(self) -> None:
        lines = event_stream(artifact()).splitlines()
        for output in (b"\n".join([lines[0], *lines[2:]]), b"\n".join(lines[:-1])):
            provider = CodexReviewProvider(
                request(), executor=FakeExecutor(ProcessOutput(0, output, b"")))
            with self.assertRaisesRegex(CodexReviewError, "lifecycle"):
                provider.run(task_id="TASK-001", role="code_review")

    def test_valid_qa_requires_acceptance_mapping(self) -> None:
        executor = FakeExecutor(
            ProcessOutput(
                returncode=0,
                stdout=event_stream(artifact("qa_review")),
                stderr=b"",
            )
        )
        provider = CodexReviewProvider(request("qa_review"), executor=executor)

        provider.run(task_id="TASK-001", role="qa_review")

        self.assertEqual(provider.artifact.acceptance_evidence[0].status, "pass")
        properties = executor.schemas[0]["properties"]
        self.assertEqual(properties["reviewType"]["enum"], ["qa"])
        self.assertNotIn("maxItems", properties["acceptanceEvidence"])

    def test_error_item_does_not_surface_provider_message(self) -> None:
        events = [
            {"type": "thread.started", "thread_id": "fresh-session"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "type": "error",
                    "message": "provider failed api_key=sk-exampleabcdefghijklmnop",
                },
            },
        ]
        output = ("\n".join(json.dumps(item) for item in events) + "\n").encode()
        executor = FakeExecutor(ProcessOutput(returncode=0, stdout=output, stderr=b""))
        provider = CodexReviewProvider(request(), executor=executor)

        with self.assertRaisesRegex(CodexReviewError, "structured error") as raised:
            provider.run(task_id="TASK-001", role="code_review")
        self.assertNotIn("sk-example", str(raised.exception))

    def test_unknown_event_without_item_is_rejected(self) -> None:
        events = [
            {"type": "thread.started", "thread_id": "fresh-session"},
            {"type": "tool.completed", "tool": "mcp"},
        ]
        output = ("\n".join(json.dumps(item) for item in events) + "\n").encode()
        provider = CodexReviewProvider(
            request(),
            executor=FakeExecutor(ProcessOutput(0, output, b"")),
        )

        with self.assertRaisesRegex(CodexReviewError, "unknown event shape"):
            provider.run(task_id="TASK-001", role="code_review")

    def test_thread_lifecycle_cannot_hide_tool_item(self) -> None:
        event = {"type": "thread.started", "thread_id": "fresh-session",
                 "item": {"type": "command_execution"}}
        provider = CodexReviewProvider(
            request(), executor=FakeExecutor(ProcessOutput(
                0, (json.dumps(event) + "\n").encode(), b"")))
        with self.assertRaisesRegex(CodexReviewError, "thread event"):
            provider.run(task_id="TASK-001", role="code_review")

    def test_duplicate_thread_identity_is_rejected(self) -> None:
        events = [
            {"type": "thread.started", "thread_id": "first"},
            {"type": "thread.started", "thread_id": "second"},
        ]
        output = ("\n".join(json.dumps(item) for item in events) + "\n").encode()
        provider = CodexReviewProvider(
            request(),
            executor=FakeExecutor(ProcessOutput(0, output, b"")),
        )

        with self.assertRaisesRegex(CodexReviewError, "duplicate thread"):
            provider.run(task_id="TASK-001", role="code_review")

    def test_null_thread_identity_cannot_hide_duplicate(self) -> None:
        events = [{"type": "thread.started", "thread_id": None},
                  {"type": "thread.started", "thread_id": "fresh-session"}]
        output = ("\n".join(json.dumps(item) for item in events) + "\n").encode()
        provider = CodexReviewProvider(
            request(), executor=FakeExecutor(ProcessOutput(0, output, b"")))
        with self.assertRaisesRegex(CodexReviewError, "thread event"):
            provider.run(task_id="TASK-001", role="code_review")

    def test_activity_after_final_message_is_rejected(self) -> None:
        event = {"type": "item.completed", "item": {"type": "reasoning"}}
        lines = event_stream(artifact()).splitlines()
        output = b"\n".join([*lines[:-1], json.dumps(event).encode(), lines[-1]])
        provider = CodexReviewProvider(
            request(), executor=FakeExecutor(ProcessOutput(0, output, b"")))
        with self.assertRaisesRegex(CodexReviewError, "after its final"):
            provider.run(task_id="TASK-001", role="code_review")

    def test_duplicate_event_keys_are_rejected_recursively(self) -> None:
        output = (
            b'{"type":"thread.started","thread_id":"fresh-session"}\n'
            b'{"type":"item.completed","item":{"type":"agent_message",'
            b'"type":"reasoning","text":"ambiguous"}}\n'
        )
        provider = CodexReviewProvider(
            request(),
            executor=FakeExecutor(ProcessOutput(0, output, b"")),
        )

        with self.assertRaisesRegex(CodexReviewError, "ambiguous JSONL"):
            provider.run(task_id="TASK-001", role="code_review")

    def test_forbidden_tool_event_is_rejected(self) -> None:
        executor = FakeExecutor(
            ProcessOutput(
                returncode=0,
                stdout=event_stream(artifact(), tool="command_execution"),
                stderr=b"",
            )
        )
        provider = CodexReviewProvider(request(), executor=executor)

        with self.assertRaisesRegex(CodexReviewError, "forbidden tool"):
            provider.run(task_id="TASK-001", role="code_review")

    def test_stale_head_sha_is_rejected(self) -> None:
        value = artifact()
        value["headSha"] = "c" * 40
        executor = FakeExecutor(
            ProcessOutput(returncode=0, stdout=event_stream(value), stderr=b"")
        )
        provider = CodexReviewProvider(request(), executor=executor)

        with self.assertRaisesRegex(CodexReviewError, "stale Git SHAs"):
            provider.run(task_id="TASK-001", role="code_review")

    def test_qa_without_acceptance_evidence_is_rejected(self) -> None:
        value = artifact("qa_review")
        value["acceptanceEvidence"] = []
        executor = FakeExecutor(
            ProcessOutput(returncode=0, stdout=event_stream(value), stderr=b"")
        )
        provider = CodexReviewProvider(request("qa_review"), executor=executor)

        with self.assertRaisesRegex(CodexReviewError, "map every acceptance"):
            provider.run(task_id="TASK-001", role="qa_review")

    def test_qa_unknown_criterion_is_rejected(self) -> None:
        value = artifact("qa_review")
        value["acceptanceEvidence"][0]["criterion"] = "Invented criterion"
        executor = FakeExecutor(
            ProcessOutput(returncode=0, stdout=event_stream(value), stderr=b"")
        )
        provider = CodexReviewProvider(request("qa_review"), executor=executor)

        with self.assertRaisesRegex(CodexReviewError, "map every acceptance"):
            provider.run(task_id="TASK-001", role="qa_review")

    def test_qa_unknown_evidence_reference_is_rejected(self) -> None:
        value = artifact("qa_review")
        value["acceptanceEvidence"][0]["evidenceRefs"] = ["invented"]
        executor = FakeExecutor(
            ProcessOutput(returncode=0, stdout=event_stream(value), stderr=b"")
        )
        provider = CodexReviewProvider(request("qa_review"), executor=executor)

        with self.assertRaisesRegex(CodexReviewError, "unknown refs"):
            provider.run(task_id="TASK-001", role="qa_review")

    def test_qa_missing_required_evidence_source_is_rejected(self) -> None:
        value = artifact("qa_review")
        qa_request = replace(
            request("qa_review"),
            task_contract={
                "id": "TASK-001",
                "acceptanceCriteria": ["CI passes"],
                "acceptanceEvidenceIds": {"CI passes": ["ci-controller-tests"]},
                "acceptanceEvidenceRequirements": {"CI passes": ["local_rootless"]},
            },
        )
        executor = FakeExecutor(
            ProcessOutput(returncode=0, stdout=event_stream(value), stderr=b"")
        )
        provider = CodexReviewProvider(qa_request, executor=executor)

        with self.assertRaisesRegex(CodexReviewError, "required evidence sources"):
            provider.run(task_id="TASK-001", role="qa_review")

    def test_qa_pass_with_failed_criterion_is_rejected(self) -> None:
        value = artifact("qa_review")
        value["acceptanceEvidence"][0]["status"] = "fail"
        executor = FakeExecutor(
            ProcessOutput(returncode=0, stdout=event_stream(value), stderr=b"")
        )
        provider = CodexReviewProvider(request("qa_review"), executor=executor)

        with self.assertRaisesRegex(CodexReviewError, "aggregate criterion"):
            provider.run(task_id="TASK-001", role="qa_review")

    def test_qa_block_with_failed_criterion_needs_no_separate_finding(self) -> None:
        value = artifact("qa_review")
        value["verdict"] = "block"
        value["acceptanceEvidence"][0]["status"] = "fail"
        executor = FakeExecutor(
            ProcessOutput(returncode=0, stdout=event_stream(value), stderr=b"")
        )
        provider = CodexReviewProvider(request("qa_review"), executor=executor)

        provider.run(task_id="TASK-001", role="qa_review")

        self.assertEqual(provider.artifact.verdict, "block")
        self.assertEqual(provider.artifact.findings, ())

    def test_qa_block_with_all_criteria_passing_is_rejected(self) -> None:
        value = artifact("qa_review")
        value["verdict"] = "block"
        value["findings"] = [
            {
                "id": "unrelated",
                "severity": "medium",
                "path": "README.md",
                "line": 1,
                "evidence": "unrelated",
                "risk": "unrelated",
                "requiredAction": "remove unrelated finding",
                "suggestedTest": None,
                "confidence": 0.9,
            }
        ]
        executor = FakeExecutor(
            ProcessOutput(returncode=0, stdout=event_stream(value), stderr=b"")
        )
        provider = CodexReviewProvider(request("qa_review"), executor=executor)

        with self.assertRaisesRegex(CodexReviewError, "aggregate criterion"):
            provider.run(task_id="TASK-001", role="qa_review")

    def test_qa_duplicate_criterion_is_rejected(self) -> None:
        value = artifact("qa_review")
        value["acceptanceEvidence"].append(value["acceptanceEvidence"][0].copy())
        executor = FakeExecutor(
            ProcessOutput(returncode=0, stdout=event_stream(value), stderr=b"")
        )
        provider = CodexReviewProvider(request("qa_review"), executor=executor)

        with self.assertRaisesRegex(CodexReviewError, "duplicate acceptance"):
            provider.run(task_id="TASK-001", role="qa_review")

    def test_successful_process_with_any_stderr_is_rejected(self) -> None:
        for stderr in (b"hidden tool attempt", b"\n"):
            executor = FakeExecutor(ProcessOutput(
                0, event_stream(artifact()), stderr))
            provider = CodexReviewProvider(request(), executor=executor)
            with self.assertRaisesRegex(CodexReviewError, "unexpected stderr") as raised:
                provider.run(task_id="TASK-001", role="code_review")
            self.assertNotIn("hidden tool", str(raised.exception))

    def test_nonzero_exit_uses_structured_stdout_error(self) -> None:
        events = [
            {
                "type": "item.completed",
                "item": {"type": "error", "message": "model capacity unavailable"},
            }
        ]
        output = ("\n".join(json.dumps(item) for item in events) + "\n").encode()
        executor = FakeExecutor(ProcessOutput(returncode=1, stdout=output, stderr=b""))
        provider = CodexReviewProvider(request(), executor=executor)

        with self.assertRaisesRegex(CodexReviewError, "structured error") as raised:
            provider.run(task_id="TASK-001", role="code_review")
        self.assertNotIn("model capacity", str(raised.exception))

    def test_nonzero_non_object_json_diagnostic_is_normalized(self) -> None:
        executor = FakeExecutor(ProcessOutput(returncode=1, stdout=b"[]\n", stderr=b""))
        provider = CodexReviewProvider(request(), executor=executor)

        with self.assertRaisesRegex(CodexReviewError, "without diagnostics"):
            provider.run(task_id="TASK-001", role="code_review")

    def test_invalid_utf8_event_stream_is_normalized(self) -> None:
        executor = FakeExecutor(ProcessOutput(returncode=0, stdout=b"\xff", stderr=b""))
        provider = CodexReviewProvider(request(), executor=executor)

        with self.assertRaisesRegex(CodexReviewError, "invalid UTF-8"):
            provider.run(task_id="TASK-001", role="code_review")

    def test_nonzero_exit_records_only_safe_failure_classification(self) -> None:
        executor = FakeExecutor(
            ProcessOutput(
                returncode=1,
                stdout=b"",
                stderr=b"request failed: HTTP 429 token=secret-value",
            )
        )
        provider = CodexReviewProvider(request(), executor=executor)

        with self.assertRaisesRegex(CodexReviewError, "rate_limit") as raised:
            provider.run(task_id="TASK-001", role="code_review")
        message = str(raised.exception)
        self.assertRegex(message, r"diagnostic=\d+:[0-9a-f]{12}")
        self.assertNotIn("secret-value", message)

    def test_nonzero_process_exit_is_redacted_and_rejected(self) -> None:
        executor = FakeExecutor(
            ProcessOutput(
                returncode=1,
                stdout=b"",
                stderr=b"provider unavailable token=secret-value",
            )
        )
        provider = CodexReviewProvider(request(), executor=executor)

        with self.assertRaisesRegex(CodexReviewError, "reported stderr") as raised:
            provider.run(task_id="TASK-001", role="code_review")
        self.assertNotIn("secret-value", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import unittest
from pathlib import Path

from td_controller.model_broker import (
    ModelBrokerError,
    ModelBrokerSmokeProbe,
)
from td_controller.review_contract import CodexReviewError
from td_controller.review_runtime import ProcessOutput


def event_stream(message: str, session: str = "session-1") -> bytes:
    events = (
        {"type": "thread.started", "thread_id": session},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": message},
        },
        {"type": "turn.completed"},
    )
    return b"".join(
        json.dumps(event).encode("utf-8") + b"\n" for event in events
    )


class FakeDispatch:
    def __init__(self, output: ProcessOutput | Exception) -> None:
        self.output = output
        self.calls: list[tuple[Path, list[str], bytes, Path, int]] = []

    def __call__(
        self, runtime: Path, arguments: list[str], *, input_bytes: bytes,
        cwd: Path, timeout_seconds: int,
    ) -> ProcessOutput:
        self.calls.append(
            (runtime, arguments, input_bytes, cwd, timeout_seconds)
        )
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


class ModelBrokerSmokeProbeTests(unittest.TestCase):
    def test_valid_response_binds_nonce_and_fresh_session(self) -> None:
        nonce = "a" * 32
        dispatch = FakeDispatch(
            ProcessOutput(
                0,
                event_stream(json.dumps({"nonce": nonce, "status": "ready"})),
                b"",
            )
        )

        result = ModelBrokerSmokeProbe(dispatch=dispatch).run(nonce)

        self.assertEqual(result.nonce, nonce)
        self.assertEqual(result.session_id, "session-1")
        runtime, arguments, prompt, cwd, timeout = dispatch.calls[0]
        self.assertEqual(runtime, cwd / "runtime")
        self.assertEqual(timeout, 300)
        self.assertIn(nonce.encode(), prompt)
        self.assertNotIn(str(Path.cwd()), prompt.decode())
        self.assertEqual(arguments[-1], "-")

    def test_command_disables_tools_rules_environment_and_approvals(self) -> None:
        command = ModelBrokerSmokeProbe._command(Path("/tmp/schema.json"))
        joined = " ".join(command)

        self.assertIn("never exec", joined)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn('default_permissions="deny-all"', command)
        self.assertIn("permissions.deny-all.network.enabled=false", command)
        self.assertIn('shell_environment_policy.inherit="none"', command)
        self.assertIn('web_search="disabled"', command)
        self.assertNotIn("danger-full-access", joined)

    def test_fresh_runs_use_new_nonce_and_reject_replayed_response(self) -> None:
        first = "a" * 32
        second = "b" * 32
        nonces = iter((first, second))
        dispatch = FakeDispatch(
            ProcessOutput(
                0,
                event_stream(json.dumps({"nonce": first, "status": "ready"})),
                b"",
            )
        )
        probe = ModelBrokerSmokeProbe(
            dispatch=dispatch, nonce_factory=lambda: next(nonces)
        )

        self.assertEqual(probe.run_fresh().nonce, first)
        with self.assertRaisesRegex(ModelBrokerError, "validation"):
            probe.run_fresh()
        self.assertIn(first.encode(), dispatch.calls[0][2])
        self.assertIn(second.encode(), dispatch.calls[1][2])

    def test_invalid_nonce_fails_before_dispatch(self) -> None:
        dispatch = FakeDispatch(ProcessOutput(0, b"", b""))
        probe = ModelBrokerSmokeProbe(dispatch=dispatch)

        for nonce in ("", "A" * 32, "a" * 31, "../" + "a" * 29):
            with self.subTest(nonce=nonce):
                with self.assertRaisesRegex(ModelBrokerError, "nonce"):
                    probe.run(nonce)
        self.assertEqual(dispatch.calls, [])

    def test_nonzero_stderr_and_malformed_lifecycle_fail_closed(self) -> None:
        outputs = (
            ProcessOutput(1, b"", b"provider detail"),
            ProcessOutput(0, b"", b"unexpected"),
            ProcessOutput(0, b'{"type":"turn.completed"}\n', b""),
        )
        for output in outputs:
            with self.subTest(output=output):
                probe = ModelBrokerSmokeProbe(
                    dispatch=FakeDispatch(output)
                )
                with self.assertRaises(ModelBrokerError):
                    probe.run("b" * 32)

    def test_wrong_or_ambiguous_payload_fails_closed(self) -> None:
        messages = (
            json.dumps({"nonce": "c" * 32, "status": "wrong"}),
            json.dumps({"nonce": "d" * 32, "status": "ready", "extra": 1}),
            '{"nonce":"' + "c" * 32 + '","nonce":"' + "c" * 32
            + '","status":"ready"}',
            "not-json",
        )
        for message in messages:
            with self.subTest(message=message):
                output = ProcessOutput(0, event_stream(message), b"")
                with self.assertRaises(ModelBrokerError):
                    ModelBrokerSmokeProbe(
                        dispatch=FakeDispatch(output)
                    ).run("c" * 32)

    def test_dispatch_errors_are_normalized_without_diagnostics(self) -> None:
        secret = "credential-value-must-not-escape"
        probe = ModelBrokerSmokeProbe(
            dispatch=FakeDispatch(CodexReviewError(secret))
        )

        with self.assertRaises(ModelBrokerError) as raised:
            probe.run("e" * 32)
        self.assertNotIn(secret, str(raised.exception))

    def test_timeout_configuration_is_bounded(self) -> None:
        for timeout in (29, 601, True):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ModelBrokerError):
                    ModelBrokerSmokeProbe(timeout_seconds=timeout)
